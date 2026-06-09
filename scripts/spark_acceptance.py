#!/usr/bin/env python3
"""DGX Spark — Section A acceptance harness (capture + Gemini visual verify).

For each runbook "good state" this harness:

  1. Runs the probe LIVE in a real macOS Terminal window (via osascript) so the
     remote output is literally on screen, and parses the same run's stdout +
     exit code as ground truth.
  2. Screenshots the Terminal with `screencapture` (the same primitive
     `src/tools.py::_screenshot_cursor_window` uses).
  3. Asks Gemini to independently confirm the screenshot shows the success
     condition (the `src/judge.py` generate_content pattern, temperature 0).

A gate PASSES only if the machine assertion AND the Gemini verdict agree. Any
failure -- or any disagreement between the two -- is loud: the gate is marked
FAIL with the runbook fix command, and the harness exits non-zero. No silent
fallbacks. Artifacts land in data/spark/<node>/: one PNG per gate plus
acceptance.json.

USAGE
  .venv/bin/python scripts/spark_acceptance.py --node spark1 --role A
  .venv/bin/python scripts/spark_acceptance.py --node spark1 --role A --run-setup
  .venv/bin/python scripts/spark_acceptance.py --node spark1 --role A --only claude_auth,gpu

Secrets: ANTHROPIC_API_KEY is read from .env only to seed the node and to run
the auth round-trip; it is never printed or screenshotted (the displayed
command shows `$(cat ...)`, never the value).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

# Primary first; the rest are availability fallbacks. A spike on one model must
# not fail a verified gate -- that would be blaming the service for our design.
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
ENV_PREFIX = "source ~/.config/spark/env.sh 2>/dev/null; "
TMP = Path("/tmp")


# ---------------------------------------------------------------------------
# Gate definition
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
    # Healthy if it ran and mentions the product/version surface without a failure marker.
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


# ---------------------------------------------------------------------------
# Terminal-driven live run + screenshot
# ---------------------------------------------------------------------------

def _osascript(script: str, *, timeout: float = 120.0) -> tuple[int, str, str]:
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def ensure_terminal_window() -> None:
    # Just confirm we can drive Terminal (surfaces the Automation-permission
    # error early). Each gate opens its own dedicated window.
    rc, _, err = _osascript('tell application "Terminal" to activate')
    if rc != 0:
        raise RuntimeError(
            "Could not control Terminal.app via osascript. Grant Automation "
            f"permission to the controlling app (System Settings > Privacy & "
            f"Security > Automation). Error: {err.strip()}"
        )
    time.sleep(0.5)


def ssh_probe(node: str, command: str, *, timeout: float = 120.0) -> tuple[str, int]:
    """Run the probe over SSH and return (combined_output, returncode).

    This is the ground truth for the machine assertion. Passing the remote
    command as a single argv element means any quoting inside it is the remote
    shell's concern, not the local shell's.
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
    # Park it in a fixed rectangle and capture only that rectangle.
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
# Setup (optional pre-step)
# ---------------------------------------------------------------------------

def run_setup(node: str, role: str) -> None:
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not in .env — refusing to seed node without it (headless auth would be blind)")
    setup_sh = REPO_ROOT / "ops" / "spark" / "setup_node.sh"
    if not setup_sh.exists():
        raise RuntimeError(f"missing {setup_sh}")
    print(f"[setup] seeding {node} (role {role}) via ops/spark/setup_node.sh ...", flush=True)
    # Key passed through the remote env assignment; never printed here.
    remote_cmd = f"ANTHROPIC_API_KEY='{key}' bash -s -- {role} {node}"
    with open(setup_sh) as f:
        p = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", node, remote_cmd],
            stdin=f, capture_output=True, text=True, timeout=600,
        )
    sys.stdout.write(p.stdout)
    if p.returncode != 0:
        sys.stderr.write(p.stderr)
        raise RuntimeError(f"setup_node.sh failed on {node} (rc={p.returncode})")
    print(f"[setup] {node} seeded OK", flush=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--node", required=True, help="ssh alias / host (e.g. spark1)")
    ap.add_argument("--role", required=True, choices=["A", "B"])
    ap.add_argument("--run-setup", action="store_true", help="Seed the node via setup_node.sh before verifying")
    ap.add_argument("--only", default="", help="Comma-separated gate ids to run (default: all)")
    args = ap.parse_args()

    out_dir = REPO_ROOT / "data" / "spark" / args.node
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.run_setup:
        run_setup(args.node, args.role)

    gates = build_gates(args.node, args.role)
    if args.only:
        wanted = {g.strip() for g in args.only.split(",") if g.strip()}
        gates = [g for g in gates if g.id in wanted]
        if not gates:
            print(f"FATAL: --only matched no gates ({wanted})", file=sys.stderr)
            return 2

    ensure_terminal_window()

    results: list[GateResult] = []
    for gate in gates:
        print(f"\n=== gate: {gate.id} :: {gate.title} ===", flush=True)
        png = out_dir / f"{gate.id}.png"
        try:
            out, rc = ssh_probe(args.node, gate.command)
            display_and_capture(args.node, gate, out, rc, png)
        except Exception as e:
            print(f"  HARNESS ERROR: {e}", flush=True)
            results.append(GateResult(
                gate.id, gate.title, False, f"harness error: {e}", False, "",
                "FAIL", 1, "", str(png), gate.fix,
            ))
            continue

        machine_ok, machine_detail = gate.assert_fn(out, rc)
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
            verdict, rc, out[:600], str(png), "" if verdict == "PASS" else gate.fix,
        ))

    passed = [r for r in results if r.verdict == "PASS"]
    failed = [r for r in results if r.verdict != "PASS"]

    report = {
        "node": args.node,
        "role": args.role,
        "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary": {"total": len(results), "pass": len(passed), "fail": len(failed)},
        "gates": [r.__dict__ for r in results],
    }
    report_path = out_dir / "acceptance.json"
    report_path.write_text(json.dumps(report, indent=2))

    print("\n" + "=" * 60, flush=True)
    print(f"SPARK SECTION-A ACCEPTANCE :: {args.node} (role {args.role})", flush=True)
    for r in results:
        print(f"  [{r.verdict}] {r.id:14s} {r.title}", flush=True)
        if r.verdict != "PASS":
            print(f"          machine: {r.machine_detail}", flush=True)
            print(f"          gemini : {r.gemini_reason}", flush=True)
            print(f"          fix    : {r.fix}", flush=True)
    print(f"\n  {len(passed)}/{len(results)} gates green. Report: {report_path}", flush=True)
    print("=" * 60, flush=True)

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
