"""Gmail anchors: search, read, send."""

from __future__ import annotations

import asyncio
import logging
import re

from .base import AnchorReport, log
from .normalize import extract_number_claim, ids_from_text
from .oauth_clients import get_gmail_service

log = logging.getLogger(__name__)

COUNT_TOLERANCE = 0.05  # 5% for approximate claims


class GmailSearchAnchor:
    spec_version = 1

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="gmail.search_emails")
        svc = get_gmail_service()
        if not svc:
            report.unverified = True
            report.fact("status", "gmail_api_unavailable", "oauth_clients")
            return report

        query = tool_call.get("args", {}).get("query", "")
        if not query:
            report.fact("status", "no_query_in_trace", "trace_inspection")
            return report

        try:
            resp = await asyncio.to_thread(
                svc.users().messages().list(userId="me", q=query, maxResults=500).execute
            )
        except Exception as e:
            report.unverified = True
            report.fact("error", str(e)[:200], "gmail_api")
            return report

        ground_count = resp.get("resultSizeEstimate", 0)
        ground_ids = [m["id"] for m in resp.get("messages", [])]

        report.fact("ground_truth_count", ground_count, "gmail_api_resultSizeEstimate")
        report.fact("ground_truth_ids_sample", ground_ids[:10], "gmail_api_messages_list")

        trace_result = tool_call.get("result", "")
        trace_ids = ids_from_text(trace_result, "ID:")
        report.fact("trace_id_count", len(trace_ids), "trace_inspection")

        aria_count = extract_number_claim(aria_result, "email")
        if aria_count is None:
            aria_count = extract_number_claim(aria_result, "message")
        if aria_count is not None:
            report.fact("aria_claimed_count", aria_count, "aria_result_extraction")
            exact = "exactly" in aria_result.lower() or "precisely" in aria_result.lower()
            tol = 0 if exact else max(1, int(ground_count * COUNT_TOLERANCE))
            if abs(aria_count - ground_count) > tol:
                report.violate(
                    3, "hard",
                    f"Aria claimed {aria_count}, ground truth is {ground_count} (tolerance={tol})"
                )

        if tool_call.get("result_truncated") and ground_count > len(trace_ids):
            report.fact("coverage_gap", ground_count - len(trace_ids), "comparison")

        return report

    async def health_check(self) -> bool:
        svc = get_gmail_service()
        if not svc:
            return False
        try:
            await asyncio.to_thread(
                svc.users().labels().get(userId="me", id="INBOX").execute
            )
            return True
        except Exception:
            return False


class GmailReadAnchor:
    spec_version = 1

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="gmail.read_email")
        svc = get_gmail_service()
        if not svc:
            report.unverified = True
            return report

        msg_id = tool_call.get("args", {}).get("messageId", "")
        if not msg_id:
            report.fact("status", "no_messageId", "trace_inspection")
            return report

        try:
            msg = await asyncio.to_thread(
                svc.users().messages().get(userId="me", id=msg_id, format="metadata",
                                           metadataHeaders=["Subject", "From", "Date"]).execute
            )
        except Exception as e:
            report.unverified = True
            report.fact("error", str(e)[:200], "gmail_api")
            return report

        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        report.fact("subject", headers.get("Subject", ""), "gmail_api")
        report.fact("from", headers.get("From", ""), "gmail_api")
        report.fact("date", headers.get("Date", ""), "gmail_api")
        return report

    async def health_check(self) -> bool:
        return get_gmail_service() is not None


class GmailSendAnchor:
    """Write anchor: poll Sent folder to verify the email was actually sent."""
    spec_version = 1
    # The Sent index is eventually consistent: a just-sent message may not yet
    # be searchable at the instant of dispatch. So a found==0 at the producer is
    # UNCONFIRMED (loud, but not a false wall) rather than a hard BLOCK; the
    # async correctness judge re-checks once Sent settles. (See
    # anchors/postcondition.py — `immediate=False` routes a HARD failure to an
    # annotation instead of a block.)
    immediate = False

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="gmail.send_email")
        svc = get_gmail_service()
        if not svc:
            report.unverified = True
            return report

        args = tool_call.get("args", {})
        to_addr = str(args.get("to", ""))
        subject = str(args.get("subject", ""))

        if not to_addr or not subject:
            report.fact("status", "missing_to_or_subject", "trace_inspection")
            return report

        trace_result = tool_call.get("result", "")
        if "declined" in trace_result.lower() or "error" in trace_result.lower()[:60]:
            report.fact("send_outcome", "declined_or_errored", "trace_inspection")
            return report

        try:
            query = f"in:sent to:{to_addr} subject:{subject}"
            resp = await asyncio.to_thread(
                svc.users().messages().list(userId="me", q=query, maxResults=5).execute
            )
            found = len(resp.get("messages", []))
            report.fact("sent_folder_match_count", found, "gmail_api_sent_poll")
            if found == 0:
                report.violate(7, "hard", f"No matching message found in Sent for to={to_addr} subject={subject}")
        except Exception as e:
            report.unverified = True
            report.fact("error", str(e)[:200], "gmail_api")

        return report

    async def health_check(self) -> bool:
        return get_gmail_service() is not None
