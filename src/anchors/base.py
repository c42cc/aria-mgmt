"""Anchor protocol and core dataclasses."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

log = logging.getLogger(__name__)

VERDICT_RANK = {"correct": 2, "degraded": 1, "failed": 0}


@dataclass(frozen=True)
class Fact:
    key: str
    value: Any
    source: str


@dataclass(frozen=True)
class Violation:
    spec_property: int
    severity: str          # "hard" | "soft"
    detail: str


@dataclass
class AnchorReport:
    tool: str
    facts: list[Fact] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    binary: str = "correct"    # "correct" | "degraded" | "failed"
    unverified: bool = False

    def fact(self, key: str, value: Any, source: str) -> None:
        self.facts.append(Fact(key=key, value=value, source=source))

    def violate(self, prop: int, severity: str, detail: str) -> None:
        self.violations.append(Violation(spec_property=prop, severity=severity, detail=detail))
        if severity == "hard":
            self.binary = "failed"
        elif self.binary == "correct":
            self.binary = "degraded"

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "facts": [{"key": f.key, "value": f.value, "source": f.source} for f in self.facts],
            "violations": [{"prop": v.spec_property, "severity": v.severity, "detail": v.detail} for v in self.violations],
            "binary": self.binary,
            "unverified": self.unverified,
        }


class Anchor(Protocol):
    """Protocol every anchor must satisfy."""

    spec_version: int

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        """Compare tool_call trace entry against ground truth.

        tool_call has keys: tool, args, result, result_chars, result_truncated.
        aria_result is the final text Aria produced.
        """
        ...

    async def health_check(self) -> bool:
        """Return True if this anchor's dependencies are reachable."""
        ...


def verdict_min(a: str, b: str) -> str:
    return a if VERDICT_RANK.get(a, 0) <= VERDICT_RANK.get(b, 0) else b


def floor_from_reports(reports: list[AnchorReport]) -> str | None:
    """Compute worst binary across all anchor reports.

    Returns None if all anchors are unverified or no reports exist.
    """
    verified = [r for r in reports if not r.unverified]
    if not verified:
        return None
    worst = "correct"
    for r in verified:
        worst = verdict_min(worst, r.binary)
    return worst
