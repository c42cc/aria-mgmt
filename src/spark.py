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
import shlex
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


# ===========================================================================
# live_visuals_4 Claude Code workspace + headless run surface
# ===========================================================================
# Aria stands up the SAME Claude Code environment on a spark that the project
# uses on the Mac (the live_visuals_4_CC control-plane), then dispatches the
# forensic audit + collapse as a DETACHED tmux run and supervises it by polling
# files over SSH. A dropped connection, a bot restart, or Aria going idle never
# kills or loses the run — that is the robustness the live Discord stream lacks.
#
# The Mac-side orchestrator is ops/spark/setup_cc_workspace.sh; the run uses the
# node's `claude` on the Max subscription with ANTHROPIC_API_KEY stripped from
# the run env so the metered key can never shadow the subscription (mirrors the
# billing guard in src/claude_code.py).

AGI_ENV = REPO_ROOT.parent
LV4_LOCAL = AGI_ENV / "live_visuals_4"
SETUP_CC_SCRIPT = REPO_ROOT / "ops" / "spark" / "setup_cc_workspace.sh"
REMOTE_WORKSPACE = "live_visuals_4"            # under the node user's $HOME
REMOTE_RUNS = ".cache/spark_cc_runs"           # under the node user's $HOME
REMOTE_CREDS = ".claude/.credentials.json"     # subscription OAuth (Linux home)
LOCAL_RUN_ARTIFACTS = REPO_ROOT / "data" / "spark" / "runs"
LEDGER_NAME = "TODO_GO_FORWARD_FORENSIC_AUDIT_COLLAPSE_LEDGER.md"
DEFAULT_AUDIT_INSTRUCTION = REPO_ROOT / "ops" / "spark" / "audit_collapse_instruction.md"
DEFAULT_RUN_MODE = "bypassPermissions"
_VALID_MODES = ("plan", "default", "acceptEdits", "bypassPermissions")

# Model + reasoning policy.
#   - The node's *interactive/default* claude is Opus 4.8 at MAX effort (set in
#     the committed .claude/settings.json + ~/.bashrc by setup_cc_workspace.sh).
#   - The forensic-audit RUN intentionally differs: Opus 4.8 at MEDIUM effort
#     with NO extended thinking (MAX_THINKING_TOKENS=0 disables thinking even on
#     4.8, where effort otherwise drives reasoning depth).
AUDIT_MODEL = "claude-opus-4-8"
AUDIT_EFFORT = "medium"
AUDIT_EXTENDED_THINKING = False
_VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _new_run_id() -> str:
    return time.strftime("cc_%Y%m%dT%H%M%S")


def _ssh_put(node: str, rel_path: str, content: str, *, executable: bool = False,
             timeout: float = 60.0) -> None:
    """Write `content` to ~/rel_path on the node (creating parent dirs). The path
    is relative to the node user's $HOME (ssh's default cwd)."""
    parent = os.path.dirname(rel_path)
    pre = f"mkdir -p {shlex.quote(parent)} && " if parent else ""
    p = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", node,
         f"{pre}cat > {shlex.quote(rel_path)}"],
        input=content, capture_output=True, text=True, timeout=timeout,
    )
    if p.returncode != 0:
        raise RuntimeError(f"could not write ~/{rel_path} on {node}: {p.stderr.strip()}")
    if executable:
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", node, f"chmod +x {shlex.quote(rel_path)}"],
            capture_output=True, text=True, timeout=timeout,
        )


