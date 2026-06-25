"""The Floor must be honest: ABSENT until a real NAS mount, never faked by local disk."""

from __future__ import annotations

import pytest

from src import floor


def test_unset_floor_is_absent_and_loud():
    s = floor.status(root="")
    assert s.present is False
    assert s.redundant is False
    assert "ABSENT" in s.detail


def test_nonexistent_floor_is_absent():
    s = floor.status(root="/definitely/not/a/real/mount/xyzzy")
    assert s.present is False
    assert "does not exist" in s.detail


def test_local_directory_never_masquerades_as_floor(tmp_path):
    # A plain directory exists but is NOT a mountpoint -> must be refused as the Floor.
    s = floor.status(root=str(tmp_path))
    assert s.present is False
    assert "NOT THE FLOOR" in s.detail


def test_require_raises_loudly_when_absent():
    # require() reads config (FLOOR_ROOT unset in test env) -> must halt, not fall back.
    with pytest.raises(floor.FloorAbsent):
        floor.require("a path of record")
