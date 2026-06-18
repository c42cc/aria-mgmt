"""src.build_hash — the ONE content hash of "the build" the trunk gate and proof
receipts are keyed to.

Ported from live_visuals_4/lib/build_hash.py and adapted to UCS. Two questions
are answered by the same primitive: "is the running process the source we think
it is?" (the deployed_trunk preflight gate) and "is this outcome proven on the
build it ran on?" (the live-outcome receipt). The key is a content hash of THE
BUILD — the behavior-determining app — NOT a boot sentinel that can be re-blessed.

THE BUILD, defined here ONCE so it cannot drift: the code (``src/**.py``), the
prompts that ARE behavior (``prompts/**.md`` — editing one changes how Aria
acts), the correctness specs the judge reads (``specs/**.md``), and the model
registry (``models.yaml``). EXCLUDED by construction are the things a proof is
ABOUT-but-not-OF: ``data/**`` and receipts (so a receipt can never change its own
key), ``tests/**`` and root ``*.md`` docs, and runtime/VCS noise (``.git``,
``.venv``, ``__pycache__``). Explicit inclusions, explicit exclusions — never an
implicit glob.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import subprocess

log = logging.getLogger(__name__)

_REPO = pathlib.Path(__file__).resolve().parent.parent

# The one pinned trunk. The running process must be on this branch; a process on
# any other branch is drift and refuses ready (it is not the trunk).
TRUNK = "main"

# THE BUILD's inputs — the ONE home. Each entry is (subdir, suffixes); a file
# under that subdir whose suffix matches is part of the build. Adding a
# behavioral surface means adding it HERE, deliberately, never silently.
BUILD_INPUTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("src", (".py",)),
    ("prompts", (".md",)),
    ("specs", (".md",)),
)

# Top-level files that are part of the build (a single file, not a dir).
_BUILD_FILES: tuple[str, ...] = ("models.yaml",)

# Path fragments never part of the build even under an included subdir
# (runtime/VCS noise; the build is source, not artifacts).
_EXCLUDE_FRAGMENTS = ("__pycache__", "/.", "/node_modules/", "/.venv/")

# The build hash this PROCESS booted on. Set ONCE at startup by stamp_boot() and
# never rewritten in-process — so an edit-after-boot is caught (live != boot),
# not laundered. The old running_code probe re-wrote its sentinel on every run,
# which is exactly the self-laundering this avoids.
_BOOT_HASH: str | None = None
_BOOT_SENTINEL = os.path.join("data", ".preflight_boot_sha")


def _iter_build_files(repo: pathlib.Path) -> list[pathlib.Path]:
    """Every build file, sorted by repo-relative POSIX path (stable, OS-independent)."""
    out: set[pathlib.Path] = set()
    for subdir, suffixes in BUILD_INPUTS:
        base = repo / subdir
        if not base.is_dir():
            continue
        for suffix in suffixes:
            for p in base.rglob(f"*{suffix}"):
                if not p.is_file():
                    continue
                rel = p.relative_to(repo).as_posix()
                if any(frag in f"/{rel}" for frag in _EXCLUDE_FRAGMENTS):
                    continue
                out.add(p)
    for name in _BUILD_FILES:
        p = repo / name
        if p.is_file():
            out.add(p)
    return sorted(out, key=lambda p: p.relative_to(repo).as_posix())


def compute_build_hash(repo: pathlib.Path | None = None) -> str:
    """SHA-256 over (repo-relative path, bytes) of every build file, in sorted
    path order. PURE + deterministic: the same source tree yields the same hash on
    any machine; a change to ANY behavioral file changes it; a change to a
    receipt, a doc, a test, or runtime data does NOT. Returns the full hex digest.
    """
    r = repo or _REPO
    h = hashlib.sha256()
    for p in _iter_build_files(r):
        rel = p.relative_to(r).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def stamp_boot() -> str:
    """Record the build hash for THIS process exactly once, at boot. Idempotent
    per process: the first call freezes ``_BOOT_HASH``; later calls return it
    unchanged. Only the boot sequence calls this — the probe never does — so a
    post-boot source edit can never re-bless itself. Loud on a write failure."""
    global _BOOT_HASH
    if _BOOT_HASH is None:
        _BOOT_HASH = compute_build_hash()
        os.makedirs(os.path.dirname(_BOOT_SENTINEL), exist_ok=True)
        with open(_BOOT_SENTINEL, "w") as f:
            f.write(_BOOT_HASH)
        log.info("build_hash: boot stamp recorded sha=%s", _BOOT_HASH[:12])
    return _BOOT_HASH


def boot_hash() -> str | None:
    """The hash frozen at boot, or None if this process never stamped (e.g. the
    one-shot preflight CLI, where 'changed since boot' is meaningless)."""
    return _BOOT_HASH


def _git(args: list[str], repo: pathlib.Path | None = None) -> str:
    """Run a git command in the repo and return stdout. Raises (loudly) if git is
    missing or the repo is unreadable — a broken VCS is a real fault, not '' ."""
    r = repo or _REPO
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=str(r), check=False
    ).stdout.strip()


def current_branch(repo: pathlib.Path | None = None) -> str:
    return _git(["rev-parse", "--abbrev-ref", "HEAD"], repo)


def build_tree_dirty(repo: pathlib.Path | None = None) -> str:
    """Porcelain git status limited to the build inputs; '' means clean. Scoped to
    build paths so unrelated untracked files (notes, scratch) don't trip the gate."""
    paths = [subdir for subdir, _ in BUILD_INPUTS] + list(_BUILD_FILES)
    return _git(["status", "--porcelain", "--", *paths], repo)