def sync_workspace(node: str, *, mirror: bool = False, skip_bootstrap: bool = False,
                   smoke_gate: bool = False, timeout: float = 2400.0) -> dict:
    """Stand up / update the live_visuals_4 CC workspace on the node via
    ops/spark/setup_cc_workspace.sh (rsync repo + overlay control-plane +
    bootstrap). Loud on failure; never sets ANTHROPIC_API_KEY."""
    alias, _ip, _role = resolve_node(node)
    if not SETUP_CC_SCRIPT.exists():
        raise RuntimeError(f"missing {SETUP_CC_SCRIPT}")
    if not (LV4_LOCAL / ".git").is_dir():
        raise RuntimeError(f"live_visuals_4 checkout not found at {LV4_LOCAL}")
    env = dict(os.environ)
    if skip_bootstrap:
        env["SKIP_BOOTSTRAP"] = "1"
    if smoke_gate:
        env["SMOKE_GATE"] = "1"
    args = ["bash", str(SETUP_CC_SCRIPT), alias]
    if mirror:
        args.append("--mirror")
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
    tail = (p.stdout or "")[-2000:]
    if p.returncode != 0:
        return {"ok": False, "node": alias, "error": "setup_cc_workspace.sh failed",
                "detail": ((p.stderr or "") + tail).strip()[-1500:]}
    return {"ok": True, "node": alias, "detail": tail.strip()}


def cc_auth_status(node: str, *, probe: bool = False, timeout: float = 60.0) -> dict:
    """Is the node's claude authenticated on the Max subscription? Cheap check =
    the OAuth creds file exists and no metered key leaks into the login env.
    `probe=True` spends one tiny subscription call to confirm a live round-trip."""
    alias, _ip, _role = resolve_node(node)
    cmd = (f'test -f "$HOME/{REMOTE_CREDS}" && echo HAS_CREDS || echo NO_CREDS; '
           r'printf "KEYENV=%s\n" "${ANTHROPIC_API_KEY:+set}"')
    out, _rc = ssh_probe(alias, cmd, timeout=timeout)
    has_creds = "HAS_CREDS" in out
    key_in_env = "KEYENV=set" in out
    result = {"node": alias, "has_oauth_creds": has_creds, "key_in_login_env": key_in_env}
    if probe:
        pout, prc = ssh_probe(
            alias, 'env -u ANTHROPIC_API_KEY claude -p "Reply with exactly: OK"',
            timeout=max(timeout, 90),
        )
        ok = prc == 0 and re.search(r"\bok\b", pout.strip(), re.IGNORECASE) is not None
        result["subscription_ok"] = bool(ok)
        result["probe_detail"] = pout.strip()[:200]
        result["ok"] = bool(ok)
    else:
        result["ok"] = has_creds
    return result


_RUNNER_TEMPLATE = """#!/usr/bin/env bash
# Detached Claude Code run launched by src/spark.py::run_audit. Survives SSH
# drops (tmux); writes stream-json to run.log; ANTHROPIC_API_KEY stripped so the
# Max subscription is used (never the metered key).
set -uo pipefail
source "$HOME/.config/spark/env.sh" 2>/dev/null || true
# Pin reasoning HERE: this runner is non-interactive (it does NOT source
# ~/.bashrc), so the node's interactive 'max effort' default never applies. The
# CLAUDE_CODE_EFFORT_LEVEL env var beats the --effort flag, so we set it for
# determinism; {thinking_label} extended thinking.
export CLAUDE_CODE_EFFORT_LEVEL="{effort}"
{thinking_export}
RUNDIR="$HOME/{runs}/{run_id}"
mkdir -p "$RUNDIR"
cd "$HOME/{workspace}" || {{ echo "FATAL: no workspace $HOME/{workspace}" > "$RUNDIR/run.log"; echo 127 > "$RUNDIR/rc"; touch "$RUNDIR/DONE"; exit 127; }}
git switch "{branch}" 2>/dev/null || git switch -c "{branch}" 2>/dev/null || git checkout -b "{branch}" 2>/dev/null || true
{{ echo "[runner] $(date -Is) model={model} effort={effort} thinking={thinking_label} branch=$(git branch --show-current) head=$(git rev-parse --short HEAD)"; }} >> "$RUNDIR/meta.txt"
env -u ANTHROPIC_API_KEY claude -p "$(cat "$RUNDIR/instruction.md")" \\
  --model {model} --permission-mode {mode} --output-format stream-json --verbose \\
  >> "$RUNDIR/run.log" 2>&1
echo $? > "$RUNDIR/rc"
touch "$RUNDIR/DONE"
"""


