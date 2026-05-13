"""Outline generation via Grok text API. Ported from the original SpicyLit project."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

GROK_TEXT_URL = "https://api.x.ai/v1/chat/completions"
GROK_TEXT_MODEL = "grok-4-fast"


@dataclass
class OutlineResult:
    outline_text: str
    kinks: list[str]
    user_name: str


def get_outline_prompt(
    user_name: str,
    kinks: list[str],
    target_pages: int = 2,
    target_words: int = 2500,
    prior_outline: str | None = None,
    prior_kinks: list[str] | None = None,
    is_continuation: bool = False,
) -> str:
    if is_continuation and prior_outline:
        return (
            f"Create a concise outline for a {target_pages}-page CONTINUATION of an erotic story.\n\n"
            f"CRITICAL: This is a CONTINUATION. Maintain the SAME characters, names, and relationships.\n\n"
            f"Previous outline:\n{prior_outline}\n\n"
            f"Previous kinks explored: {', '.join(prior_kinks) if prior_kinks else 'None'}\n"
            f"New kinks to add: {', '.join(kinks)}\n\n"
            f"First-person from {user_name}. Structure: 1) Opening that continues from previous, "
            f"2) Escalation, 3) {3 + (target_pages // 5)} deeper scenes, 4) Major climax, "
            f"5) Resolution. Target: {target_words} words. Be brief."
        )
    return (
        f"Create a concise outline for a {target_pages}-page erotic story in casual erotica style.\n\n"
        f"First-person from {user_name}. Kinks: {', '.join(kinks)}\n\n"
        f"Structure:\n"
        f"1. Opening hook with tension\n"
        f"2. Character intro with power dynamics\n"
        f"3. {3 + (target_pages // 5)} escalating scenes\n"
        f"4. Major climax\n"
        f"5. Resolution\n\n"
        f"Target: {target_words} words. Be brief - just key plot points."
    )


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
