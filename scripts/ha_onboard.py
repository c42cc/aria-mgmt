#!/usr/bin/env python3
"""Headless Home Assistant onboarding + long-lived token minting.

Stands up the owner account on a fresh HA (the container on the Mind),
completes onboarding, mints a long-lived access token, and writes it straight
into .env as HASS_TOKEN (the secret is never printed). Idempotent-ish: if HA is
already onboarded it stops loudly with the one fix (it cannot recreate the owner).

    .venv/bin/python scripts/ha_onboard.py

Reads HASS_URL from .env (the Mind's HA over Tailscale).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from src.config import config  # noqa: E402

OWNER_NAME = "Aria"
OWNER_USER = "aria"
CLIENT = config.hass_url.rstrip("/") + "/"


def _post(path: str, body: dict, token: str | None = None) -> dict:
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(config.hass_url + path, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw.strip() else {}


def _onboarding_state() -> list[dict]:
    with urllib.request.urlopen(config.hass_url + "/api/onboarding", timeout=15) as r:
        return json.loads(r.read().decode())


async def _mint_long_lived(access_token: str) -> str:
    import websockets

    ws_url = config.hass_url.replace("http", "ws", 1) + "/api/websocket"
    async with websockets.connect(ws_url, max_size=None) as ws:
        await ws.recv()  # auth_required
        await ws.send(json.dumps({"type": "auth", "access_token": access_token}))
        ok = json.loads(await ws.recv())
        if ok.get("type") != "auth_ok":
            raise RuntimeError(f"WS auth failed: {ok}")
        await ws.send(json.dumps({
            "id": 1, "type": "auth/long_lived_access_token",
            "client_name": "aria-natural-state", "lifespan": 3650,
        }))
        resp = json.loads(await ws.recv())
        if not resp.get("success"):
            raise RuntimeError(f"long-lived token request failed: {resp}")
        return resp["result"]


def _write_env(token: str, password: str) -> None:
    env = REPO / ".env"
    lines = env.read_text().splitlines()
    out, set_token = [], False
    for ln in lines:
        if ln.startswith("HASS_TOKEN="):
            out.append(f"HASS_TOKEN={token}"); set_token = True
        else:
            out.append(ln)
    if not set_token:
        out.append(f"HASS_TOKEN={token}")
    if not any(l.startswith("HASS_OWNER_USERNAME=") for l in out):
        out.append(f"HASS_OWNER_USERNAME={OWNER_USER}")
        out.append(f"HASS_OWNER_PASSWORD={password}")
    env.write_text("\n".join(out) + "\n")


def main() -> int:
    if not config.hass_url:
        print("FATAL: HASS_URL is not set in .env", file=sys.stderr)
        return 2
    try:
        state = _onboarding_state()
    except urllib.error.URLError as e:
        print(f"FATAL: cannot reach HA at {config.hass_url} ({e}) — is the container up on the Mind?", file=sys.stderr)
        return 2

    user_done = next((s["done"] for s in state if s["step"] == "user"), False)
    if user_done:
        print("HA is already onboarded (owner exists). This script cannot recreate the owner.\n"
              "Fix: mint a long-lived token in the HA UI (Profile -> Long-lived tokens) and set HASS_TOKEN,\n"
              "or recreate the hub: ssh spark1 'docker rm -f homeassistant && rm -rf ~/.config/homeassistant/.storage' then rerun.",
              file=sys.stderr)
        return 1

    password = secrets.token_urlsafe(24)
    created = _post("/api/onboarding/users", {
        "client_id": CLIENT, "name": OWNER_NAME, "username": OWNER_USER,
        "password": password, "language": "en",
    })
    auth_code = created.get("auth_code")
    if not auth_code:
        print(f"FATAL: onboarding/users returned no auth_code: {created}", file=sys.stderr)
        return 2

    # auth_code -> access_token (form-encoded grant).
    form = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": auth_code, "client_id": CLIENT,
    }).encode()
    req = urllib.request.Request(config.hass_url + "/auth/token", data=form, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        tok = json.loads(r.read().decode())
    access_token = tok["access_token"]

    # Finish the remaining onboarding steps so the hub is fully set up (loud if any fail).
    for step in ("core_config", "analytics"):
        try:
            _post(f"/api/onboarding/{step}", {}, token=access_token)
        except urllib.error.HTTPError as e:
            print(f"WARN: onboarding/{step} returned {e.code} (continuing)", file=sys.stderr)

    long_lived = asyncio.run(_mint_long_lived(access_token))
    _write_env(long_lived, password)
    print(f"OK: HA onboarded; owner '{OWNER_USER}' created; long-lived token minted "
          f"({len(long_lived)} chars) and written to .env (HASS_TOKEN). Owner creds saved to .env.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
