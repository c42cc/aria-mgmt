"""Plan citation anchor (P1 extension for F14).

Plans produced by `plan_with_claude` historically invented file paths and
internal symbols that did not exist in workspace context (sessions 8, 13,
14: `src/spotify_handler.py`, `_do_with_claude_legacy` at line 347-460, etc.).

This anchor scans the plan output for file-path citations and asserts each
exists on disk under one of the allowed workspace roots. Citations
explicitly framed as new ("create", "new file", "(to be created)", "(new)")
are exempt — the anchor only flags references that the plan treats as
present-tense facts.

The anchor is keyed on the synthetic `plan_with_claude` tool entry that
`judge._run_anchors` appends when the session's `tool_name` is
`plan_with_claude`. There is no upstream API call — the anchor's source of
truth is the local filesystem, so it never returns `unverified`.
"""

from __future__ import annotations

import logging
import os
import re

from .base import AnchorReport

log = logging.getLogger(__name__)


ALLOWED_ROOTS = [
    "/Users/corbin/PycharmProjects/agi_env_v1/ucs2",
    "/Users/corbin/PycharmProjects",
    "/Users/corbin/Documents",
    "/Users/corbin/Downloads",
]

# Match any relative path token with a recognised source-code extension and
# at least one directory segment. We deliberately accept arbitrary top-level
# directories (src/, tests/, ops/, lib/, utils/, …) so the anchor catches
# F14 cases like the invented `lib/spotify_sdk.py` from session 8.
# Constraints to avoid false positives:
#   * must contain at least one "/"
#   * must end in one of the listed extensions
#   * leading char must be a directory-segment-safe letter or underscore
#     (rules out URLs like "https://…", regex like ".*\.py")
_PATH_RE = re.compile(
    r"(?:(?<=\s)|(?<=^)|(?<=`)|(?<=\(\s)|(?<=\())"
    r"([A-Za-z_][A-Za-z0-9_\-]*/"
    r"[A-Za-z0-9_./\-]+\.(?:py|ts|tsx|js|jsx|md|yml|yaml|toml|sh|sql))",
)

# Phrases in a small window around the citation that mark it as a *new*
# (future) file rather than an existing one. Future files don't violate.
_FUTURE_MARKERS = (
    "new file", "to be created", "to create",
    "(new)", "(to be created)", "will create", "will add",
    "i'll create", "i will create", "create a new",
    "create this file", "add this file", "stub", "scaffold",
)

# Window (in chars) around the citation to check for future-marker context.
_WINDOW = 80


class PlanCitationAnchor:
    """Verify every path citation in a plan exists, unless explicitly future."""

    spec_version = 1

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="plan_with_claude")
        text = aria_result or tool_call.get("result", "") or ""
        if not text:
            report.fact("status", "no_text_to_check", "trace_inspection")
            return report

        citations = _PATH_RE.findall(text)
        # De-duplicate while preserving first-seen order so violations cite
        # the first occurrence the human reader will encounter.
        seen: dict[str, int] = {}
        for c in citations:
            if c not in seen:
                seen[c] = text.find(c)

        report.fact("citation_count", len(seen), "regex_extraction")

        missing: list[str] = []
        for path, idx in seen.items():
            if _path_resolves(path):
                continue
            context = text[max(0, idx - _WINDOW): idx + len(path) + _WINDOW].lower()
            if any(m in context for m in _FUTURE_MARKERS):
                continue
            missing.append(path)

        if missing:
            report.fact("missing_items", missing[:20], "filesystem_check")
            report.violate(
                7,
                "hard",
                "Plan references files that do not exist (and are not marked "
                f"as new): {', '.join(missing[:5])}"
                + (f"; +{len(missing) - 5} more" if len(missing) > 5 else ""),
            )

        return report

    async def health_check(self) -> bool:
        return any(os.path.isdir(r) for r in ALLOWED_ROOTS)


def _path_resolves(path: str) -> bool:
    """True iff `path` exists as a file under any allowed root.

    Plans cite relative paths (`src/foo.py`); the anchor checks each
    allowed root in order. We accept the first hit.
    """
    for root in ALLOWED_ROOTS:
        candidate = os.path.join(root, path)
        if os.path.isfile(candidate):
            return True
    return False
