"""Shell anchor: audit-trail verification only. NEVER re-executes commands."""

from __future__ import annotations

import json
import logging

from .base import AnchorReport

log = logging.getLogger(__name__)


class ShellAnchor:
    spec_version = 1

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="shell.execute_command")
        trace_result = tool_call.get("result", "")

        if "declined" in trace_result.lower():
            report.fact("execution_status", "declined", "trace_inspection")
            if "confirmed" not in aria_result.lower() and "declined" not in aria_result.lower():
                report.violate(5, "soft", "Shell command was declined but Aria did not mention this")
            return report

        report.fact("execution_status", "executed", "trace_inspection")
        report.fact("result_chars", tool_call.get("result_chars", 0), "trace_inspection")
        return report

    async def health_check(self) -> bool:
        return True
