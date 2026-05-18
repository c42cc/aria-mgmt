#!/usr/bin/env python3
"""End-to-end test of Aria's remote-pilot capability.

Simulates the user-away-from-keyboard experience as closely as possible
without Discord voice. The script:

  1. Spins up the real cursor_external observer on the configured port.
  2. Creates a temp skeleton project under /tmp/aria_e2e_<ts>/.
  3. Opens that project in a NEW Cursor IDE window (open -a Cursor).
  4. Adds the project to PROJECT_REGISTRY at runtime so the tools can
     address it by short name.
  5. Drives the pilot loop via the real tool entry points:
        list_cursor_windows  -> verify the new window appears
        read_cursor_window   -> verify on-disk transcript reader works
        send_to_cursor_chat  -> paste a real instruction into the chat
        (wait for Cursor's agent to act)
        read_cursor_window   -> see what the agent did
  6. The observer's pager callback records every event Aria would have
     seen — what she'd narrate on voice, what she'd DM if the user were
     away. The script prints those records so a human can verify the
     'experience' is right.

This is the closest practical analogue to the real flow: the user is on
their phone, Aria is the entire body on the workstation, and we want to
prove Aria can both observe and act.

USAGE:
  .venv/bin/python scripts/e2e_remote_pilot.py
  .venv/bin/python scripts/e2e_remote_pilot.py --keep-project   # don't rm /tmp/aria_e2e_...
  .venv/bin/python scripts/e2e_remote_pilot.py --skip-cursor-open  # use a project already open

The script does NOT require Discord, Gemini, or any LLM API to run. It
exercises the local tool surface and the on-disk observation layer end
to end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)


def snap(label: str, out_dir: str) -> str:
    """Take a full-screen screenshot for diagnostics. Returns saved path."""
    os.makedirs(out_dir, exist_ok=True)
    ts = int(time.time() * 1000)
    safe = "".join(c for c in label if c.isalnum() or c in "._-")[:50]
    path = os.path.join(out_dir, f"{ts}-{safe}.png")
    subprocess.run(["screencapture", "-x", path], check=False)
    if os.path.exists(path):
        print(f"  [snapshot] {label}: {path}  ({os.path.getsize(path)} bytes)", flush=True)
    return path

from src import tools as tools_module
from src.cursor_bridge import CursorBridge
from src.cursor_external import CursorEvent, CursorExternalObserver


def step(num: int, label: str) -> None:
    print(f"\n=== STEP {num}: {label}", flush=True)


def make_skeleton_project(root: str) -> None:
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "README.md"), "w") as f:
        f.write(
            "# Aria E2E Test Project\n\n"
            "Temporary project for verifying Aria's Cursor remote-pilot loop.\n"
        )
    with open(os.path.join(root, "calculator.py"), "w") as f:
        f.write(
            '"""Tiny calculator skeleton — Aria is going to extend this."""\n\n'
            "from __future__ import annotations\n\n\n"
            "def add(a: float, b: float) -> float:\n"
            "    return a + b\n"
        )
    print(f"  wrote skeleton at {root}")


def cleanup_project(root: str) -> None:
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
        print(f"  removed {root}")


async def run_e2e(
    *,
    keep_project: bool,
    skip_cursor_open: bool,
    project_root: str | None,
) -> int:
    paged: list[CursorEvent] = []

    async def fake_pager(evt: CursorEvent) -> None:
        paged.append(evt)
        print(
            f"  >> ARIA WOULD PAGE: severity={evt.severity} brief={evt.brief!r}",
            flush=True,
        )
        if evt.transcript_snippet:
            last = evt.transcript_snippet[-1]
            print(
                f"     last transcript turn ({last.get('role')}): "
                f"{last.get('text', '')[:140]!r}",
                flush=True,
            )

    tools_module.init_tools(cursor_bridge=CursorBridge())

    if project_root is None:
        project_root = tempfile.mkdtemp(prefix="aria_e2e_")
    proj_name = os.path.basename(project_root.rstrip("/")) or "aria_e2e"
    tools_module.PROJECT_REGISTRY[proj_name] = project_root
    print(f"\nregistered project: {proj_name} -> {project_root}", flush=True)

    obs = CursorExternalObserver(
        pager_callback=fake_pager,
        registry_provider=lambda: dict(tools_module.PROJECT_REGISTRY),
    )
    try:
        await obs.start()
    except OSError as exc:
        print(f"FATAL: could not start observer: {exc}", flush=True)
        print(
            "If port 8731 is already in use, set UCS_CURSOR_EVENT_PORT to "
            "a free port or kill the other listener.",
            flush=True,
        )
        return 1
    print(f"observer running on {obs.url}", flush=True)

    snap_dir = os.path.join(project_root, "_aria_snapshots")

    try:
        if not skip_cursor_open:
            step(1, f"creating skeleton project at {project_root}")
            make_skeleton_project(project_root)

            step(2, "opening project in a NEW Cursor window (open -a Cursor)")
            proc = await asyncio.create_subprocess_exec(
                "open", "-a", "Cursor", project_root
            )
            await proc.wait()
            print(f"  open exit={proc.returncode}", flush=True)
            await asyncio.sleep(6.0)
            snap("02-after-open", snap_dir)
        else:
            step(1, "using existing Cursor window (skip-cursor-open)")
            if not os.path.isdir(project_root):
                print(
                    f"  WARN: {project_root} does not exist on disk; read tools "
                    f"will return no turns until Cursor creates the project folder.",
                    flush=True,
                )

        step(3, "list_cursor_windows — verify Aria can see the new window")
        r = await tools_module.handle_tool_call("list_cursor_windows", {})
        parsed = json.loads(r)
        print(f"  result: {json.dumps(parsed, indent=2)[:1200]}", flush=True)

        windows = parsed.get("windows", [])
        target_window = next(
            (w for w in windows if proj_name in (w.get("title") or "")), None
        )
        if target_window is None:
            print(
                f"  WARN: no Cursor window title contains {proj_name!r}. "
                f"Proceeding anyway — tools will try to find by substring.",
                flush=True,
            )

        step(4, "focus_cursor_window — bring it to the front")
        r = await tools_module.handle_tool_call(
            "focus_cursor_window", {"project": proj_name}
        )
        print(f"  result: {r}", flush=True)
        await asyncio.sleep(1.5)
        snap("04-after-focus", snap_dir)

        step(5, "read_cursor_window — Aria reads any existing state")
        r = await tools_module.handle_tool_call(
            "read_cursor_window", {"project": proj_name, "n_turns": 3}
        )
        parsed = json.loads(r)
        print(
            f"  project={parsed.get('project')} cwd={parsed.get('cwd')} "
            f"turns_returned={parsed.get('turns_returned')}",
            flush=True,
        )
        for t in parsed.get("turns", [])[:3]:
            print(
                f"    [{t.get('role')}] {(t.get('text') or '')[:120]!r} "
                f"tool_use={t.get('has_tool_use')}",
                flush=True,
            )

        step(6, "send_to_cursor_chat — Aria pastes a real task into the chat")
        instruction = os.environ.get("ARIA_E2E_INSTRUCTION") or (
            "Please extend calculator.py with the following:\n"
            "1. A subtract(a, b) function that returns a - b.\n"
            "2. A multiply(a, b) function that returns a * b.\n"
            "3. Type hints and one-line docstrings on every function.\n"
            "After editing, write a small Markdown summary of the changes "
            "as report.md in the same directory."
        )
        print(f"  sending instruction ({len(instruction)} chars):", flush=True)
        print(f"  --- BEGIN INSTRUCTION ---\n{instruction}\n  --- END INSTRUCTION ---", flush=True)
        r = await tools_module.handle_tool_call(
            "send_to_cursor_chat",
            {"project": proj_name, "message": instruction},
        )
        print(f"  result: {r}", flush=True)
        await asyncio.sleep(1.5)
        snap("06-after-send", snap_dir)

        step(7, "verifying the send landed via read_cursor_window (Aria's canonical pattern)")
        landed_via_transcript = False
        for poll in range(8):
            await asyncio.sleep(5)
            r = await tools_module.handle_tool_call(
                "read_cursor_window", {"project": proj_name, "n_turns": 4}
            )
            p = json.loads(r)
            turns = p.get("turns") or []
            if turns:
                latest = turns[-1]
                role = latest.get("role")
                has_tool = latest.get("has_tool_use")
                text = (latest.get("text") or "")[:140]
                print(
                    f"  poll {poll+1}/8 t={5*(poll+1)}s: turns={len(turns)} "
                    f"latest=[{role} tool_use={has_tool}] {text!r}",
                    flush=True,
                )
                last_user_turn = [t for t in turns if t.get("role") == "user"]
                if last_user_turn:
                    landed_via_transcript = True
                    print("  -> send LANDED (new user turn in transcript). "
                          "Will now wait for stop hook or agent settle...",
                          flush=True)
                    break
            else:
                print(f"  poll {poll+1}/8 t={5*(poll+1)}s: still 0 turns", flush=True)

        if not landed_via_transcript:
            print("  WARN: send did not appear in transcript after 40s. "
                  "In production Aria would retry once and otherwise tell the user.",
                  flush=True)

        step(7.5, "monitoring hooks/files for 60 more seconds")
        deadline = time.time() + 60
        last_seen = 0
        last_files: set[str] = set(os.listdir(project_root)) if os.path.isdir(project_root) else set()
        while time.time() < deadline:
            await asyncio.sleep(5)
            now_files = set(os.listdir(project_root)) if os.path.isdir(project_root) else set()
            new_files = now_files - last_files
            if new_files:
                print(f"  + new files in project: {sorted(new_files)}", flush=True)
                last_files = now_files
            if len(paged) > last_seen:
                print(
                    f"  + {len(paged) - last_seen} new event(s) "
                    f"(total paged so far: {len(paged)})",
                    flush=True,
                )
                last_seen = len(paged)
            stats = obs.stats
            stop_events = [e for e in paged if e.hook_type == "stop"]
            if stop_events:
                print(
                    "  STOP hook fired — Cursor agent finished its turn. "
                    "Wrapping up monitor loop.",
                    flush=True,
                )
                break

        step(8, "read_cursor_window again — Aria reads what happened")
        r = await tools_module.handle_tool_call(
            "read_cursor_window", {"project": proj_name, "n_turns": 6}
        )
        parsed = json.loads(r)
        print(
            f"  turns_returned={parsed.get('turns_returned')} "
            f"recent_plans={len(parsed.get('recent_plans') or [])}",
            flush=True,
        )
        for t in parsed.get("turns", [])[-5:]:
            text = (t.get("text") or "").replace("\n", " ")
            print(
                f"    [{t.get('role')}] {text[:160]!r} tool_use={t.get('has_tool_use')}",
                flush=True,
            )

        step(9, "verify files on disk")
        if os.path.isdir(project_root):
            files = sorted(os.listdir(project_root))
            print(f"  files in project: {files}", flush=True)
            calc = os.path.join(project_root, "calculator.py")
            if os.path.exists(calc):
                with open(calc) as f:
                    body = f.read()
                has_subtract = "def subtract" in body
                has_multiply = "def multiply" in body
                print(
                    f"  calculator.py: subtract={has_subtract} multiply={has_multiply} "
                    f"({len(body)} bytes)",
                    flush=True,
                )
            report = os.path.join(project_root, "report.md")
            if os.path.exists(report):
                with open(report) as f:
                    body = f.read()
                print(f"  report.md: {len(body)} bytes:", flush=True)
                print(f"  --- BEGIN report.md ---\n{body[:600]}\n  --- END ---", flush=True)
            else:
                print("  report.md NOT created", flush=True)

        step(10, "summary of what Aria would have done on voice")
        print(f"  observer.stats: {obs.stats}", flush=True)
        print(f"  total pages: {len(paged)}", flush=True)
        for i, evt in enumerate(paged, 1):
            print(
                f"  [{i}] severity={evt.severity} hook={evt.hook_type} "
                f"project={evt.project} brief={evt.brief!r}",
                flush=True,
            )
        return 0

    finally:
        await obs.stop()
        if not keep_project and project_root and project_root.startswith("/tmp/aria_e2e_"):
            cleanup_project(project_root)
        elif project_root:
            print(
                f"\n(project kept at {project_root}; close the Cursor window when done)",
                flush=True,
            )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keep-project", action="store_true")
    ap.add_argument("--skip-cursor-open", action="store_true")
    ap.add_argument(
        "--project-root",
        help="Use this project root instead of creating one under /tmp",
    )
    args = ap.parse_args()
    return asyncio.run(
        run_e2e(
            keep_project=args.keep_project,
            skip_cursor_open=args.skip_cursor_open,
            project_root=args.project_root,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
