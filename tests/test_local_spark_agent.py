"""Unit tests for the Local Spark Agent — the local-brained chat window.

Locks the pure logic that the live stack depends on, so a regression reds the
gate without needing a Spark or a browser:

  - serve-gate assertions (src/spark.py): especially the tool_use round-trip,
    the #1 OSS-serving risk;
  - the live-meter local-brain receipt (distinct name + brain field, so a local
    receipt never overwrites the canonical cloud trunk proof);
  - the chat event hub (ask/propose blocking + reply resolution);
  - the web transport end-to-end (real aiohttp routes, the agent loop monkey-
    patched) — POST /chat -> the loop -> /last shows a tool-backed answer;
  - the no-silent-fallback / no-ModelRouter posture.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_REPO, os.path.join(_REPO, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from src import spark  # noqa: E402


# --------------------------------------------------------------------------
# serve-gate assertions (the contract scripts/spark_serve.py proves)
# --------------------------------------------------------------------------

def _msg(stop_reason: str, blocks: list[dict], status: int = 200) -> str:
    return json.dumps({"stop_reason": stop_reason, "content": blocks}) + f"\nHTTP_STATUS={status}\n"


def test_serve_toolcall_pass():
    out = _msg("tool_use", [{"type": "tool_use", "name": "get_weather", "input": {"city": "Paris"}}])
    ok, detail = spark.assert_serve_toolcall(out, 0)
    assert ok is True and "tool_use" in detail


def test_serve_toolcall_fail_when_no_tool_block():
    # The exact OSS failure mode: model talked instead of calling the tool.
    out = _msg("end_turn", [{"type": "text", "text": "It is sunny in Paris."}])
    ok, detail = spark.assert_serve_toolcall(out, 0)
    assert ok is False and "tool" in detail.lower()


def test_serve_toolcall_fail_when_stop_reason_drifts():
    # tool_use block present but stop_reason wrong (parser drift) -> FAIL loudly.
    out = _msg("end_turn", [{"type": "tool_use", "name": "get_weather", "input": {}}])
    ok, _ = spark.assert_serve_toolcall(out, 0)
    assert ok is False


def test_serve_toolcall_fail_on_non_200():
    out = _msg("tool_use", [{"type": "tool_use", "name": "get_weather", "input": {}}], status=500)
    ok, _ = spark.assert_serve_toolcall(out, 0)
    assert ok is False


def test_serve_models_and_chat_and_cache_control():
    assert spark.assert_serve_models('{"data":[{"id":"local-brain"}]}\nHTTP_STATUS=200', 0)[0]
    assert not spark.assert_serve_models('{"data":[{"id":"other"}]}\nHTTP_STATUS=200', 0)[0]
    chat = _msg("end_turn", [{"type": "text", "text": "OK"}])
    assert spark.assert_serve_chat(chat, 0)[0]
    assert not spark.assert_serve_chat(_msg("end_turn", [], status=200), 0)[0]
    cc = _msg("end_turn", [{"type": "text", "text": "OK"}])
    assert spark.assert_serve_cache_control(cc, 0)[0]
    assert not spark.assert_serve_cache_control('{}\nHTTP_STATUS=400', 0)[0]


def test_serve_gpu_residency():
    assert spark.assert_serve_gpu("NVIDIA GB10, 101376 MiB, 122880 MiB", 0)[0]
    # idle GPU (model not loaded) must fail
    assert not spark.assert_serve_gpu("NVIDIA GB10, 512 MiB, 122880 MiB", 0)[0]
    assert not spark.assert_serve_gpu("NVIDIA A100, 90000 MiB, 122880 MiB", 0)[0]


def test_serve_payloads_carry_cache_control_like_the_loop():
    p = spark.messages_payload_cache_control()
    assert p["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert p["tools"][0]["cache_control"] == {"type": "ephemeral"}
    assert p["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    tc = spark.messages_payload_toolcall()
    assert tc["tools"][0]["name"] == "get_weather"


def test_serve_endpoint_uses_tailnet_ip():
    assert spark.serve_endpoint("spark1") == "http://100.106.152.104:8000"


def test_serve_model_registry_has_both_bench_candidates():
    assert "gpt-oss-120b" in spark.SERVE_MODELS
    assert "qwen3-30b-a3b" in spark.SERVE_MODELS
    key, cfg = spark._resolve_serve_model(None)
    assert key == spark.DEFAULT_SERVE_MODEL and cfg["parser"]


# --------------------------------------------------------------------------
# live-meter local-brain receipt
# --------------------------------------------------------------------------

def test_live_meter_brain_receipt_and_suffix(tmp_path, monkeypatch):
    import live_meter

    cloud = live_meter.build_receipt(
        build_hash="abc", head_sha="h", dirty=False, branch="main",
        certifies_trunk=True, task="t", result="ok", tool_fired=True, ok=True,
        verdict_reason="r",
    )
    assert cloud["brain"] == "cloud" and cloud["certifies_trunk"] is True

    local = live_meter.build_receipt(
        build_hash="abc", head_sha="h", dirty=False, branch="main",
        certifies_trunk=False, task="t", result="ok", tool_fired=True, ok=True,
        verdict_reason="r", brain="http://spark:8000",
    )
    assert local["brain"] == "http://spark:8000"

    monkeypatch.setattr(live_meter, "_receipts_dir", lambda: str(tmp_path))
    cloud_path = live_meter.write_receipt(cloud)
    local_path = live_meter.write_receipt(local, suffix=".local")
    assert cloud_path.endswith("abc.json")
    assert local_path.endswith("abc.local.json")
    assert cloud_path != local_path  # a local run never clobbers the cloud proof


# --------------------------------------------------------------------------
# chat event hub
# --------------------------------------------------------------------------

def test_chat_hub_ask_then_resolve():
    from src import local_chat_web as w

    hub = w.ChatHub()

    async def scenario():
        task = asyncio.create_task(hub.ask("s1", "your name?", timeout=5))
        await asyncio.sleep(0.05)
        ev = hub.sessions["s1"].queue.get_nowait()
        assert ev["type"] == "ask" and ev["text"] == "your name?"
        assert hub.resolve("s1", ev["id"], "Aria") is True
        return await task

    assert asyncio.run(scenario()) == "Aria"


def test_chat_hub_ask_timeout_returns_empty():
    from src import local_chat_web as w
    hub = w.ChatHub()
    assert asyncio.run(hub.ask("s2", "q", timeout=0.05)) == ""


def test_chat_hub_emit_and_broadcast():
    from src import local_chat_web as w
    hub = w.ChatHub()

    async def s():
        await hub.emit("a", {"type": "status", "text": "hi"})
        hub.get("b")  # second session
        await hub.broadcast({"type": "note", "text": "bye"})
        return hub.sessions["a"].queue.qsize(), hub.sessions["b"].queue.qsize()

    a_sz, b_sz = asyncio.run(s())
    assert a_sz == 2 and b_sz == 1


# --------------------------------------------------------------------------
# web transport (real aiohttp routes; the agent loop monkeypatched)
# --------------------------------------------------------------------------

def test_web_transport_chat_to_last(monkeypatch):
    asyncio.run(_web_flow(monkeypatch))


async def _web_flow(monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from src import local_chat_web as w
    from src import tools

    w.hub.sessions.clear()
    w.hub.answers.clear()

    async def fake_loop(task, session_key=""):
        # simulate a real tool firing so tool_fired is True
        tools._state_for(session_key).last_tool_trace = [{"tool": "list_directory"}]
        return f"Done: counted dirs for {task[:8]}"

    monkeypatch.setattr(tools, "_do_with_claude", fake_loop)

    client = TestClient(TestServer(w.build_app()))
    await client.start_server()
    try:
        r = await client.get("/healthz")
        assert r.status == 200

        r = await client.post("/chat", json={"session": "t1", "message": "list dirs please"})
        assert r.status == 200

        ans = None
        for _ in range(100):
            r = await client.get("/last?session=t1")
            d = await r.json()
            if d.get("answered"):
                ans = d
                break
            await asyncio.sleep(0.02)
        assert ans is not None, "no answer recorded"
        assert ans["tool_fired"] is True
        assert "Done:" in ans["text"]

        # /chat requires a message
        r = await client.post("/chat", json={"session": "t1"})
        assert r.status == 400
    finally:
        await client.close()


# --------------------------------------------------------------------------
# posture: no silent cloud fallback, no ModelRouter
# --------------------------------------------------------------------------

def test_no_modelrouter_resurrected():
    # The brain is a base_url, not a router. The removed src/ucs.py ModelRouter
    # must not come back under the local-brain work.
    for mod in ("local_chat_web", "spark", "tools"):
        path = os.path.join(_REPO, "src", f"{mod}.py")
        text = open(path, encoding="utf-8").read()
        assert "class ModelRouter" not in text
        assert "UCS_ENABLED" not in text


def test_search_server_absent_without_key(monkeypatch):
    # No key configured -> no dead, perpetually-failing search server registered.
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    import importlib
    from src import mcp as mcp_mod
    importlib.reload(mcp_mod)
    assert "search" not in mcp_mod.MCP_SERVERS
