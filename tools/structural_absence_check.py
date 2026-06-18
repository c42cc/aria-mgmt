#!/usr/bin/env python3
"""tools/structural_absence_check.py — assert the enforce-by-absence ledger.

Ported discipline from live_visuals_4. Reads configs/structural_absences.json and
proves, loudly, that:

  - `reference_must_not_exist`: a forbidden substring (a deleted mechanism)
    appears in ZERO files under its scope. A resurrected mechanism reds the gate.
  - `single_reader`: a single-homed symbol is read ONLY in its allowed files. A
    new reader (a second home) reds the gate.
  - `awaiting_collapse`: tracked debt — printed as a visible warning, never
    silently accepted, but not (yet) gating.

Exit 0 if every gating assertion holds; 1 with the offending files otherwise.
"""

from __future__ import annotations

import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LEDGER = os.path.join(_REPO, "configs", "structural_absences.json")


def _py_files(rel_dir: str) -> list[str]:
    base = os.path.join(_REPO, rel_dir)
    out: list[str] = []
    for root, _dirs, files in os.walk(base):
        if "__pycache__" in root:
            continue
        for name in files:
            if name.endswith(".py"):
                out.append(os.path.join(root, name))
    return out


def _rel(path: str) -> str:
    return os.path.relpath(path, _REPO)


def check_reference_absence(entry: dict) -> list[str]:
    sub = entry["substring"]
    hits: list[str] = []
    for rel_dir in entry.get("where", ["src"]):
        for path in _py_files(rel_dir):
            with open(path, encoding="utf-8", errors="replace") as f:
                if sub in f.read():
                    hits.append(_rel(path))
    return hits


def check_single_reader(entry: dict) -> list[str]:
    symbol = entry["symbol"]
    allowed = set(entry["allowed_files"])
    offenders: list[str] = []
    for path in _py_files("src"):
        rel = _rel(path)
        if rel in allowed:
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            if symbol in f.read():
                offenders.append(rel)
    return offenders


def main() -> int:
    with open(_LEDGER) as f:
        ledger = json.load(f)

    violations: list[str] = []

    for entry in ledger.get("reference_must_not_exist", []):
        hits = check_reference_absence(entry)
        if hits:
            violations.append(
                f"FORBIDDEN reference {entry['substring']!r} resurrected in: {', '.join(hits)}\n"
                f"    why it is gone: {entry['why']}"
            )

    for entry in ledger.get("single_reader", []):
        offenders = check_single_reader(entry)
        if offenders:
            violations.append(
                f"SECOND HOME for {entry['symbol']!r} (allowed only in "
                f"{', '.join(entry['allowed_files'])}) found in: {', '.join(offenders)}\n"
                f"    why one home: {entry['why']}"
            )

    awaiting = ledger.get("awaiting_collapse", [])
    if awaiting:
        print("structural_absence_check: tracked debt awaiting collapse (visible, not silent):")
        for entry in awaiting:
            print(f"  - {entry['id']}: {entry.get('pattern','')} "
                  f"(~{entry.get('count_at_ledger','?')} sites in {entry.get('where')})")

    if violations:
        print("\nstructural_absence_check: RED — the ledger was broken:\n")
        for v in violations:
            print(f"  * {v}")
        return 1

    print("structural_absence_check: GREEN — every absence/single-home assertion holds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
