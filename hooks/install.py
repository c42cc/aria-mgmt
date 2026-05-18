#!/usr/bin/env python3
"""Install Aria's Cursor hooks into ~/.cursor/hooks.json.

Reads the existing hooks file (following symlinks — yours is symlinked to
live_visuals_3/hooks/hooks.json), merges in the Aria forwarder entries, and
writes it back atomically. Idempotent: re-running is safe; entries are
keyed by command path so duplicates are avoided.

Run once after pulling new code, or whenever you want to rewire which
events Aria sees. Re-running will not remove unrelated entries.

Usage:
    python3 hooks/install.py            # merge in
    python3 hooks/install.py --uninstall # remove Aria entries
    python3 hooks/install.py --dry-run   # print the result, don't write
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORWARDER = os.path.join(REPO_ROOT, "hooks", "cursor-event.py")
HOOKS_FILE = os.path.expanduser("~/.cursor/hooks.json")

ARIA_TAG = "aria-cursor-event"


def aria_entry(hook_type: str, *, matcher: str | None = None) -> dict:
    """One hook entry; tagged so we can find/remove it on re-install."""
    cmd = f"{FORWARDER} {hook_type}"
    entry: dict = {"command": cmd, "_tag": ARIA_TAG}
    if matcher:
        entry["matcher"] = matcher
    return entry


def desired_entries() -> dict[str, list[dict]]:
    """The Aria-specific hook entries we want present.

    Mapping of hook event -> list of entries to add under that event.

    Events we listen to:
      - stop:                fires when an agent loop ends
      - subagentStop:        fires when a subagent (Task tool) finishes
      - sessionEnd:          fires when a composer conversation closes
      - postToolUse:         only for CreatePlan and Task matchers
      - afterAgentResponse:  light narration of every assistant text turn
    """
    return {
        "stop": [aria_entry("stop")],
        "subagentStop": [aria_entry("subagentStop")],
        "sessionEnd": [aria_entry("sessionEnd")],
        "postToolUse": [
            aria_entry("postToolUse", matcher="CreatePlan"),
            aria_entry("postToolUse", matcher="Task"),
        ],
        "afterAgentResponse": [aria_entry("afterAgentResponse")],
    }


def load_hooks() -> dict:
    """Load the existing hooks.json, returning the parsed structure.

    We follow the symlink chain because os.path.exists already does that.
    Missing file -> minimal skeleton. Invalid JSON -> abort (we are NOT
    going to silently overwrite a corrupt-looking file the user might be
    trying to debug).
    """
    if not os.path.exists(HOOKS_FILE):
        return {"version": 1, "hooks": {}}
    with open(HOOKS_FILE) as f:
        data = json.load(f)
    if not isinstance(data, dict) or "hooks" not in data:
        raise SystemExit(
            f"{HOOKS_FILE} does not look like a Cursor hooks file (no top-level 'hooks' key). "
            f"Refusing to overwrite. Fix or remove it first."
        )
    return data


def strip_aria(hooks: dict) -> dict:
    """Remove any prior Aria-tagged entries. Preserves all other entries."""
    hooks_section = hooks.setdefault("hooks", {})
    for event, entries in list(hooks_section.items()):
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not (isinstance(e, dict) and e.get("_tag") == ARIA_TAG)]
        if kept:
            hooks_section[event] = kept
        else:
            del hooks_section[event]
    return hooks


def merge_aria(hooks: dict) -> dict:
    """Add the desired Aria entries to the hooks structure (after stripping)."""
    hooks = strip_aria(hooks)
    hooks_section = hooks.setdefault("hooks", {})
    for event, entries in desired_entries().items():
        existing = hooks_section.setdefault(event, [])
        if not isinstance(existing, list):
            raise SystemExit(
                f"{HOOKS_FILE}: hooks.{event} is not a list (got {type(existing).__name__}). "
                f"Refusing to merge."
            )
        for e in entries:
            existing.append(e)
    return hooks


def atomic_write(path: str, data: dict) -> None:
    """Write JSON to `path` via a temp file + rename; back up the original."""
    real = os.path.realpath(path)
    backup = real + f".bak.{int(time.time())}"
    if os.path.exists(real):
        shutil.copy2(real, backup)
        print(f"backed up existing hooks file -> {backup}")
    tmp = real + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, real)
    print(f"wrote {real}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--uninstall", action="store_true", help="Remove Aria hook entries")
    ap.add_argument("--dry-run", action="store_true", help="Print the result, do not write")
    args = ap.parse_args()

    if not os.path.exists(FORWARDER):
        raise SystemExit(f"forwarder not found at {FORWARDER}")
    if not os.access(FORWARDER, os.X_OK):
        raise SystemExit(f"forwarder is not executable: chmod +x {FORWARDER}")

    hooks = load_hooks()
    if args.uninstall:
        hooks = strip_aria(hooks)
        print("Aria entries removed.")
    else:
        hooks = merge_aria(hooks)
        print("Aria entries merged.")

    rendered = json.dumps(hooks, indent=2)
    if args.dry_run:
        print()
        print(rendered)
        return 0

    atomic_write(HOOKS_FILE, hooks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
