"""Google Calendar anchor: list-events recount, get-event verify."""

from __future__ import annotations

import asyncio
import json
import logging
import re

from .base import AnchorReport
from .normalize import extract_number_claim
from .oauth_clients import get_calendar_service

log = logging.getLogger(__name__)


class GoogleCalendarAnchor:
    spec_version = 1

    async def check(self, tool_call: dict, aria_result: str) -> AnchorReport:
        tool_name = tool_call.get("tool", "")
        if "list-events" in tool_name or "list_events" in tool_name:
            return await self._check_list_events(tool_call, aria_result)
        if "get-event" in tool_name or "get_event" in tool_name:
            return await self._check_get_event(tool_call, aria_result)
        if "create-event" in tool_name or "create_event" in tool_name:
            return await self._check_create_event(tool_call, aria_result)
        report = AnchorReport(tool=f"calendar.{tool_name}")
        report.fact("status", "no_anchor_for_subtool", "registry")
        return report

    async def _check_list_events(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="calendar.list-events")
        svc = get_calendar_service()
        if not svc:
            report.unverified = True
            return report

        args = tool_call.get("args", {})
        time_min = args.get("timeMin", "")
        time_max = args.get("timeMax", "")
        calendar_ids = args.get("calendarId", [])
        tz = args.get("timeZone", "UTC")

        if isinstance(calendar_ids, str):
            calendar_ids = [calendar_ids]
        if not time_min or not calendar_ids:
            report.fact("status", "missing_time_or_calendar_args", "trace_inspection")
            return report

        all_events = []
        for cal_id in calendar_ids:
            try:
                resp = await asyncio.to_thread(
                    svc.events().list(
                        calendarId=cal_id, timeMin=time_min, timeMax=time_max,
                        timeZone=tz, singleEvents=True, orderBy="startTime",
                    ).execute
                )
                all_events.extend(resp.get("items", []))
            except Exception as e:
                log.warning("Calendar anchor: failed to list %s: %s", cal_id, e)

        ground_count = len(all_events)
        ground_summaries = [e.get("summary", "(no title)") for e in all_events]
        report.fact("ground_truth_event_count", ground_count, "gcal_api_recount")
        report.fact("ground_truth_summaries", ground_summaries, "gcal_api_recount")

        aria_count = extract_number_claim(aria_result, "event")
        if aria_count is not None:
            report.fact("aria_claimed_count", aria_count, "aria_result_extraction")
            if aria_count != ground_count:
                report.violate(3, "hard", f"Aria claimed {aria_count} events, ground truth is {ground_count}")

        return report

    async def _check_get_event(self, tool_call: dict, aria_result: str) -> AnchorReport:
        report = AnchorReport(tool="calendar.get-event")
        svc = get_calendar_service()
        if not svc:
            report.unverified = True
            return report

        args = tool_call.get("args", {})
        cal_id = args.get("calendarId", "")
        event_id = args.get("eventId", "")
        if not cal_id or not event_id:
            return report

        try:
            event = await asyncio.to_thread(
                svc.events().get(calendarId=cal_id, eventId=event_id).execute
            )
            report.fact("event_summary", event.get("summary", ""), "gcal_api")
            report.fact("event_start", event.get("start", {}), "gcal_api")
        except Exception as e:
            report.unverified = True
            report.fact("error", str(e)[:200], "gcal_api")

        return report

    async def _check_create_event(self, tool_call: dict, aria_result: str) -> AnchorReport:
        """Write anchor: verify the event exists after creation."""
        report = AnchorReport(tool="calendar.create-event")
        svc = get_calendar_service()
        if not svc:
            report.unverified = True
            return report

        trace_result = tool_call.get("result", "")
        if "declined" in trace_result.lower() or "error" in trace_result.lower()[:60]:
            report.fact("create_outcome", "declined_or_errored", "trace_inspection")
            return report

        event_id_match = re.search(r'"id"\s*:\s*"([^"]+)"', trace_result)
        if not event_id_match:
            report.fact("status", "no_event_id_in_result", "trace_inspection")
            return report

        event_id = event_id_match.group(1)
        cal_id = tool_call.get("args", {}).get("calendarId", "primary")

        try:
            event = await asyncio.to_thread(
                svc.events().get(calendarId=cal_id, eventId=event_id).execute
            )
            report.fact("event_exists", True, "gcal_api")
            report.fact("event_summary", event.get("summary", ""), "gcal_api")
        except Exception as e:
            report.violate(7, "hard", f"Created event {event_id} not found: {e}")

        return report

    async def health_check(self) -> bool:
        svc = get_calendar_service()
        if not svc:
            return False
        try:
            await asyncio.to_thread(svc.calendarList().list().execute)
            return True
        except Exception:
            return False