def run_audit(node: str, instruction: str, *, branch: str | None = None,
              mode: str = DEFAULT_RUN_MODE, run_id: str | None = None,
              model: str = AUDIT_MODEL, effort: str = AUDIT_EFFORT,
              extended_thinking: bool = AUDIT_EXTENDED_THINKING,
              force_unauthed: bool = False) -> dict:
    """Launch a detached, disconnection-proof Claude Code run on the node: write
    the instruction + a runner into ~/REMOTE_RUNS/<run_id>/, ensure a fresh
    branch, and start `claude -p` in a detached tmux session that streams
    stream-json to run.log and touches a DONE sentinel. Returns immediately.

    Model/reasoning default to the audit policy (Opus 4.8, medium effort, no
    extended thinking); override per-run if needed."""
    alias, _ip, _role = resolve_node(node)
    run_id = run_id or _new_run_id()
    branch = branch or f"collapse/{time.strftime('%Y%m%d-%H%M%S')}"
    if mode not in _VALID_MODES:
        raise ValueError(f"bad permission mode {mode!r} (one of {_VALID_MODES})")
    if effort not in _VALID_EFFORTS:
        raise ValueError(f"bad effort {effort!r} (one of {_VALID_EFFORTS})")
    if not instruction.strip():
        raise ValueError("instruction is required")

    # Refuse to launch a run that would immediately fail auth (loud, not silent).
    auth = cc_auth_status(alias)
    if not auth.get("has_oauth_creds") and not force_unauthed:
        return {"ok": False, "node": alias,
                "error": f"node claude is not subscription-authed (~/{REMOTE_CREDS} "
                         f"missing). Run `ssh -t {alias}` then `claude` -> `/login`, "
                         "then retry. (force_unauthed=True launches anyway.)"}

    thinking_export = ("export MAX_THINKING_TOKENS=0" if not extended_thinking
                       else "# extended thinking enabled (effort drives depth on Opus 4.8)")
    thinking_label = "NO" if not extended_thinking else "WITH"

    rundir_rel = f"{REMOTE_RUNS}/{run_id}"
    _ssh_put(alias, f"{rundir_rel}/instruction.md", instruction)
    runner = _RUNNER_TEMPLATE.format(
        runs=REMOTE_RUNS, run_id=run_id, workspace=REMOTE_WORKSPACE, branch=branch,
        mode=mode, model=model, effort=effort,
        thinking_export=thinking_export, thinking_label=thinking_label,
    )
    _ssh_put(alias, f"{rundir_rel}/run.sh", runner, executable=True)

    launch = (f"tmux new-session -d -s {shlex.quote(run_id)} "
              f'"bash $HOME/{rundir_rel}/run.sh"')
    out, rc = ssh_probe(alias, launch, timeout=30)
    if rc != 0:
        return {"ok": False, "node": alias, "run_id": run_id,
                "error": f"tmux launch failed (rc={rc})", "detail": out.strip()[:400]}
    return {"ok": True, "node": alias, "run_id": run_id, "branch": branch, "mode": mode,
            "model": model, "effort": effort, "extended_thinking": extended_thinking,
            "tmux_session": run_id, "rundir": f"~/{rundir_rel}",
            "log": f"~/{rundir_rel}/run.log",
            "note": "detached tmux run started; poll with run_status(node, run_id)."}


