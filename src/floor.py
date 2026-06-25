"""The Floor — the durable, shared, redundant state-of-record plane.

This is the storage-layout contract (§1.1) made into one honest primitive. The
Floor is where state of record lives: it must be SHARED (every plane mounts it)
and REDUNDANT (RAID + backups). Today there is no NAS, so the Floor is ABSENT —
and absence is a first-class, LOUD state here, never papered over. No code may
let a compute-local disk masquerade as the Floor (that is the silent lie this
module exists to prevent).

The seam: set FLOOR_ROOT to the NAS mount and the Floor flips to present with
zero code change. `os.path.ismount` is the discriminator — a plain directory is
NOT the Floor; only a real mountpoint is.

Halt-don't-heal: `require()` raises a loud FloorAbsent (never a fallback) for any
path that genuinely needs the durable plane.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .config import config


class FloorAbsent(RuntimeError):
    """The durable Floor is not available. Loud, typed — never a silent fallback."""


@dataclass(frozen=True)
class FloorStatus:
    present: bool
    redundant: bool | None  # None = present but redundancy not verifiable from here
    root: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.present


def status(root: str | None = None) -> FloorStatus:
    """The one honest read of the Floor. ABSENT until the NAS mounts at FLOOR_ROOT.

    `root` defaults to config.floor_root; pass an explicit path to probe a
    candidate mount (used by the doctor and tests).
    """
    root = config.floor_root if root is None else root.strip().rstrip("/")
    if not root:
        return FloorStatus(
            present=False,
            redundant=False,
            root="",
            detail=(
                "ABSENT — FLOOR_ROOT is unset; state of record has no shared, redundant "
                "home yet. Set FLOOR_ROOT to the NAS mount when it arrives."
            ),
        )
    if not os.path.exists(root):
        return FloorStatus(
            present=False,
            redundant=False,
            root=root,
            detail=f"ABSENT — FLOOR_ROOT={root!r} does not exist; mount the NAS there.",
        )
    if not os.path.ismount(root):
        # A plain directory is NOT the Floor. Refuse to let local disk pretend.
        return FloorStatus(
            present=False,
            redundant=False,
            root=root,
            detail=(
                f"NOT THE FLOOR — {root!r} exists but is not a mountpoint; that is "
                "compute-local disk, not the shared/redundant Floor. Mount the NAS at "
                "FLOOR_ROOT (a local directory must never masquerade as the Floor)."
            ),
        )
    return FloorStatus(
        present=True,
        redundant=None,  # honest: RAID/backup health is the NAS's to report, not guessable here
        root=root,
        detail=(
            f"present at {root} (mountpoint). Redundancy/backups are the NAS's to attest — "
            "confirm RAID + offsite backup on the appliance."
        ),
    )


def require(purpose: str) -> str:
    """Return the Floor root for a path that MUST have it, or raise loudly.

    No fallback to local disk — a missing Floor halts with the one fix.
    """
    s = status()
    if not s.present:
        raise FloorAbsent(f"{purpose} needs the durable Floor, but it is {s.detail}")
    return s.root
