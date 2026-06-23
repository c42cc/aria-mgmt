"""Boot preflight — loud, cheap, earns its place.

Two checks that prevent a failure in front of the user (review 3.2): the keys
are present, and every configured model id actually resolves (a 1-token ping).
A retired/typo'd model id is an instant hard error otherwise. No silent
fallback: a failure refuses ready and prints the exact fix.
"""

from __future__ import annotations

import anthropic

from .config import config
from .loops import load_loops


class PreflightError(RuntimeError):
    pass


def check(ping_models: bool = True) -> list[str]:
    """Return a list of OK lines, or raise PreflightError with the fix."""
    ok: list[str] = []

    if not config.anthropic_api_key:
        raise PreflightError("ANTHROPIC_API_KEY is empty — set it in .env (the conductor can't think without it).")
    ok.append("anthropic key present")

    loops = load_loops()
    ok.append(f"{len(loops)} loop(s) loaded: {', '.join(loops)}")

    # Optional endpoints (Phase 4 house + the Spark local-model). Reported, not
    # forced: an unconfigured endpoint is fine (a later phase), and the loud
    # connectivity check lives at USE time (the endpoint returns the one fix),
    # so a briefly-down hub never blocks the whole bot from booting.
    ok.append(
        "house endpoint: configured"
        if (config.hass_url and config.hass_token)
        else "house endpoint: not configured (set HASS_URL + HASS_TOKEN for Phase 4)"
    )
    ok.append(
        f"spark endpoint: configured ({config.spark_model})"
        if config.spark_base_url
        else "spark endpoint: not configured (set SPARK_BASE_URL)"
    )

    if ping_models:
        client = anthropic.Anthropic(api_key=config.anthropic_api_key, timeout=30)
        try:
            client.messages.create(
                model=config.reasoning_model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
        except anthropic.NotFoundError as e:
            raise PreflightError(
                f"reasoning model {config.reasoning_model!r} did not resolve ({e}). "
                "Pin a current id (e.g. claude-opus-4-8) via ARIA_REASONING_MODEL."
            ) from e
        ok.append(f"reasoning model resolves: {config.reasoning_model}")

    return ok
