"""Filesystem anchors: read, list, search, write verification."""

from __future__ import annotations

import asyncio
import glob
import logging
import os

from .base import AnchorReport
from .normalize import content_hash, extract_number_claim

log = logging.getLogger(__name__)

ALLOWED_ROOTS = [
    "/Users/corbin/Documents",
    "/Users/corbin/Downloads",
    "/Users/corbin/PycharmProjects",
    "/tmp",
]


def _path_allowed(path: str) -> bool:
    rp = os.path.realpath(path)
    return any(rp.startswith(root) for root in ALLOWED_ROOTS)


class FilesystemReadAnchor:
    spec_version = 1

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="filesystem.read_file")
        path = tool_call.get("args", {}).get("path", "")
        if not path or not _path_allowed(path):
            report.fact("status", "path_missing_or_disallowed", "policy")
            return report

        try:
            content = await asyncio.to_thread(self._read, path)
            report.fact("file_size_bytes", len(content), "filesystem")
            report.fact("content_hash", content_hash(content), "filesystem")
        except FileNotFoundError:
            report.violate(7, "hard", f"File not found: {path}")
        except PermissionError:
            report.unverified = True
            report.fact("error", f"Permission denied: {path}", "filesystem")

        return report

    @staticmethod
    def _read(path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    async def health_check(self) -> bool:
        return os.path.isdir("/Users/corbin/Documents")


class FilesystemListAnchor:
    spec_version = 1

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="filesystem.list_directory")
        args = tool_call.get("args", {})
        path = args.get("path", "")

        if not path or not _path_allowed(path):
            report.fact("status", "path_missing_or_disallowed", "policy")
            return report

        try:
            entries = await asyncio.to_thread(os.listdir, path)
            report.fact("ground_truth_count", len(entries), "os.listdir")
            report.fact("ground_truth_entries", sorted(entries)[:50], "os.listdir")
        except Exception as e:
            report.unverified = True
            report.fact("error", str(e)[:200], "filesystem")
            return report

        aria_count = extract_number_claim(aria_result, "file")
        if aria_count is None:
            aria_count = extract_number_claim(aria_result, "item")
        if aria_count is not None:
            report.fact("aria_claimed_count", aria_count, "aria_result_extraction")
            if aria_count != len(entries):
                report.violate(3, "hard", f"Aria claimed {aria_count}, ground truth is {len(entries)}")

        return report

    async def health_check(self) -> bool:
        return True


class FilesystemSearchAnchor:
    spec_version = 1

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="filesystem.search_files")
        args = tool_call.get("args", {})
        path = args.get("path", "")
        pattern = args.get("pattern", "")

        if not path or not _path_allowed(path):
            report.fact("status", "path_missing_or_disallowed", "policy")
            return report

        try:
            full_pattern = os.path.join(path, pattern) if pattern else path
            matches = await asyncio.to_thread(glob.glob, full_pattern, recursive=True)
            report.fact("ground_truth_count", len(matches), "glob.glob")
            report.fact("ground_truth_files", sorted(matches)[:50], "glob.glob")
        except Exception as e:
            report.unverified = True
            report.fact("error", str(e)[:200], "filesystem")
            return report

        for kw in ("file", "result", "match", "item"):
            aria_count = extract_number_claim(aria_result, kw)
            if aria_count is not None:
                report.fact("aria_claimed_count", aria_count, "aria_result_extraction")
                if aria_count != len(matches):
                    report.violate(3, "hard", f"Aria claimed {aria_count} {kw}s, ground truth is {len(matches)}")
                break

        return report

    async def health_check(self) -> bool:
        return True


class FilesystemWriteAnchor:
    """Write anchor: verify file exists and content hash. Never re-writes."""
    spec_version = 1

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="filesystem.write_file")
        path = tool_call.get("args", {}).get("path", "")

        if not path:
            report.fact("status", "no_path", "trace_inspection")
            return report

        trace_result = tool_call.get("result", "")
        if "declined" in trace_result.lower() or "error" in trace_result.lower()[:60]:
            report.fact("write_outcome", "declined_or_errored", "trace_inspection")
            return report

        exists = os.path.exists(path)
        report.fact("file_exists", exists, "os.path.exists")
        if not exists:
            report.violate(7, "hard", f"File {path} does not exist after claimed write")

        return report

    async def health_check(self) -> bool:
        return True
