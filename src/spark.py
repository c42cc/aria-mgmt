"""DGX Spark control surface — the single home for reaching and verifying the sparks.

This module is the factored-out core that §8 of `ops/spark/NODES.md` called for:
the node registry, the SSH ground-truth probe, the gate catalog (machine
assertion + an independent Gemini visual question + the fix command), the macOS
Terminal capture, the Gemini visual verdict, and three high-level operations:

  status(node)            -> read-only health JSON (identity / GPU / unified
                             memory / toolchain / cluster link). No Terminal, no
                             Gemini, no model spend. Least privilege: SSH as the
                             node user. This is the fast "how are the sparks?".
  verify(node, role, ...) -> the full "prove it twice" acceptance. Each gate is
                             proven by a machine assertion over the probe's stdout
                             AND an independent Gemini verdict over a screenshot of
                             a real macOS Terminal showing that probe. A
                             machine/Gemini disagreement is itself a loud FAIL.
                             Artifacts land in data/spark/<node>/.
  setup(node, role)       -> run ops/spark/setup_node.sh on the node (idempotent).

`scripts/spark_acceptance.py`, `scripts/spark_cluster.py`, and Aria's spark tools
in `src/tools.py` all call THIS module — there is no second implementation. The
failure posture matches preflight: every gate returns (ok, detail) and a red gate
refuses "all clear" and surfaces the runbook fix.
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent

# Read keys from .env regardless of entry point (CLI script or the bot). This
# mirrors the acceptance harness and is idempotent — it never clobbers a value
# already present in the environment.
try:
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:  # dotenv is a hard dep; if it is somehow absent, os.environ still wins
    pass


# ---------------------------------------------------------------------------
# Node registry — the source of truth for how we address the sparks
# ---------------------------------------------------------------------------
# tailnet IPs (MagicDNS does not resolve from the Mac shell — see NODES.md §2).
# The SSH alias (spark1/spark2) is what the node-user Tailscale-SSH path uses;
# the IP is what root@ and the cluster scripts use.
NODES: dict[str, dict[str, str]] = {
    "spark1": {"ip": "100.106.152.104", "role": "A"},
    "spark2": {"ip": "100.119.143.76", "role": "B"},
}

# Read-only cluster link facts (used only for a *visibility* line in status();
# bringing the link up / cluster ops are deliberately out of these tools' scope).
LINK_IFACE = "enp1s0f0np0"

# Primary model first; the rest are availability fallbacks. A demand spike on one
# model must not fail a genuinely-good gate — that would blame the service for our
# design. (Mirrors src/judge.py.)
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]

# Put ~/.local/bin (the user-level toolchain) on PATH for non-interactive probes.
ENV_PREFIX = "source ~/.config/spark/env.sh 2>/dev/null; "
TMP = Path("/tmp")


def resolve_node(node: str) -> tuple[str, str, str]:
    """(alias, ip, default_role) for a node name. Raises on an unknown node."""
    key = (node or "").strip()
    if key not in NODES:
        raise ValueError(f"unknown spark node {node!r} (known: {', '.join(NODES)})")
    return key, NODES[key]["ip"], NODES[key]["role"]


# ---------------------------------------------------------------------------
# Gate definitions
# ---------------------------------------------------------------------------

@dataclass
class Gate:
    id: str
    title: str
    # Remote command (ground truth + live run). env.sh is prefixed automatically.
    command: str
    # Human-readable, secret-safe label shown on screen above the output.
    display: str
    # (stdout_plus_stderr, returncode) -> (ok, detail)
    assert_fn: Callable[[str, int], tuple[bool, str]]
    gemini_q: str
    fix: str


@dataclass
class GateResult:
    id: str
    title: str
    machine_ok: bool
    machine_detail: str
    gemini_pass: bool
    gemini_reason: str
    verdict: str  # PASS / FAIL
    rc: int
    output_preview: str
    png_path: str
    fix: str = ""


# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _semver_at_least(text: str, floor: tuple[int, int, int]) -> tuple[bool, str]:
    m = _SEMVER.search(text)
    if not m:
        return False, "no semver found in output"
    got = tuple(int(x) for x in m.groups())
    return (got >= floor), f"found {got[0]}.{got[1]}.{got[2]} (floor {floor[0]}.{floor[1]}.{floor[2]})"


def assert_claude_version(out: str, rc: int) -> tuple[bool, str]:
    if rc != 0:
        return False, f"claude --version exited {rc}"
    return _semver_at_least(out, (2, 1, 100))


def assert_claude_doctor(out: str, rc: int) -> tuple[bool, str]:
    low = out.lower()
    bad = any(k in low for k in ("not found", "fatal", "unhealthy", "failed to", "command not found"))
    if bad:
        return False, "doctor output contains a failure marker"
    if rc == 0 or "claude" in low or "version" in low:
        return True, f"doctor ran (rc={rc}), no failure markers"
    return False, f"doctor unclear (rc={rc})"


def assert_claude_auth(out: str, rc: int) -> tuple[bool, str]:
    if rc != 0:
        return False, f"claude -p exited {rc} (auth/model round-trip failed)"
    if re.search(r"\bok\b", out.strip(), re.IGNORECASE):
        return True, "model replied OK (authenticated round-trip)"
    return False, f"no OK in reply: {out.strip()[:80]!r}"


def assert_node_version(out: str, rc: int) -> tuple[bool, str]:
    if rc != 0:
        return False, f"node --version exited {rc}"
    m = re.search(r"v?(\d+)\.\d+\.\d+", out)
    if not m:
        return False, "no node version found"
    major = int(m.group(1))
    return (major >= 18), f"node major {major} (need >=18)"


def assert_gpu(out: str, rc: int) -> tuple[bool, str]:
    if rc != 0:
        return False, f"nvidia-smi/free exited {rc}"
    if "GB10" not in out:
        return False, "no GB10 GPU named in nvidia-smi"
    if re.search(r"NVIDIA-SMI has failed|couldn't communicate|No devices were found", out, re.IGNORECASE):
        return False, "nvidia-smi reported a driver error"
    mem = re.search(r"Mem:\s+(\d+)\s*Gi", out)
    if not mem or int(mem.group(1)) < 100:
        return False, f"unified memory not ~128GB (saw {mem.group(1) if mem else '?'}Gi)"
    return True, f"GB10 present, {mem.group(1)}Gi unified memory"


def assert_toolchain(out: str, rc: int) -> tuple[bool, str]:
    if "MISSING" in out:
        missing = [ln.split()[0] for ln in out.splitlines() if "MISSING" in ln]
        return False, f"missing tools: {missing}"
    return True, "all required tools resolve"


def assert_settings(out: str, rc: int) -> tuple[bool, str]:
    if rc != 0:
        return False, f"reading settings exited {rc}"
    if '"stable"' not in out and "stable" not in out:
        return False, "autoUpdatesChannel not stable"
    if "USE_BUILTIN_RIPGREP" not in out:
        return False, "USE_BUILTIN_RIPGREP missing"
    if "minimumVersion" not in out and not _SEMVER.search(out):
        return False, "minimumVersion missing"
    return True, "stable channel + minimumVersion + USE_BUILTIN_RIPGREP present"


def assert_claude_md(node: str, role: str) -> Callable[[str, int], tuple[bool, str]]:
    role_word = "worker node A" if role == "A" else "worker node B"

    def _fn(out: str, rc: int) -> tuple[bool, str]:
        if rc != 0:
            return False, f"reading CLAUDE.md exited {rc}"
        if node not in out:
            return False, f"identity does not name {node}"
        if role_word not in out:
            return False, f"identity missing role '{role_word}'"
        if "GB10" not in out:
            return False, "identity missing GB10 hardware note"
        return True, f"identity names {node} as {role_word}"

    return _fn


def assert_mcp(out: str, rc: int) -> tuple[bool, str]:
    if rc != 0:
        return False, f"claude mcp list exited {rc}"
    if "filesystem" not in out:
        return False, "filesystem MCP not listed"
    return True, "filesystem MCP registered"


# ---------------------------------------------------------------------------
# Gate catalog
# ---------------------------------------------------------------------------

def build_gates(node: str, role: str) -> list[Gate]:
    return [
        Gate(
            "claude_version", "claude --version >= 2.1.100",
            "timeout 30 claude --version",
            "claude --version",
            assert_claude_version,
            "Does the terminal show `claude --version` printing a version number (2.1.100 or higher) with no error?",
            "Re-run ops/spark/setup_node.sh (curl -fsSL https://claude.ai/install.sh | bash -s stable).",
        ),
        Gate(
            "claude_doctor", "claude doctor healthy",
            "timeout 45 claude doctor 2>&1 | head -40",
            "claude doctor",
            assert_claude_doctor,
            "Does the terminal show `claude doctor` output indicating a healthy install/config, with no 'not found' or failure errors?",
            "Re-run the native installer; check ~/.local/bin is on PATH.",
        ),
        Gate(
            "claude_auth", "headless auth round-trip (claude -p OK)",
            'ANTHROPIC_API_KEY="$(cat ~/.config/spark/anthropic_key)" timeout 60 claude -p "Reply with exactly: OK"',
            'ANTHROPIC_API_KEY=*** claude -p "Reply with exactly: OK"',
            assert_claude_auth,
            "Does the terminal show Claude replying 'OK' to the prompt (a successful authenticated model round-trip) with no authentication error?",
            "Check ANTHROPIC_API_KEY in .env is a valid paid/Console key; re-seed with --run-setup.",
        ),
        Gate(
            "node_version", "node --version >= 18",
            "timeout 20 node --version",
            "node --version",
            assert_node_version,
            "Does the terminal show `node --version` printing v18 or higher?",
            "Re-run setup_node.sh; it installs Node from the official ARM64 tarball into ~/.local/bin.",
        ),
        Gate(
            "gpu", "GB10 GPU + ~128GB unified memory",
            "nvidia-smi; echo '--- MEM ---'; free -h | head -2",
            "nvidia-smi ; free -h",
            assert_gpu,
            "Does the terminal show nvidia-smi reporting an NVIDIA GB10 GPU with no driver error, plus ~128 GB total system memory?",
            "GPU/driver fault is hardware-level: escalate, do not patch around it.",
        ),
        Gate(
            "toolchain", "git/tmux/jq/curl/node/npm/uv/claude resolve",
            "for b in git tmux jq curl node npm uv claude; do printf '%-8s ' \"$b\"; command -v \"$b\" || echo MISSING; done",
            "for b in git tmux jq curl node npm uv claude; do command -v $b; done",
            assert_toolchain,
            "Does the terminal show every listed tool (git, tmux, jq, curl, node, npm, uv, claude) resolving to a path, with none marked MISSING?",
            "Re-run setup_node.sh to install the missing user-level tool.",
        ),
        Gate(
            "settings", "~/.claude/settings.json (stable + floor + ripgrep)",
            "cat ~/.claude/settings.json",
            "cat ~/.claude/settings.json",
            assert_settings,
            "Does the terminal show a settings.json containing autoUpdatesChannel \"stable\", a minimumVersion, and USE_BUILTIN_RIPGREP?",
            "Re-run setup_node.sh to rewrite ~/.claude/settings.json.",
        ),
        Gate(
            "claude_md", "node identity ~/.claude/CLAUDE.md",
            "cat ~/.claude/CLAUDE.md",
            "cat ~/.claude/CLAUDE.md",
            assert_claude_md(node, role),
            f"Does the terminal show a CLAUDE.md identity naming {node} as a worker node on a two-node DGX Spark rig with a GB10 GPU?",
            "Re-run setup_node.sh with the correct ROLE (A for spark1, B for spark2).",
        ),
        Gate(
            "mcp", "filesystem MCP registered",
            "timeout 30 claude mcp list 2>&1",
            "claude mcp list",
            assert_mcp,
            "Does the terminal show `claude mcp list` output that includes a 'filesystem' MCP server?",
            "Re-run setup_node.sh; it runs `claude mcp add filesystem -- npx -y @modelcontextprotocol/server-filesystem $HOME`.",
        ),
    ]


# Gates whose assertion is read-only and free (no model round-trip, no writes).
# status() never runs claude_auth (it spends a Claude call) or setup.
READONLY_GATE_IDS = (
    "claude_version", "claude_doctor", "node_version", "gpu",
    "toolchain", "settings", "claude_md", "mcp",
)


# ---------------------------------------------------------------------------
# SSH ground truth
# ---------------------------------------------------------------------------

def ssh_probe(node: str, command: str, *, timeout: float = 120.0) -> tuple[str, int]:
    """Run the probe over SSH as the node user and return (combined_output, rc).

    This is the ground truth for the machine assertion. Passing the remote
    command as a single argv element means any quoting inside it is the remote
    shell's concern, not the local shell's. `-n` keeps ssh from swallowing the
    rest of a piped script (a real macOS-shell gotcha — see NODES.md §2).
    """
    remote = f"{ENV_PREFIX}{command}"
    try:
        p = subprocess.run(
            ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", node, remote],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "[probe timed out]", 124
    out = p.stdout
    if p.stderr.strip():
        out = f"{out}\n{p.stderr}" if out else p.stderr
    return out.strip("\n"), p.returncode


# ---------------------------------------------------------------------------
# Terminal-driven live run + screenshot (the second, independent proof)
# ---------------------------------------------------------------------------

def _osascript(script: str, *, timeout: float = 120.0) -> tuple[int, str, str]:
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _terminal_cg_window_id() -> int | None:
    """CoreGraphics window number of Terminal's frontmost on-screen window.

    Lets us capture that specific window (screencapture -l) regardless of
    occlusion. Returns None if Quartz is unavailable or no window is found.
    """
    try:
        from Quartz import (  # type: ignore
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )
    except Exception:
        return None
    try:
        infos = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
    except Exception:
        return None
    for w in infos or []:
        if w.get("kCGWindowOwnerName") == "Terminal" and int(w.get("kCGWindowLayer", 0)) == 0:
            num = w.get("kCGWindowNumber")
            if num:
                return int(num)
    return None


def ensure_terminal_window() -> None:
    # Confirm we can drive Terminal (surfaces the Automation-permission error
    # early). Each gate opens its own dedicated window.
    rc, _, err = _osascript('tell application "Terminal" to activate')
    if rc != 0:
        raise RuntimeError(
            "Could not control Terminal.app via osascript. Grant Automation "
            "permission to the controlling app (System Settings > Privacy & "
            f"Security > Automation). Error: {err.strip()}"
        )
    time.sleep(0.5)


def display_and_capture(node: str, gate: Gate, out: str, rc: int, png_path: Path) -> None:
    """Show the real probe output in a Terminal window and screenshot it.

    Ground truth already came from ssh_probe; Terminal is only the human-facing
    surface so the screenshot is a genuine macOS terminal capture. A plain
    `cat` of a temp file (no AppleScript polling, no window-content parsing)
    keeps this deterministic and fast.
    """
    txt = TMP / f"spark_gate_{gate.id}.txt"
    txt.write_text(
        f"=== spark acceptance :: {node} :: {gate.title} ===\n"
        f"$ {gate.display}\n"
        f"{out}\n"
        f"[exit code: {rc}]\n"
    )
    # Open a DEDICATED NEW local Terminal window for the capture. Targeting an
    # existing "front window" is unsafe: the user may have an interactive ssh
    # session in front, and our `cat` would then run on the REMOTE host. A bare
    # `do script` (no `in` clause) always spawns a fresh LOCAL shell window.
    x, y, w, h = 60, 60, 1400, 900
    open_script = (
        'tell application "Terminal"\n'
        '  activate\n'
        f'  do script "clear; cat {txt}; echo"\n'
        f'  set bounds of front window to {{{x}, {y}, {x + w}, {y + h}}}\n'
        '  return id of front window\n'
        'end tell'
    )
    rc2, wid_out, err = _osascript(open_script, timeout=30)
    if rc2 != 0:
        raise RuntimeError(f"Terminal display failed for gate {gate.id}: {err.strip()}")
    wid = wid_out.strip()
    time.sleep(1.4)  # let Terminal paint the cat output before capturing
    # Capture the Terminal WINDOW by its CoreGraphics id (screencapture -l), not
    # a screen rectangle: this grabs the window's own buffer regardless of what
    # is on top (a fullscreen video can otherwise occlude a -R region grab).
    cg = _terminal_cg_window_id()
    cap = None
    if cg is not None:
        cap = subprocess.run(
            ["screencapture", "-x", "-o", "-l", str(cg), str(png_path)],
            capture_output=True, text=True,
        )
    if cap is None or cap.returncode != 0 or not png_path.exists():
        # Fallback: rectangle grab (works when nothing occludes the window).
        cap = subprocess.run(
            ["screencapture", "-x", "-R", f"{x},{y},{w},{h}", str(png_path)],
            capture_output=True, text=True,
        )
    if wid:  # tidy up the window we created (ignore close errors)
        _osascript(f'tell application "Terminal" to close (first window whose id is {wid}) saving no', timeout=15)
    if cap.returncode != 0 or not png_path.exists():
        raise RuntimeError(f"screencapture failed for gate {gate.id}: {cap.stderr.strip()}")


# ---------------------------------------------------------------------------
# Gemini visual verdict
# ---------------------------------------------------------------------------

def gemini_verdict(png_path: Path, question: str) -> tuple[bool, str]:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set in .env — cannot run visual verify")
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    img = types.Part.from_bytes(data=png_path.read_bytes(), mime_type="image/png")
    prompt = (
        "You are verifying one step of a DGX Spark engineer setup from a screenshot of a macOS Terminal. "
        f"Question: {question}\n"
        'Return STRICT JSON only: {"pass": true|false, "reason": "<one short sentence>"}. '
        "Set pass=true ONLY if the screenshot clearly shows the success condition with no error."
    )
    # Transient model-availability spikes (503/429) are ours to absorb, not to
    # blame on the service: retry each model with jittered backoff, then route
    # around an overloaded model to the next one before failing loud.
    transient_markers = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                         "overloaded", "500", "INTERNAL", "deadline", "timeout")
    last = ""
    for model in GEMINI_MODELS:
        for attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model=model,
                    contents=[prompt, img],
                    config=types.GenerateContentConfig(temperature=0.0),
                )
                text = (resp.text or "").strip()
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    data = json.loads(m.group(0))
                    return bool(data.get("pass")), f"[{model}] {str(data.get('reason', ''))[:180]}"
                last = f"{model}: no JSON ({text[:80]!r})"
                break  # got a reply but unparseable -> try next model
            except Exception as e:
                msg = str(e)
                last = f"{model}: {type(e).__name__}: {msg[:120]}"
                if not any(k in msg for k in transient_markers):
                    break  # non-transient (bad model id / auth) -> next model
                time.sleep(2 * (2 ** attempt) + random.uniform(0, 1.0))  # 2,4,8s + jitter
    return False, f"all gemini models failed: {last}"


# ---------------------------------------------------------------------------
# Setup (run the node provisioner)
# ---------------------------------------------------------------------------

def setup(node: str, role: str) -> dict:
    """Run ops/spark/setup_node.sh on the node (idempotent). Loud on failure.

    Tier X (executable): seeds Claude Code + toolchain at user level and injects
    ANTHROPIC_API_KEY over SSH (never stored in the repo, never printed here).
    """
    alias, _ip, default_role = resolve_node(node)
    role = (role or default_role or "").strip().upper()
    if role not in ("A", "B"):
        raise ValueError(f"role must be 'A' or 'B' (got {role!r})")

    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY not in .env — refusing to seed the node without it "
            "(headless auth would be blind)"
        )
    setup_sh = REPO_ROOT / "ops" / "spark" / "setup_node.sh"
    if not setup_sh.exists():
        raise RuntimeError(f"missing {setup_sh}")

    remote_cmd = f"ANTHROPIC_API_KEY='{key}' bash -s -- {role} {alias}"
    with open(setup_sh) as f:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", alias, remote_cmd],
            stdin=f, capture_output=True, text=True, timeout=600,
        )
    tail = (p.stdout or "")[-1500:]
    if p.returncode != 0:
        err_tail = (p.stderr or p.stdout or "").strip()[-800:]
        return {
            "ok": False, "node": alias, "role": role,
            "error": f"setup_node.sh failed (rc={p.returncode})",
            "detail": err_tail,
        }
    return {"ok": True, "node": alias, "role": role, "detail": tail.strip()}


# Backwards-compatible alias for the CLI harness's old name.
def run_setup(node: str, role: str) -> None:
    res = setup(node, role)
    if not res.get("ok"):
        raise RuntimeError(res.get("error", "setup failed"))
    print(f"[setup] {node} seeded OK", flush=True)


# ---------------------------------------------------------------------------
# status() — read-only structured health (no Terminal, no Gemini, no spend)
# ---------------------------------------------------------------------------

_STATUS_CMD = (
    "echo '##id'; hostname; whoami; uname -m; "
    "echo '##gpu'; nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>&1; "
    "echo '##mem'; free -h | awk '/Mem:/{print $2}'; "
    "echo '##tools'; for b in git tmux jq curl node npm uv claude; do "
    "printf '%s=' \"$b\"; command -v \"$b\" >/dev/null 2>&1 && echo ok || echo MISSING; done; "
    "echo '##versions'; claude --version 2>/dev/null | head -1; node --version 2>/dev/null; uv --version 2>/dev/null; "
    f"echo '##link'; ip -br addr show {LINK_IFACE} 2>/dev/null; "
    f"echo speed=$(cat /sys/class/net/{LINK_IFACE}/speed 2>/dev/null)"
)


def _split_sections(out: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    cur = ""
    for line in out.splitlines():
        if line.startswith("##"):
            cur = line[2:].strip()
            sections[cur] = []
        elif cur:
            sections[cur].append(line)
    return sections


def status(node: str, *, timeout: float = 30.0) -> dict:
    """Read-only health for one node. Never spends model budget, never writes.

    Returns a structured dict with an overall `ok`. Unreachable / errored nodes
    are reported loudly (ok=False with the reason), never silently.
    """
    alias, ip, role = resolve_node(node)
    out, rc = ssh_probe(alias, _STATUS_CMD, timeout=timeout)

    if rc != 0 and ("[probe timed out]" in out or not out.strip()):
        return {
            "node": alias, "ip": ip, "role": role, "reachable": False, "ok": False,
            "error": f"ssh probe failed (rc={rc})", "detail": out.strip()[:400],
            "fix": "Check Tailscale is up and the node is online (tailscale status | grep "
                   f"{alias}); re-auth Tailscale SSH if prompted.",
        }

    sec = _split_sections(out)
    id_lines = sec.get("id", [])
    identity = {
        "hostname": id_lines[0].strip() if len(id_lines) > 0 else "",
        "user": id_lines[1].strip() if len(id_lines) > 1 else "",
        "arch": id_lines[2].strip() if len(id_lines) > 2 else "",
    }

    gpu_line = " ".join(sec.get("gpu", [])).strip()
    gpu_ok = "GB10" in gpu_line and not re.search(
        r"NVIDIA-SMI has failed|couldn't communicate|No devices were found", gpu_line, re.IGNORECASE
    )
    gpu_name = gpu_line.split(",")[0].strip() if gpu_line else ""
    gpu_driver = ""
    gm = re.search(r"(\d+\.\d+\.\d+)", gpu_line)
    if gm:
        gpu_driver = gm.group(1)

    mem = (sec.get("mem", [""])[0] or "").strip()

    tools: dict[str, str] = {}
    for ln in sec.get("tools", []):
        if "=" in ln:
            name, _, val = ln.partition("=")
            tools[name.strip()] = val.strip()
    missing = sorted(k for k, v in tools.items() if v != "ok")

    ver_lines = [v.strip() for v in sec.get("versions", []) if v.strip()]
    versions = {
        "claude": next((v for v in ver_lines if re.search(r"\d+\.\d+\.\d+", v) and "v" != v[:1]), ""),
        "node": next((v for v in ver_lines if v.startswith("v")), ""),
        "uv": next((v for v in ver_lines if v.startswith("uv ")), ""),
    }

    link_lines = sec.get("link", [])
    link_iface_line = next((ln for ln in link_lines if LINK_IFACE in ln), "")
    speed_match = re.search(r"speed=(\d+)", "\n".join(link_lines))
    link = {
        "iface": LINK_IFACE,
        "up": "UP" in link_iface_line,
        "speed_mbps": int(speed_match.group(1)) if speed_match and speed_match.group(1) != "" else None,
        "line": link_iface_line.strip(),
    }

    ok = bool(identity["hostname"]) and gpu_ok and not missing
    return {
        "node": alias, "ip": ip, "role": role, "reachable": True, "ok": ok,
        "identity": identity,
        "gpu": {"name": gpu_name, "driver": gpu_driver, "ok": gpu_ok, "raw": gpu_line},
        "unified_memory": mem,
        "tools": tools,
        "missing_tools": missing,
        "versions": versions,
        "cluster_link": link,
    }


# ---------------------------------------------------------------------------
# verify() — the full "prove it twice" acceptance (machine AND Gemini agree)
# ---------------------------------------------------------------------------

def verify(node: str, role: str | None = None, only: Iterable[str] | None = None) -> dict:
    """Run the gate set: machine assertion AND independent Gemini visual verdict.

    A gate PASSES only if both agree; any disagreement is a loud FAIL. Writes one
    PNG per gate + acceptance.json under data/spark/<node>/ and returns the
    report dict. Prints per-gate progress to stdout (the CLI surface). This is the
    single implementation the CLI harness and Aria's spark_verify both call.
    """
    alias, _ip, default_role = resolve_node(node)
    role = (role or default_role or "").strip().upper()
    if role not in ("A", "B"):
        raise ValueError(f"role must be 'A' or 'B' (got {role!r})")

    out_dir = REPO_ROOT / "data" / "spark" / alias
    out_dir.mkdir(parents=True, exist_ok=True)

    gates = build_gates(alias, role)
    if only:
        wanted = {g.strip() for g in only if str(g).strip()}
        gates = [g for g in gates if g.id in wanted]
        if not gates:
            raise ValueError(f"`only` matched no gates ({wanted})")

    ensure_terminal_window()

    results: list[GateResult] = []
    for gate in gates:
        print(f"\n=== gate: {gate.id} :: {gate.title} ===", flush=True)
        png = out_dir / f"{gate.id}.png"
        try:
            g_out, rc = ssh_probe(alias, gate.command)
            display_and_capture(alias, gate, g_out, rc, png)
        except Exception as e:
            print(f"  HARNESS ERROR: {e}", flush=True)
            results.append(GateResult(
                gate.id, gate.title, False, f"harness error: {e}", False, "",
                "FAIL", 1, "", str(png), gate.fix,
            ))
            continue

        machine_ok, machine_detail = gate.assert_fn(g_out, rc)
        print(f"  machine: {'OK' if machine_ok else 'FAIL'} (rc={rc}) — {machine_detail}", flush=True)

        try:
            gpass, greason = gemini_verdict(png, gate.gemini_q)
        except Exception as e:
            gpass, greason = False, f"gemini error: {e}"
        print(f"  gemini : {'PASS' if gpass else 'FAIL'} — {greason}", flush=True)

        verdict = "PASS" if (machine_ok and gpass) else "FAIL"
        if machine_ok != gpass:
            verdict = "FAIL"  # disagreement is a loud failure, never silently resolved
            print("  !! machine/gemini DISAGREEMENT — treating as FAIL", flush=True)
        print(f"  -> {verdict}", flush=True)

        results.append(GateResult(
            gate.id, gate.title, machine_ok, machine_detail, gpass, greason,
            verdict, rc, g_out[:600], str(png), "" if verdict == "PASS" else gate.fix,
        ))

    passed = [r for r in results if r.verdict == "PASS"]
    failed = [r for r in results if r.verdict != "PASS"]
    report = {
        "node": alias,
        "role": role,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": {"total": len(results), "pass": len(passed), "fail": len(failed)},
        "gates": [r.__dict__ for r in results],
    }
    (out_dir / "acceptance.json").write_text(json.dumps(report, indent=2))
    return report