def _parse_stream_json(log_text: str) -> dict:
    """Pull the human-relevant signal out of a claude stream-json log tail."""
    last_assistant = ""
    last_tool = ""
    result: dict = {}
    n_assistant = 0
    for raw in log_text.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        t = ev.get("type")
        if t == "assistant":
            for blk in (ev.get("message", {}) or {}).get("content", []) or []:
                if blk.get("type") == "text" and blk.get("text", "").strip():
                    last_assistant = blk["text"].strip()
                    n_assistant += 1
                elif blk.get("type") == "tool_use":
                    name = blk.get("name", "")
                    inp = blk.get("input", {})
                    last_tool = (f"Bash: {str(inp.get('command', ''))[:160]}"
                                 if name == "Bash" else f"{name}: {str(inp)[:120]}")
        elif t == "result":
            result = {
                "subtype": ev.get("subtype", ""),
                "is_error": bool(ev.get("is_error", False)),
                "cost_usd": ev.get("total_cost_usd"),
                "num_turns": ev.get("num_turns"),
            }
    return {"last_assistant": last_assistant[:1200], "last_tool": last_tool,
            "n_assistant_turns": n_assistant, "result": result}


def run_status(node: str, run_id: str, *, timeout: float = 45.0) -> dict:
    """Poll a detached run over SSH (decoupled from any live stream): tmux
    liveness, the DONE sentinel + exit code, branch/commit progress, the parsed
    last assistant turn / current tool, and notional cost."""
    alias, _ip, _role = resolve_node(node)
    rundir = f"{REMOTE_RUNS}/{run_id}"
    cmd = (
        f"echo '##alive'; tmux has-session -t {shlex.quote(run_id)} 2>/dev/null && echo yes || echo no; "
        f"echo '##done'; test -f \"$HOME/{rundir}/DONE\" && echo yes || echo no; "
        f"echo '##rc'; cat \"$HOME/{rundir}/rc\" 2>/dev/null; "
        f"echo '##git'; (cd \"$HOME/{REMOTE_WORKSPACE}\" 2>/dev/null && "
        f"git branch --show-current 2>/dev/null && git rev-parse --short HEAD 2>/dev/null && "
        f"git rev-list --count main..HEAD 2>/dev/null); "
        f"echo '##logtail'; tail -c 200000 \"$HOME/{rundir}/run.log\" 2>/dev/null"
    )
    out, rc = ssh_probe(alias, cmd, timeout=timeout)
    sec = _split_sections(out)
    alive = (sec.get("alive", ["no"])[0].strip() == "yes") if sec.get("alive") else False
    done = (sec.get("done", ["no"])[0].strip() == "yes") if sec.get("done") else False
    rc_lines = [l for l in sec.get("rc", []) if l.strip()]
    exit_code = int(rc_lines[0]) if rc_lines and rc_lines[0].strip().lstrip("-").isdigit() else None
    git_lines = [l for l in sec.get("git", []) if l.strip()]
    branch = git_lines[0].strip() if len(git_lines) > 0 else ""
    head = git_lines[1].strip() if len(git_lines) > 1 else ""
    commits = git_lines[2].strip() if len(git_lines) > 2 else ""
    parsed = _parse_stream_json("\n".join(sec.get("logtail", [])))
    state = "running" if (alive and not done) else ("finished" if done else "unknown")
    return {
        "node": alias, "run_id": run_id, "state": state,
        "running": alive, "done": done, "exit_code": exit_code,
        "branch": branch, "head_commit": head, "commits_on_branch": commits,
        "last_assistant": parsed["last_assistant"], "last_tool": parsed["last_tool"],
        "assistant_turns": parsed["n_assistant_turns"], "result": parsed["result"],
        "ssh_rc": rc,
    }


