"""Tests for the one typed dispatch contract (src.tools._invoke_handler).

Reproduces the `_do_with_claude() got an unexpected keyword argument 'prompt'`
crash (forensic 2026-06-16) and watches it become a clean, correctable schema
error instead of taking down the call.
"""

from __future__ import annotations

import asyncio
import json

from src.tools import _invoke_handler


def test_prompt_vs_task_crash_becomes_schema_error():
    async def do_with_claude(task, session_key=""):
        return f"ran: {task}"

    # The original crash input: the wrong argument name.
    out = asyncio.run(
        _invoke_handler(do_with_claude, {"prompt": "hi", "session_key": "s"})
    )
    data = json.loads(out)
    assert data["_error_class"] == "schema"
    assert "task" in data["error"]  # names the arg the caller actually needs


def test_valid_args_still_run():
    async def do_with_claude(task, session_key=""):
        return f"ran: {task}"

    out = asyncio.run(
        _invoke_handler(do_with_claude, {"task": "X", "session_key": "s"})
    )
    assert out == "ran: X"


def test_unknown_args_dropped_not_crash():
    async def remember(text):
        return f"stored: {text}"

    # session_key is not in remember()'s signature — it must be dropped, not
    # crash the call (the strictly-safer behavior vs the old blind splat).
    out = asyncio.run(
        _invoke_handler(remember, {"text": "fact", "session_key": "s"})
    )
    assert out == "stored: fact"


def test_var_kwargs_handler_gets_everything():
    async def flexible(**kw):
        return ",".join(sorted(kw))

    out = asyncio.run(_invoke_handler(flexible, {"a": 1, "b": 2}))
    assert out == "a,b"
