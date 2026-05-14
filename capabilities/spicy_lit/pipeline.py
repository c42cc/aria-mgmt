"""Outline generation via Grok text API. Ported from the original SpicyLit project."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

from .prompts import get_outline_prompt

log = logging.getLogger(__name__)

GROK_TEXT_URL = "https://api.x.ai/v1/chat/completions"
GROK_TEXT_MODEL = "grok-4-fast"


@dataclass
class OutlineResult:
    outline_text: str
    kinks: list[str]
    user_name: str


async def _call_grok_text(prompt: str, api_key: str, max_tokens: int = 1000) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROK_TEXT_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(
            GROK_TEXT_URL, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=120)
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
            return data["choices"][0]["message"]["content"]


async def generate_outline(
    preferences: str,
    user_id: str,
    api_key: str,
    user_name: str = "You",
    kinks: list[str] | None = None,
    prior_outline: str | None = None,
    prior_kinks: list[str] | None = None,
    is_continuation: bool = False,
) -> OutlineResult:
    if not kinks:
        kinks = [preferences]

    prompt = get_outline_prompt(
        user_name=user_name,
        kinks=kinks,
        prior_outline=prior_outline,
        prior_kinks=prior_kinks,
        is_continuation=is_continuation,
    )

    log.info("Generating outline via Grok text API...")
    outline_text = await _call_grok_text(prompt, api_key)
    log.info("Outline generated: %d chars", len(outline_text))

    return OutlineResult(outline_text=outline_text, kinks=kinks, user_name=user_name)