def fetch_results(node: str, run_id: str, *, branch: str | None = None,
                  timeout: float = 600.0) -> dict:
    """Pull a finished run's artifacts back to the Mac: the run log, the refreshed
    ledger, and an incremental git bundle of the collapse branch (importable into
    the local live_visuals_4 with `git fetch <bundle> <branch>:<branch>`)."""
    alias, _ip, _role = resolve_node(node)
    rundir = f"{REMOTE_RUNS}/{run_id}"
    local_dir = LOCAL_RUN_ARTIFACTS / alias / run_id
    local_dir.mkdir(parents=True, exist_ok=True)

    if not branch:
        bout, _ = ssh_probe(alias, f'cd "$HOME/{REMOTE_WORKSPACE}" && git branch --show-current', timeout=30)
        lines = [l for l in bout.strip().splitlines() if l.strip()]
        branch = lines[-1].strip() if lines else ""

    artifacts: dict = {"node": alias, "run_id": run_id, "branch": branch,
                       "local_dir": str(local_dir), "fetched": []}

    bundle_ok = False
    if branch and branch != "main":
        bcmd = (f'cd "$HOME/{REMOTE_WORKSPACE}" && '
                f'git bundle create "$HOME/{rundir}/collapse.bundle" {shlex.quote(branch)} --not main 2>&1 '
                f'|| git bundle create "$HOME/{rundir}/collapse.bundle" {shlex.quote(branch)} 2>&1')
        bout, brc = ssh_probe(alias, bcmd, timeout=180)
        bundle_ok = brc == 0
        artifacts["bundle_detail"] = bout.strip()[-300:]

    def _pull(remote_rel: str, dest_name: str | None = None) -> bool:
        dest = str(local_dir / (dest_name or os.path.basename(remote_rel)))
        p = subprocess.run(
            ["rsync", "-az", "-e", "ssh -o BatchMode=yes", f"{alias}:{remote_rel}", dest],
            capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode == 0

    if _pull(f"{rundir}/run.log"):
        artifacts["fetched"].append("run.log")
    if _pull(f"{rundir}/rc"):
        artifacts["fetched"].append("rc")
    if bundle_ok and _pull(f"{rundir}/collapse.bundle"):
        artifacts["fetched"].append("collapse.bundle")
        artifacts["bundle_path"] = str(local_dir / "collapse.bundle")
    if _pull(f"{REMOTE_WORKSPACE}/{LEDGER_NAME}", LEDGER_NAME):
        artifacts["fetched"].append(LEDGER_NAME)

    if artifacts.get("bundle_path"):
        artifacts["import_hint"] = (
            f"git -C {LV4_LOCAL} fetch {artifacts['bundle_path']} {branch}:{branch} "
            f"&& git -C {LV4_LOCAL} push origin {branch}")
    return artifacts


# ===========================================================================
# Local-brain model serving (Section C, simplified for the chat agent)
# ===========================================================================
# Aria's one agent loop speaks the Anthropic Messages API. vLLM now serves that
# API natively (/v1/messages — vLLM #22627 + #27882), so serving an open-source
# model HERE and pointing ANTHROPIC_BASE_URL at this node makes the loop run on a
# LOCAL brain with ZERO loop changes. This is the relocation of the dysfunctional
# primitive — a remote, metered, cloud-bound brain — into a local weights
# artifact behind the same interface the loop already uses.
#
# ops/spark/serve_model.sh is the node-side engine (vLLM under tmux, or the NGC
# container); these functions are the Mac-side orchestration over Tailscale SSH,
# plus the PURE assertions the serve gate (scripts/spark_serve.py) proves twice
# (machine + an independent Gemini reading of a Terminal screenshot).
#
# Single-node, independent-worker only (Profile 1). The 2-node cluster link is
# power-throttled (NODES.md §9), so distributed inference is out of scope here.

SERVE_SCRIPT = REPO_ROOT / "ops" / "spark" / "serve_model.sh"
SERVE_PORT = 8000
SERVE_SESSION = "vllm_serve"
SERVED_NAME = "local-brain"               # the stable --served-model-name
# A loaded model holds far more than this; the floor only separates "weights
# resident" from "GPU idle" so the gate can tell the server actually loaded.
SERVE_GPU_MIN_USED_MIB = 20_000

# Bench candidates. The bench step (scripts/spark_serve.py --bench) stands up
# each behind the SAME served name and picks the default on tool-call
# reliability + latency — the choice is a one-env-var swap, never a code change.
SERVE_MODELS: dict[str, dict] = {
    "gpt-oss-120b": {
        "hf": "openai/gpt-oss-120b", "parser": "openai",
        "max_model_len": 65536, "gpu_mem_util": "0.85",
        "note": "MXFP4 MoE, ~56-60 tok/s on GB10, ~100GB — most capable but quick",
    },
    "qwen3-30b-a3b": {
        "hf": "Qwen/Qwen3-30B-A3B", "parser": "hermes",
        "max_model_len": 65536, "gpu_mem_util": "0.80",
        "note": "30B-A3B MoE — snappier first token, more room for context/concurrency",
    },
}
DEFAULT_SERVE_MODEL = "gpt-oss-120b"


def serve_endpoint(node: str, port: int = SERVE_PORT) -> str:
    """Base URL the Mac (and Aria's local-brain process) use to reach the node's
    vLLM over Tailscale. Uses the tailnet IP (MagicDNS does not resolve from the
    Mac shell — NODES.md §2)."""
    _alias, ip, _role = resolve_node(node)
    return f"http://{ip}:{port}"


def _resolve_serve_model(model: str | None) -> tuple[str, dict]:
    """(key, cfg) for a registry name OR an ad-hoc HF id (hermes parser default)."""
    key = (model or DEFAULT_SERVE_MODEL).strip()
    if key in SERVE_MODELS:
        return key, SERVE_MODELS[key]
    return key, {
        "hf": key, "parser": "hermes", "max_model_len": 65536,
        "gpu_mem_util": "0.80", "note": "ad-hoc model id (not in registry)",
    }


# ---------------------------------------------------------------------------
# Mac-side orchestration: pipe serve_model.sh over SSH (mirrors setup()).
# ---------------------------------------------------------------------------

def _pipe_serve(node: str, subcmd: str, env_prefix: str = "", *, timeout: float = 60.0):
    alias, _ip, _role = resolve_node(node)
    if not SERVE_SCRIPT.exists():
        raise RuntimeError(f"missing {SERVE_SCRIPT}")
    with open(SERVE_SCRIPT) as f:
        return subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", alias,
             f"{env_prefix}bash -s -- {subcmd}"],
            stdin=f, capture_output=True, text=True, timeout=timeout,
        )


