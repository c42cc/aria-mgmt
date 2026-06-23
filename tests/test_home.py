"""The house endpoint — pure NL->call logic AND a full dispatcher round-trip.

The round-trip runs the real `dispatcher.run(home-control, ...)` against an
in-process Home Assistant API double over a real socket, so we verify the whole
path: resolve entity -> call_service -> re-read state -> ground-truth verdict.
The double behaves like HA's documented REST API; the only thing it cannot prove
is a physical bulb (hardware-gated, accepted), which is exactly what the Phase-4
camera+Gemini harness covers.
"""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from src import dispatcher, homeassistant
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


# --------------------------------------------------------------------------
# pure logic
# --------------------------------------------------------------------------

def test_normalize_action_synonyms_and_junk():
    assert homeassistant.normalize_action("please turn on the lights") == "on"
    assert homeassistant.normalize_action("OFF") == "off"
    assert homeassistant.normalize_action("unlock") == "unlock"
    assert homeassistant.normalize_action("shut the blinds") == "close"
    assert homeassistant.normalize_action("activate movie night") == "activate"
    assert homeassistant.normalize_action("frobnicate") is None


def _states():
    return [
        {"entity_id": "light.living_room", "state": "off", "attributes": {"friendly_name": "Living Room Lights"}},
        {"entity_id": "light.kitchen", "state": "off", "attributes": {"friendly_name": "Kitchen Lights"}},
        {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "Front Door"}},
        {"entity_id": "cover.bedroom", "state": "closed", "attributes": {"friendly_name": "Bedroom Blinds"}},
        {"entity_id": "scene.movie_night", "state": "on", "attributes": {"friendly_name": "Movie Night"}},
    ]


def test_resolve_entity_match_filter_and_ambiguous():
    s = _states()
    e, amb = homeassistant.resolve_entity(s, "living room lights", ("light", "switch"))
    assert e and e["entity_id"] == "light.living_room" and not amb
    # domain filter: a lock verb must not match a light
    e2, _ = homeassistant.resolve_entity(s, "front door", ("lock",))
    assert e2 and e2["entity_id"] == "lock.front_door"
    # no match
    e3, amb3 = homeassistant.resolve_entity(s, "garage", ("cover",))
    assert e3 is None and amb3 == []
    # ambiguous: "lights" ties living-room and kitchen
    e4, amb4 = homeassistant.resolve_entity(s, "lights", ("light",))
    assert e4 is None and len(amb4) == 2


def test_plan_call_covers_the_domains():
    s = {e["entity_id"]: e for e in _states()}
    assert homeassistant.plan_call("on", s["light.living_room"], None)[:2] == ("homeassistant", "turn_on")
    assert homeassistant.plan_call("off", s["light.living_room"], None)[:2] == ("homeassistant", "turn_off")
    assert homeassistant.plan_call("unlock", s["lock.front_door"], None) == ("lock", "unlock", {"entity_id": "lock.front_door"}, "unlocked")
    assert homeassistant.plan_call("open", s["cover.bedroom"], None)[:2] == ("cover", "open_cover")
    assert homeassistant.plan_call("activate", s["scene.movie_night"], None)[:2] == ("scene", "turn_on")
    dom, svc, data, _ = homeassistant.plan_call("set", s["light.living_room"], "50%")
    assert (dom, svc) == ("light", "turn_on") and data["brightness_pct"] == 50
    assert homeassistant.plan_call("set", s["light.living_room"], None) is None  # set needs a value


def test_actuate_unconfigured_is_loud_not_silent(cfgset):
    cfgset(hass_url="", hass_token="")
    r = homeassistant.actuate("living room lights", "on")
    assert r.delivered is False and "configured" in r.broke and "no cloud fallback" in r.broke.lower()


# --------------------------------------------------------------------------
# full dispatcher round-trip vs a faithful HA API double over a real socket
# --------------------------------------------------------------------------

def _ha_stub(entities, *, do_actuate=True):
    state = {e["entity_id"]: dict(e) for e in entities}
    _SERVICE_STATE = {
        "turn_on": "on", "turn_off": "off", "toggle": "on", "lock": "locked",
        "unlock": "unlocked", "open_cover": "open", "close_cover": "closed",
    }

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path == "/api/states":
                return self._send(list(state.values()))
            if self.path.startswith("/api/states/"):
                eid = self.path[len("/api/states/"):]
                return self._send(state.get(eid) or {}, 200 if eid in state else 404)
            if self.path == "/api/":
                return self._send({"message": "API running."})
            return self._send({}, 404)

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(ln) or b"{}")
            if self.path.startswith("/api/services/"):
                _, _, _, _domain, service = self.path.split("/", 4)
                eid = body.get("entity_id")
                if do_actuate and eid in state and service in _SERVICE_STATE:
                    state[eid]["state"] = _SERVICE_STATE[service]
                return self._send([])
            return self._send({}, 404)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _point_at(cfgset, base):
    cfgset(hass_url=base, hass_token="test-token")


def test_dispatch_turn_on_verifies_ground_truth(cfgset):
    srv, base = _ha_stub(_states())
    try:
        _point_at(cfgset, base)
        loop = load_loops()["home-control"]
        res = dispatcher.run(loop, {"device": "living room lights", "action": "on"})
        assert res.delivered is True, res.broke
        assert "on" in res.summary.lower()
        # ground truth actually flipped
        assert homeassistant.get_state("light.living_room")["state"] == "on"
    finally:
        srv.shutdown()


def test_dispatch_unlock_verifies(cfgset):
    srv, base = _ha_stub(_states())
    try:
        _point_at(cfgset, base)
        loop = load_loops()["home-control"]
        res = dispatcher.run(loop, {"device": "front door", "action": "unlock"})
        assert res.delivered is True, res.broke
        assert homeassistant.get_state("lock.front_door")["state"] == "unlocked"
    finally:
        srv.shutdown()


def test_dispatch_unknown_entity_is_honest(cfgset):
    srv, base = _ha_stub(_states())
    try:
        _point_at(cfgset, base)
        loop = load_loops()["home-control"]
        res = dispatcher.run(loop, {"device": "garage door", "action": "open"})
        assert res.delivered is False
        assert "no exposed entity" in res.broke
    finally:
        srv.shutdown()


def test_dispatch_groundtruth_catches_a_noop(cfgset):
    # The service call "succeeds" (HTTP 200) but the state never changes. The
    # endpoint must NOT report delivered — it verifies the actual state.
    srv, base = _ha_stub(_states(), do_actuate=False)
    try:
        _point_at(cfgset, base)
        loop = load_loops()["home-control"]
        res = dispatcher.run(loop, {"device": "living room lights", "action": "on"})
        assert res.delivered is False
        assert "not verified" in res.broke
    finally:
        srv.shutdown()


def test_dispatch_status_reads_state(cfgset):
    srv, base = _ha_stub(_states())
    try:
        _point_at(cfgset, base)
        loop = load_loops()["home-status"]
        res = dispatcher.run(loop, {"device": "front door"})
        assert res.delivered is True
        assert "locked" in res.summary.lower()
    finally:
        srv.shutdown()


def test_unreachable_hub_is_loud_not_silent(cfgset):
    # Point at a closed port: a connection failure is a loud, typed broke with the
    # fix — never a silent success, never blamed on HA.
    _point_at(cfgset, "http://127.0.0.1:1")  # nothing listening
    loop = load_loops()["home-control"]
    res = dispatcher.run(loop, {"device": "living room lights", "action": "on"})
    assert res.delivered is False
    assert "reach Home Assistant" in res.broke or "HASS_URL" in res.broke
