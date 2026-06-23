"""The Spark endpoint — a local model as an executor, via the Anthropic SDK.

Full dispatcher round-trip against an in-process Anthropic-Messages double over a
real socket (vLLM serves /v1/messages natively, so the SDK drives it unchanged).
Proves: configured -> the local model answers -> delivered; unconfigured -> a loud
"not configured", never a silent cloud fallback.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from src import dispatcher
from src.config import config
from src.loops import load_loops


@pytest.fixture
def cfgset():
    """Override frozen-dataclass config fields for a test, restoring after."""
    saved: dict = {}

    def _set(**kw):
        for k, v in kw.items():
            if k not in saved:
                saved[k] = getattr(config, k)
            object.__setattr__(config, k, v)

    yield _set
    for k, v in saved.items():
        object.__setattr__(config, k, v)


def _spark_stub(answer="Here is a haiku about the sea."):
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(ln)  # drain
            msg = {
                "id": "msg_stub",
                "type": "message",
                "role": "assistant",
                "model": "local-brain",
                "content": [{"type": "text", "text": answer}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 9},
            }
            b = json.dumps(msg).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_spark_unconfigured_is_loud(cfgset):
    cfgset(spark_base_url="")
    res = dispatcher.run(load_loops()["local-ask"], {"request": "draft a haiku"})
    assert res.delivered is False
    assert "configured" in res.broke and "no cloud fallback" in res.broke.lower()


def test_spark_round_trip_delivers(cfgset):
    srv, base = _spark_stub("Salt wind on the waves / a quiet tide returning / the gulls call goodnight")
    try:
        cfgset(spark_base_url=base, spark_model="local-brain")
        res = dispatcher.run(load_loops()["local-ask"], {"request": "draft a haiku about the sea"})
        assert res.delivered is True, res.broke
        assert "gulls" in res.summary
    finally:
        srv.shutdown()


def test_spark_empty_answer_not_delivered(cfgset):
    srv, base = _spark_stub("")
    try:
        cfgset(spark_base_url=base, spark_model="local-brain")
        res = dispatcher.run(load_loops()["local-ask"], {"request": "say nothing"})
        assert res.delivered is False
        assert "empty" in (res.broke or "")
    finally:
        srv.shutdown()