def serve_start(node: str, model: str | None = None, *, served_name: str = SERVED_NAME,
                port: int = SERVE_PORT, engine: str = "auto", tool_parser: str | None = None,
                max_model_len: int | None = None, gpu_mem_util: str | None = None) -> dict:
    """Bring vLLM up on the node (idempotent; a healthy server is a no-op).

    Returns immediately after the launch is issued — weights may still be
    downloading/loading. Poll with serve_status(node) until healthy."""
    key, cfg = _resolve_serve_model(model)
    parser = tool_parser or cfg["parser"]
    mml = max_model_len or cfg["max_model_len"]
    gmu = gpu_mem_util or cfg["gpu_mem_util"]
    env = (
        f"MODEL={shlex.quote(cfg['hf'])} SERVED_NAME={shlex.quote(served_name)} "
        f"TOOL_PARSER={shlex.quote(parser)} PORT={port} MAX_MODEL_LEN={mml} "
        f"GPU_MEM_UTIL={gmu} SERVE_ENGINE={shlex.quote(engine)} "
    )
    try:
        p = _pipe_serve(node, "start", env, timeout=900)
    except subprocess.TimeoutExpired:
        return {"ok": False, "node": resolve_node(node)[0], "error": "serve start timed out"}
    ok = p.returncode == 0
    alias = resolve_node(node)[0]
    return {
        "ok": ok, "node": alias, "model_key": key, "model": cfg["hf"],
        "served_name": served_name, "tool_parser": parser, "port": port,
        "endpoint": serve_endpoint(node, port),
        "detail": (p.stdout or "").strip()[-1500:],
        "error": "" if ok else (p.stderr or p.stdout or "").strip()[-800:],
        "note": "launch issued; poll serve_status until healthy (weights may be loading).",
    }


def serve_stop(node: str, *, port: int = SERVE_PORT) -> dict:
    """Tear the server down (tmux session + container). Weights cache is kept."""
    try:
        p = _pipe_serve(node, "stop", f"PORT={port} ", timeout=60)
    except subprocess.TimeoutExpired:
        return {"ok": False, "node": resolve_node(node)[0], "error": "serve stop timed out"}
    return {"ok": p.returncode == 0, "node": resolve_node(node)[0],
            "detail": (p.stdout or "").strip()[-800:]}


