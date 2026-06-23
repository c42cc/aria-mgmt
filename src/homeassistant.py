"""Home Assistant — the one substrate for the physical house (Phase 4).

Aria controls the whole house through ONE hub: Home Assistant. Every device is an
`entity` with a `state` and `services` you call, so Aria learns one verb surface
(`call_service` + `get_state`), never N device APIs. This module is that home: a
stdlib-only REST client (no new dependency), natural-language -> entity
resolution, a small action -> service map, and — the load-bearing part — it
verifies an action against GROUND TRUTH by re-reading the entity's state. The LLM
(the conductor) turns speech into (device, action); actuation here is
deterministic and off the model hot path.

No silent fallback: when HA is unconfigured or unreachable, the result says so
loudly with the one fix. A failure is never blamed on Home Assistant — it is our
connectivity/config to surface and fix.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import config

NOT_CONFIGURED = (
    "home control isn't configured — set HASS_URL and HASS_TOKEN in .env and "
    "expose the entities in Home Assistant (Settings > Voice assistants > Expose). "
    "There is no cloud fallback."
)

# Domains a verb can act on. on/off/toggle generalize across controllable domains
# via homeassistant.turn_on/off; the rest are domain-specific.
_CONTROLLABLE = (
    "light", "switch", "fan", "media_player", "input_boolean", "climate",
    "humidifier", "vacuum", "remote", "siren", "automation",
)
_DOMAINS_FOR_VERB: dict[str, tuple[str, ...] | None] = {
    "on": _CONTROLLABLE,
    "off": _CONTROLLABLE,
    "toggle": _CONTROLLABLE,
    "set": ("light", "climate", "fan"),
    "lock": ("lock",),
    "unlock": ("lock",),
    "open": ("cover",),
    "close": ("cover",),
    "activate": ("scene", "script"),
}

# Synonyms -> canonical verb.
_VERB_SYNONYMS: dict[str, str] = {
    "on": "on", "turn on": "on", "enable": "on", "start": "on", "power on": "on",
    "off": "off", "turn off": "off", "disable": "off", "stop": "off", "power off": "off", "kill": "off",
    "toggle": "toggle", "flip": "toggle",
    "lock": "lock", "unlock": "unlock",
    "open": "open", "raise": "open", "up": "open",
    "close": "close", "shut": "close", "lower": "close", "down": "close",
    "activate": "activate", "run": "activate", "scene": "activate", "trigger": "activate",
    "set": "set", "dim": "set", "brightness": "set", "temperature": "set", "temp": "set",
    "status": "status", "state": "status", "check": "status", "is": "status",
}

_TOKEN = re.compile(r"[a-z0-9]+")


class HomeAssistantError(RuntimeError):
    """A loud, typed HA connectivity/HTTP failure. Never a silent fallback."""


@dataclass
class HomeResult:
    delivered: bool
    summary: str
    broke: str | None


def configured() -> bool:
    return bool(config.hass_url and config.hass_token)


# ---------------------------------------------------------------------------
# REST client (stdlib urllib; one home for the HTTP)
# ---------------------------------------------------------------------------

def _request(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
    url = f"{config.hass_url}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {config.hass_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=config.hass_timeout_sec) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        if e.code in (401, 403):
            raise HomeAssistantError(
                "Home Assistant rejected the token (HTTP "
                f"{e.code}) — refresh HASS_TOKEN (a long-lived access token)."
            ) from e
        raise HomeAssistantError(f"HA request {method} {path} failed (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise HomeAssistantError(
            f"can't reach Home Assistant at {config.hass_url} ({e.reason}) — "
            "check HASS_URL and that the hub is up and on the Tailnet."
        ) from e


def get_states() -> list[dict]:
    _status, body = _request("GET", "/api/states")
    return body if isinstance(body, list) else []


def get_state(entity_id: str) -> dict | None:
    try:
        _status, body = _request("GET", f"/api/states/{entity_id}")
    except HomeAssistantError:
        return None
    return body if isinstance(body, dict) else None


def call_service(domain: str, service: str, data: dict) -> None:
    _request("POST", f"/api/services/{domain}/{service}", data)


def camera_snapshot(entity_id: str) -> bytes:
    """Current JPEG from a camera entity (/api/camera_proxy). The Phase-4
    physical-state capture, fed to Gemini for a genuine 'is it actually X?' read."""
    url = f"{config.hass_url}/api/camera_proxy/{entity_id}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {config.hass_token}"})
    try:
        with urllib.request.urlopen(req, timeout=config.hass_timeout_sec) as r:
            return r.read()
    except urllib.error.URLError as e:
        raise HomeAssistantError(
            f"can't fetch camera {entity_id} from {config.hass_url} ({e}) — "
            "check the camera entity is exposed and the hub is reachable."
        ) from e


# ---------------------------------------------------------------------------
# Natural language -> a deterministic, verifiable HA call
# ---------------------------------------------------------------------------

def _tokens(s: str) -> set[str]:
    return set(_TOKEN.findall((s or "").lower()))


def normalize_action(action: str) -> str | None:
    a = (action or "").strip().lower()
    if not a:
        return None
    if a in _VERB_SYNONYMS:
        return _VERB_SYNONYMS[a]
    # longest synonym phrase contained in the action wins (e.g. "please turn on")
    for phrase in sorted(_VERB_SYNONYMS, key=len, reverse=True):
        if phrase in a:
            return _VERB_SYNONYMS[phrase]
    return None


def _entity_label(e: dict) -> str:
    return str((e.get("attributes") or {}).get("friendly_name") or e.get("entity_id", ""))


def resolve_entity(
    states: list[dict], target: str, domains: tuple[str, ...] | None
) -> tuple[dict | None, list[str]]:
    """Best entity matching `target`, filtered to `domains`. Returns
    (entity_or_None, ambiguous_labels). Deterministic token-overlap scoring."""
    want = _tokens(target)
    if not want:
        return None, []
    pool = [
        e for e in states
        if isinstance(e, dict) and e.get("entity_id")
        and (domains is None or e["entity_id"].split(".")[0] in domains)
    ]
    scored: list[tuple[float, dict]] = []
    for e in pool:
        hay = _tokens(_entity_label(e)) | _tokens(e["entity_id"].replace(".", " "))
        if not hay:
            continue
        overlap = want & hay
        if not overlap:
            continue
        # fraction of the request's words matched, + a bump for an exact label.
        score = len(overlap) / len(want)
        if _tokens(_entity_label(e)) == want:
            score += 1.0
        scored.append((score, e))
    if not scored:
        return None, []
    scored.sort(key=lambda t: t[0], reverse=True)
    top = scored[0]
    # Ambiguous only when the runner-up ties the leader (don't nag on a clear win).
    contenders = [e for s, e in scored if abs(s - top[0]) < 1e-9]
    if len(contenders) > 1:
        return None, [_entity_label(e) for e in contenders[:5]]
    return top[1], []


def plan_call(verb: str, entity: dict, value: str | None) -> tuple[str, str, dict, str | None] | None:
    """(domain, service, data, expected_state) for a verb on an entity, or None
    if unsupported. on/off use the cross-domain homeassistant.turn_on/off."""
    eid = entity["entity_id"]
    domain = eid.split(".")[0]
    if verb == "on":
        return ("homeassistant", "turn_on", {"entity_id": eid}, "on")
    if verb == "off":
        return ("homeassistant", "turn_off", {"entity_id": eid}, "off")
    if verb == "toggle":
        return ("homeassistant", "toggle", {"entity_id": eid}, None)
    if verb == "lock":
        return ("lock", "lock", {"entity_id": eid}, "locked")
    if verb == "unlock":
        return ("lock", "unlock", {"entity_id": eid}, "unlocked")
    if verb == "open":
        return ("cover", "open_cover", {"entity_id": eid}, "open")
    if verb == "close":
        return ("cover", "close_cover", {"entity_id": eid}, "closed")
    if verb == "activate":
        return (domain if domain in ("scene", "script") else "scene", "turn_on", {"entity_id": eid}, None)
    if verb == "set":
        n = _first_number(value)
        if n is None:
            return None
        if domain == "light":
            return ("light", "turn_on", {"entity_id": eid, "brightness_pct": int(n)}, "on")
        if domain == "climate":
            return ("climate", "set_temperature", {"entity_id": eid, "temperature": n}, None)
        if domain == "fan":
            return ("fan", "set_percentage", {"entity_id": eid, "percentage": int(n)}, None)
    return None


def _first_number(value: str | None) -> float | None:
    if value is None:
        return None
    m = re.search(r"-?\d+(\.\d+)?", str(value))
    return float(m.group(0)) if m else None


def state_satisfies(state_obj: dict | None, expect: str | None) -> bool:
    if expect is None:
        return True
    if not isinstance(state_obj, dict):
        return False
    return str(state_obj.get("state", "")).lower() == expect.lower()


# ---------------------------------------------------------------------------
# Top-level operations (never raise; return a HomeResult)
# ---------------------------------------------------------------------------

def actuate(target: str, action: str, value: str | None = None) -> HomeResult:
    """Resolve target+action to one HA service call, fire it, and VERIFY the new
    state by re-reading it (ground truth). The whole house, one path."""
    if not configured():
        return HomeResult(False, "", NOT_CONFIGURED)
    verb = normalize_action(action)
    if verb in (None, "status"):
        return HomeResult(False, "", f"I don't know the action {action!r} — try on/off, lock/unlock, open/close, activate, or set <level>.")
    if not (target or "").strip():
        return HomeResult(False, "", "which device or room? (e.g., 'living room lights', 'front door')")
    try:
        states = get_states()
    except HomeAssistantError as e:
        return HomeResult(False, "", str(e))
    entity, ambiguous = resolve_entity(states, target, _DOMAINS_FOR_VERB.get(verb))
    if entity is None:
        if ambiguous:
            return HomeResult(False, "", f"which one did you mean: {', '.join(ambiguous)}?")
        return HomeResult(False, "", f"no exposed entity matches {target!r} — expose it in Home Assistant (Settings > Voice assistants > Expose).")
    plan = plan_call(verb, entity, value)
    if plan is None:
        return HomeResult(False, "", f"I can't '{verb}' {_entity_label(entity)} (unsupported for that device, or I need a value).")
    domain, service, data, expect = plan
    eid = entity["entity_id"]
    try:
        call_service(domain, service, data)
    except HomeAssistantError as e:
        return HomeResult(False, "", str(e))
    after = get_state(eid)
    after_state = str((after or {}).get("state", "?"))
    if not state_satisfies(after, expect):
        return HomeResult(
            False,
            f"{domain}.{service} on {eid}",
            f"called {domain}.{service} on {_entity_label(entity)} but it still reads {after_state!r} "
            f"(wanted {expect!r}) — not verified.",
        )
    return HomeResult(True, f"{_entity_label(entity)} is now {after_state}.", None)


def read_status(target: str) -> HomeResult:
    """Read an entity's current state (a tier-R query). Ground truth, no change."""
    if not configured():
        return HomeResult(False, "", NOT_CONFIGURED)
    if not (target or "").strip():
        return HomeResult(False, "", "which device or room?")
    try:
        states = get_states()
    except HomeAssistantError as e:
        return HomeResult(False, "", str(e))
    entity, ambiguous = resolve_entity(states, target, None)
    if entity is None:
        if ambiguous:
            return HomeResult(False, "", f"which one: {', '.join(ambiguous)}?")
        return HomeResult(False, "", f"no exposed entity matches {target!r}.")
    st = str(entity.get("state", "?"))
    return HomeResult(True, f"{_entity_label(entity)} is {st}.", None)
