"""Cached Google API clients for anchor re-fetch calls.

Reuses the same OAuth credentials Aria's MCP servers use.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

_gmail_service: Any = None
_calendar_service: Any = None

GMAIL_CREDS_PATH = os.path.expanduser("~/.gmail-mcp/credentials.json")
GCAL_TOKEN_PATH = os.path.expanduser("~/.config/google-calendar-mcp/tokens.json")
GCAL_KEYS_PATH = os.path.expanduser("~/.config/google-calendar-mcp/gcp-oauth.keys.json")


def _build_gmail_creds():
    from google.oauth2.credentials import Credentials
    with open(GMAIL_CREDS_PATH) as f:
        data = json.load(f)
    return Credentials(
        token=None,
        refresh_token=data["refresh_token"],
        client_id=data["client_id"],
        client_secret=data["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
    )


def get_gmail_service():
    global _gmail_service
    if _gmail_service is not None:
        return _gmail_service
    try:
        from googleapiclient.discovery import build
        creds = _build_gmail_creds()
        _gmail_service = build("gmail", "v1", credentials=creds)
        return _gmail_service
    except Exception:
        log.warning("Failed to build Gmail API client for anchor", exc_info=True)
        return None


def _build_gcal_creds():
    from google.oauth2.credentials import Credentials
    with open(GCAL_TOKEN_PATH) as f:
        token_data = json.load(f)
    with open(GCAL_KEYS_PATH) as f:
        keys = json.load(f)["installed"]
    return Credentials(
        token=token_data.get("access_token"),
        refresh_token=token_data.get("refresh_token"),
        client_id=keys["client_id"],
        client_secret=keys["client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
    )


def get_calendar_service():
    global _calendar_service
    if _calendar_service is not None:
        return _calendar_service
    try:
        from googleapiclient.discovery import build
        creds = _build_gcal_creds()
        _calendar_service = build("calendar", "v3", credentials=creds)
        return _calendar_service
    except Exception:
        log.warning("Failed to build Calendar API client for anchor", exc_info=True)
        return None