def serve_status(node: str, *, port: int = SERVE_PORT, timeout: float = 30.0) -> dict:
    """Read-only serve health: tmux/container liveness, /v1/models, GPU residency."""
    alias = resolve_node(node)[0]
    try:
        p = _pipe_serve(node, "status", f"PORT={port} ", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "node": alias, "reachable": False, "error": "status probe timed out"}
    out = p.stdout or ""
    sec = _split_sections(out)
    serve_kv: dict[str, str] = {}
    for ln in sec.get("serve", []):
        if "=" in ln:
            k, _, v = ln.partition("=")
            serve_kv[k.strip()] = v.strip()
    models_blob = "\n".join(sec.get("models", []))
    gpu_blob = "\n".join(sec.get("gpu", [])).strip()
    healthy = serve_kv.get("healthy") == "yes" and SERVED_NAME in models_blob
    return {
        "ok": healthy, "node": alias, "reachable": p.returncode == 0,
        "endpoint": serve_endpoint(node, port), "port": port,
        "tmux_alive": serve_kv.get("tmux_alive") == "yes",
        "container_alive": serve_kv.get("container_alive") == "yes",
        "healthy": healthy, "served_name": serve_kv.get("served_name", SERVED_NAME),
        "models_raw": models_blob.strip()[:400], "gpu": gpu_blob[:200],
    }


# ---------------------------------------------------------------------------
# /v1/messages probes (Mac -> node over Tailscale) — exactly the wire the loop
# uses. The body + HTTP status are returned so the gate can both assert AND
# screenshot the real reply.
# ---------------------------------------------------------------------------

def _curl(url: str, *, method: str = "GET", payload: dict | None = None,
          timeout: float = 120.0) -> tuple[str, int]:
    args = ["curl", "-sS", "-X", method, url,
            "-H", "content-type: application/json",
            "-H", "anthropic-version: 2023-06-01",
            "-H", "authorization: Bearer local-brain"]
    if payload is not None:
        args += ["-d", json.dumps(payload)]
    args += ["-w", "\nHTTP_STATUS=%{http_code}\n"]
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "[curl timed out]", 124
    out = p.stdout + (("\n" + p.stderr) if p.stderr.strip() else "")
    return out.strip("\n"), p.returncode


def models_curl(node: str, *, port: int = SERVE_PORT, timeout: float = 15.0) -> tuple[str, int]:
    return _curl(f"{serve_endpoint(node, port)}/v1/models", timeout=timeout)


def messages_curl(node: str, payload: dict, *, port: int = SERVE_PORT,
                  timeout: float = 120.0) -> tuple[str, int]:
    return _curl(f"{serve_endpoint(node, port)}/v1/messages",
                 method="POST", payload=payload, timeout=timeout)


# Payload builders — minimal but representative of what the agent loop sends.

def messages_payload_plain() -> dict:
    return {"model": SERVED_NAME, "max_tokens": 64,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}]}


def messages_payload_toolcall() -> dict:
    """A request that MUST elicit a tool call. The tool_use round-trip is the
    loop's lifeblood and the #1 OSS-serving risk (parser drift)."""
    return {
        "model": SERVED_NAME, "max_tokens": 256,
        "tools": [{
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "input_schema": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "City name"}},
                "required": ["city"],
            },
        }],
        "messages": [{
            "role": "user",
            "content": "Use the get_weather tool to check the weather in Paris. You must call the tool.",
        }],
    }


