"""Shared normalization helpers for all anchors."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone


def content_hash(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def normalize_timestamp(ts: str) -> str:
    """Best-effort parse to UTC ISO string. Returns original on failure."""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            dt = datetime.strptime(ts.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            continue
    return ts


def extract_number_claim(text: str, keyword: str) -> int | None:
    """Extract a numeric claim near a keyword from Aria's prose.

    Handles patterns like:
      "7 events total", "approximately 13 receipts", "~150 emails",
      "I retrieved 147 emails"
    """
    patterns = [
        rf"(?:approximately|about|roughly|~|around)?\s*(\d+)\s+{re.escape(keyword)}",
        rf"{re.escape(keyword)}.*?(\d+)",
        rf"(\d+)\s+(?:total\s+)?{re.escape(keyword)}",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return int(m.group(1))
    return None


def ids_from_text(text: str, prefix: str = "ID:") -> list[str]:
    """Extract ID values from MCP tool result text."""
    ids = []
    for line in text.split("\n"):
        if prefix in line:
            parts = line.split(prefix, 1)
            if len(parts) > 1:
                id_val = parts[1].strip().split()[0].rstrip(",;")
                if id_val:
                    ids.append(id_val)
    return ids