def messages_payload_cache_control() -> dict:
    """Carries the exact cache_control:ephemeral breakpoints the loop puts on
    system + tools + the newest user message (src/tools.py _cache_marked_*). The
    gate proves vLLM accepts them (it ignores/strips; prefix-caching is auto)."""
    return {
        "model": SERVED_NAME, "max_tokens": 64,
        "system": [{"type": "text", "text": "You are Aria's local brain.",
                    "cache_control": {"type": "ephemeral"}}],
        "tools": [{"name": "noop", "description": "no-op",
                   "input_schema": {"type": "object", "properties": {}},
                   "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "Reply with exactly: OK",
             "cache_control": {"type": "ephemeral"}}]}],
    }


# Pure response helpers + gate assertions (importable, unit-tested).

def _http_status(out: str) -> int | None:
    m = re.search(r"HTTP_STATUS=(\d+)", out)
    return int(m.group(1)) if m else None


def _parse_messages_json(out: str) -> dict:
    body = re.sub(r"\nHTTP_STATUS=\d+\s*$", "", out).strip()
    try:
        return json.loads(body)
    except Exception:
        m = re.search(r"\{.*\}", body, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return {}
    return {}


def _first_text_block(resp: dict) -> str:
    for blk in resp.get("content", []) or []:
        if isinstance(blk, dict) and blk.get("type") == "text":
            return str(blk.get("text", ""))
    return ""


def _has_tool_use_block(resp: dict, name: str | None = None) -> bool:
    for blk in resp.get("content", []) or []:
        if isinstance(blk, dict) and blk.get("type") == "tool_use":
            if name is None or blk.get("name") == name:
                return True
    return False


def assert_serve_models(out: str, rc: int) -> tuple[bool, str]:
    st = _http_status(out)
    if st != 200:
        return False, f"/v1/models HTTP {st} (server not up?)"
    if SERVED_NAME not in out:
        return False, f"served model {SERVED_NAME!r} not listed by /v1/models"
    return True, f"/v1/models lists {SERVED_NAME!r}"


def assert_serve_chat(out: str, rc: int) -> tuple[bool, str]:
    st = _http_status(out)
    if st != 200:
        return False, f"/v1/messages HTTP {st}"
    resp = _parse_messages_json(out)
    txt = _first_text_block(resp)
    if not txt.strip():
        return False, "no assistant text block in the /v1/messages reply"
    return True, f"assistant replied ({len(txt)} chars), stop_reason={resp.get('stop_reason')!r}"


def assert_serve_toolcall(out: str, rc: int) -> tuple[bool, str]:
    st = _http_status(out)
    if st != 200:
        return False, f"/v1/messages HTTP {st}"
    resp = _parse_messages_json(out)
    sr = resp.get("stop_reason")
    if not _has_tool_use_block(resp, "get_weather"):
        return False, (f"NO parseable tool_use(get_weather) block (stop_reason={sr!r}) "
                       "— tool-call parser drift, the loop cannot work")
    if sr != "tool_use":
        return False, f"tool_use block present but stop_reason={sr!r} (parser/stop drift)"
    return True, "tool_use(get_weather) present AND stop_reason=tool_use"


def assert_serve_cache_control(out: str, rc: int) -> tuple[bool, str]:
    st = _http_status(out)
    if st != 200:
        return False, f"server rejected the cache_control payload (HTTP {st})"
    resp = _parse_messages_json(out)
    if not (_first_text_block(resp) or _has_tool_use_block(resp)):
        return False, "HTTP 200 but empty reply to the cache_control payload"
    return True, "accepted cache_control:ephemeral blocks (HTTP 200, valid reply)"


def assert_serve_gpu(out: str, rc: int) -> tuple[bool, str]:
    if "GB10" not in out:
        return False, "no GB10 GPU in nvidia-smi"
    first = next((ln for ln in out.splitlines() if "GB10" in ln), "")
    parts = [p.strip() for p in first.split(",")]
    used = 0
    if len(parts) >= 2:
        mu = re.search(r"(\d+)", parts[1])
        used = int(mu.group(1)) if mu else 0
    if used < SERVE_GPU_MIN_USED_MIB:
        return False, f"only {used} MiB resident (<{SERVE_GPU_MIN_USED_MIB}); weights not loaded?"
    return True, f"GB10, {used} MiB resident (model loaded)"
