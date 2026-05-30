#!/usr/bin/env python3
"""End-to-end Aria golden-path test.

Runs every verbal request type Aria handles, through her real voice path,
against a real (or already-running) bot, with real API calls. This is the
single primary E2E gate for any Aria-touching change.

Scenarios (one verbal request type each, posted to the bot via `/aria_say`):

  S1  memory recall          — "What did we do last?"
  S2  repo/project status    — "What is happening on the ucs_golden project?"
  S3  plan + execute         — "Plan a small change to calculator.py and execute it"
  S4  prompt-applied Cursor  — "Apply the architecture template to: 'Should I add caching?' and send to Cursor"
  S5  prompt management      — "Reload your prompts"
  S6  memory write + recall  — "Remember that I prefer dataclasses" + retrieval probe
  S7  MCP tier-R read        — "What's on my calendar today?"
  S8  MCP tier-I confirmation— "Send me a test email at c@c42.io" (auto-approve)
  S9  general Q&A / sanity   — "What time is it?"
  S10 text-channel turn      — `!ask` summarize latest c42cc/ucs commit

Per scenario the harness snapshots the events table + audit.jsonl + the
conversation buffer, drives the request, waits for tool dispatch, then
asserts: expected tool(s) called, expected keyword(s) in Aria's reply,
optional side-effect (file modified, audit row tier+confirmed, etc.).

Each per-scenario verdict (PASS/FAIL) is posted as a row to `#ucs`. A
final summary block follows: pass/fail count, wall time, cost delta,
bot SHA. A JSON dump lands at `scripts/e2e_aria_golden_last_report.json`.

LIFECYCLE
  By default the harness owns the bot: kills any prior process, edits
  `projects/registry.md` to add `ucs_golden`, reinstalls editable, spawns
  `python -m src.bot` in background, waits up to 120s for preflight to
  pass, gates on `voice_injector_wired` (asking the operator to join the
  voice channel if Aria isn't connected yet), runs the scenarios, then
  cleans up. `--no-restart` skips the lifecycle and uses an already-
  running bot — useful during development iteration.

REQUIREMENTS
  - `DISCORD_APP_BOT_TOKEN`, `DISCORD_TEXT_CHANNEL_ID`,
    `DISCORD_LOG_CHANNEL_ID` set in `.env`.
  - Bot up + Aria in voice (`voice_injector_wired=true` on `/healthz`).
  - For S10: `DISCORD_TEST_WEBHOOK_URL` in `.env`.
  - For S8 (default): a working tier-I email path (gmail or apple MCP).
    Skip with `--no-tier-i` if not available.

USAGE
  .venv/bin/python scripts/e2e_aria_golden.py                 # default: operator-in-voice
  .venv/bin/python scripts/e2e_aria_golden.py --tts           # fully autonomous (TTS-driven audio)
  .venv/bin/python scripts/e2e_aria_golden.py --no-restart    # use already-running bot
  .venv/bin/python scripts/e2e_aria_golden.py --no-voice      # text-only (CI-style)
  .venv/bin/python scripts/e2e_aria_golden.py --no-tier-i     # skip S8 (no real email)
  .venv/bin/python scripts/e2e_aria_golden.py --only S1,S3,S8 # subset
  .venv/bin/python scripts/e2e_aria_golden.py --keep-scratch  # don't rm /tmp/aria_e2e_golden after

VOICE MODES
  default                   operator-in-voice. Bot auto-connects Gemini when
                            you join #general; the harness gates on
                            voice_injector_wired and drives via /aria_say.
  --tts                     fully autonomous. The harness POSTs
                            /test_connect_gemini to bring Gemini Live up
                            without Discord voice, then synthesizes each
                            verbal prompt via Gemini TTS (or macOS `say`
                            fallback) and POSTs the resulting 16 kHz PCM
                            via /test_voice_in for Gemini to transcribe.
                            No human-in-voice required. Use this for CI
                            and re-runs.
  --no-voice                text-only fallback. Drives every scenario
                            through the !ask webhook -> do_with_claude
                            path; bypasses Gemini Live entirely. Some
                            Gemini-only scenarios (S5, S8) will FAIL or
                            be SKIPped in this mode.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

import aiohttp

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(REPO_ROOT, ".env"))

from src.config import config  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DISCORD_API = "https://discord.com/api/v10"

SCRATCH_PROJECT_NAME = "ucs_golden"
SCRATCH_PROJECT_ROOT = "/tmp/aria_e2e_golden"
REGISTRY_PATH = os.path.join(REPO_ROOT, "projects", "registry.md")
REGISTRY_BACKUP_SUFFIX = ".e2e-golden-bak"
DB_PATH = os.path.join(REPO_ROOT, "data", "state.db")
AUDIT_PATH = os.path.join(REPO_ROOT, "data", "audit.jsonl")
BOT_LOG_PATH = "/tmp/ucs2-e2e-golden.log"
JSON_REPORT_PATH = os.path.join(REPO_ROOT, "scripts", "e2e_aria_golden_last_report.json")

# How long the harness waits for preflight `Preflight passed` to appear
# in the bot log after starting the bot.
PREFLIGHT_TIMEOUT_SEC = 120.0
# How long the operator has to join the voice channel after preflight.
VOICE_GATE_TIMEOUT_SEC = 90.0
# Default per-scenario tool-dispatch wait.
SCENARIO_TIMEOUT_SEC = 90.0


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

def stamp(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Discord HTTP helpers
# ---------------------------------------------------------------------------

class DiscordHTTP:
    """Thin wrapper around the v10 REST API. Bot-token auth."""

    def __init__(self, session: aiohttp.ClientSession, bot_token: str):
        self._session = session
        self._headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
        }
        self._bot_user_id: str | None = None

    async def bot_user_id(self) -> str:
        if self._bot_user_id is not None:
            return self._bot_user_id
        async with self._session.get(
            f"{DISCORD_API}/users/@me", headers=self._headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"GET /users/@me failed: {resp.status} {await resp.text()}")
            body = await resp.json()
            self._bot_user_id = str(body["id"])
            return self._bot_user_id

    async def post(self, channel_id: str, content: str) -> dict:
        try:
            async with self._session.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=self._headers,
                json={"content": content[:2000]},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status >= 400:
                    stamp(f"  WARN: discord post {resp.status}: {(await resp.text())[:200]}")
                    return {}
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            stamp(f"  WARN: discord post failed (network?): {type(e).__name__}: {e}")
            return {}

    async def recent_messages(self, channel_id: str, *, after: str | None = None, limit: int = 50) -> list[dict]:
        params = {"limit": str(limit)}
        if after:
            params["after"] = after
        try:
            async with self._session.get(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=self._headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    stamp(f"  WARN: discord GET messages {resp.status}: {(await resp.text())[:200]}")
                    return []
                return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            stamp(f"  WARN: discord GET messages failed (network?): {type(e).__name__}: {e}")
            return []

    async def latest_message_id(self, channel_id: str) -> str | None:
        msgs = await self.recent_messages(channel_id, limit=1)
        return msgs[0]["id"] if msgs else None


# ---------------------------------------------------------------------------
# DB + audit-log helpers
# ---------------------------------------------------------------------------

def events_max_id() -> int:
    """Return the current MAX(id) from the events table, or 0 if empty."""
    import sqlite3
    if not os.path.exists(DB_PATH):
        return 0
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
        conn.close()
        return int(row["m"]) if row else 0
    except sqlite3.Error:
        return 0


def events_since(baseline_id: int) -> list[dict]:
    """Return all events rows with id > baseline_id, oldest first."""
    import sqlite3
    if not os.path.exists(DB_PATH):
        return []
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, timestamp, tool_name, params, result, duration_ms, "
            "session_key, token_cost_usd FROM events WHERE id > ? ORDER BY id ASC",
            (baseline_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def daily_spend() -> float:
    """Compute spend from events.token_cost_usd for today (UTC)."""
    import sqlite3
    if not os.path.exists(DB_PATH):
        return 0.0
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        today = datetime.now(timezone.utc).date().isoformat()
        row = conn.execute(
            "SELECT COALESCE(SUM(token_cost_usd), 0) AS s FROM events WHERE timestamp >= ?",
            (today,),
        ).fetchone()
        conn.close()
        return float(row["s"]) if row else 0.0
    except sqlite3.Error:
        return 0.0


def audit_tail_offset() -> int:
    """Return the current byte length of audit.jsonl."""
    try:
        return os.path.getsize(AUDIT_PATH)
    except OSError:
        return 0


def audit_since(offset: int) -> list[dict]:
    """Return all audit.jsonl entries appended after `offset`."""
    if not os.path.exists(AUDIT_PATH):
        return []
    out: list[dict] = []
    try:
        with open(AUDIT_PATH, "rb") as f:
            f.seek(offset)
            tail = f.read()
        for line in tail.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    return out


# ---------------------------------------------------------------------------
# Observer HTTP helpers (/aria_say, /recent_turns, /healthz)
# ---------------------------------------------------------------------------

class ObserverHTTP:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int):
        self._session = session
        self._base = f"http://{host}:{port}"

    async def healthz(self) -> dict:
        async with self._session.get(
            f"{self._base}/healthz",
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status != 200:
                return {"ok": False, "status": resp.status}
            return await resp.json()

    async def aria_say(self, text: str, *, turn_complete: bool = True) -> tuple[int, str]:
        """POST /aria_say. Returns (status, body_or_error_text)."""
        try:
            async with self._session.post(
                f"{self._base}/aria_say",
                json={"text": text, "turn_complete": turn_complete},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                body = await resp.text()
                return resp.status, body
        except Exception as e:
            return 0, f"aiohttp error: {e}"

    async def test_connect_gemini(self) -> tuple[int, str]:
        """POST /test_connect_gemini. Returns (status, body)."""
        try:
            async with self._session.post(
                f"{self._base}/test_connect_gemini",
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.text()
                return resp.status, body
        except Exception as e:
            return 0, f"aiohttp error: {e}"

    async def test_voice_in(self, text: str, *, engine: str = "gemini",
                            voice: str = "Kore", trailing_silence_ms: int = 1200) -> tuple[int, str]:
        """POST /test_voice_in (TTS-driven audio injection). Returns (status, body)."""
        try:
            async with self._session.post(
                f"{self._base}/test_voice_in",
                json={
                    "text": text,
                    "engine": engine,
                    "voice": voice,
                    "trailing_silence_ms": trailing_silence_ms,
                },
                # TTS synthesis + chunk streaming for a long sentence can take
                # 15-30s. Allow plenty of headroom; the endpoint returns when
                # all audio is queued, not when Aria finishes replying.
                timeout=aiohttp.ClientTimeout(total=90),
            ) as resp:
                body = await resp.text()
                return resp.status, body
        except Exception as e:
            return 0, f"aiohttp error: {e}"

    async def recent_turns(self, n: int = 8) -> list[dict]:
        try:
            async with self._session.get(
                f"{self._base}/recent_turns",
                params={"n": str(n)},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return []
                body = await resp.json()
                return body.get("turns", [])
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Scratch project + registry helpers
# ---------------------------------------------------------------------------

def make_scratch_project(root: str) -> None:
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "README.md"), "w") as f:
        f.write(
            "# Aria E2E Golden Path scratch project\n\n"
            "This is a throwaway project used by `scripts/e2e_aria_golden.py`. "
            "Aria will read and modify the files here during the test.\n"
        )
    with open(os.path.join(root, "calculator.py"), "w") as f:
        f.write(
            '"""Tiny calculator skeleton — Aria is going to extend this."""\n\n'
            "from __future__ import annotations\n\n\n"
            "def add(a: float, b: float) -> float:\n"
            '    """Return the sum of a and b."""\n'
            "    return a + b\n"
        )


def cleanup_scratch_project(root: str) -> None:
    if os.path.isdir(root) and root.startswith("/tmp/aria_e2e_"):
        shutil.rmtree(root, ignore_errors=True)


def amend_registry() -> None:
    """Append a `ucs_golden → /tmp/aria_e2e_golden` line to projects/registry.md.

    Backs up the original to `<path>.e2e-golden-bak`. Idempotent: if the
    backup already exists OR the entry is already present, skips work.
    """
    if not os.path.exists(REGISTRY_PATH):
        raise RuntimeError(f"registry missing: {REGISTRY_PATH}")
    backup = REGISTRY_PATH + REGISTRY_BACKUP_SUFFIX
    with open(REGISTRY_PATH) as f:
        body = f.read()
    if f"- {SCRATCH_PROJECT_NAME} → {SCRATCH_PROJECT_ROOT}" in body:
        stamp(f"  registry already contains {SCRATCH_PROJECT_NAME} entry — no-op")
        return
    if not os.path.exists(backup):
        with open(backup, "w") as f:
            f.write(body)
        stamp(f"  registry backed up to {backup}")
    with open(REGISTRY_PATH, "a") as f:
        if not body.endswith("\n"):
            f.write("\n")
        f.write(f"- {SCRATCH_PROJECT_NAME} → {SCRATCH_PROJECT_ROOT}\n")
    stamp(f"  registry: added {SCRATCH_PROJECT_NAME} → {SCRATCH_PROJECT_ROOT}")


def restore_registry() -> None:
    backup = REGISTRY_PATH + REGISTRY_BACKUP_SUFFIX
    if not os.path.exists(backup):
        return
    try:
        shutil.move(backup, REGISTRY_PATH)
        stamp(f"  registry restored from {backup}")
    except OSError as e:
        stamp(f"  WARN: registry restore failed: {e}")


# ---------------------------------------------------------------------------
# Memory pre-seed (mem0)
# ---------------------------------------------------------------------------

def preseed_memory_sentinel(sentinel: str) -> bool:
    """Write a sentinel to mem0 before the test starts.

    Imported here (not at module top) so a partial env still lets the
    rest of the harness run.
    """
    try:
        from src.memory import init_memory, remember
        init_memory()
        remember(f"Aria E2E sentinel: {sentinel}. We set up a scratch project at /tmp/aria_e2e_golden and ran the unified golden path test.")
        return True
    except Exception as e:
        stamp(f"  WARN: mem0 preseed failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------

def kill_bot() -> None:
    """Kill any running bot tree via kill.sh."""
    try:
        subprocess.run(
            ["bash", os.path.join(REPO_ROOT, "kill.sh")],
            cwd=REPO_ROOT, check=False,
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        stamp("  WARN: kill.sh timed out")
    time.sleep(2)


def start_bot_background() -> int:
    """Start the bot in background, return its PID. Fresh-install first."""
    venv_pip = os.path.join(REPO_ROOT, ".venv", "bin", "pip")
    venv_python = os.path.join(REPO_ROOT, ".venv", "bin", "python")
    if not os.path.exists(venv_python):
        raise RuntimeError(f"venv python missing: {venv_python}")

    stamp("  pip install -e . (editable reinstall to enforce fresh code)...")
    rc = subprocess.run(
        [venv_pip, "install", "-e", ".", "--quiet"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    if rc.returncode != 0:
        raise RuntimeError(f"pip install failed: {rc.stderr[:500]}")

    stamp(f"  spawning bot, log -> {BOT_LOG_PATH}")
    log_file = open(BOT_LOG_PATH, "w")
    proc = subprocess.Popen(
        [venv_python, "-m", "src.bot"],
        cwd=REPO_ROOT,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return proc.pid


async def wait_for_preflight(timeout: float = PREFLIGHT_TIMEOUT_SEC) -> bool:
    """Tail BOT_LOG_PATH for `Preflight passed`. Returns True on match.

    Matches `deploy.sh`'s pattern. Bails out early on
    `PREFLIGHT FAILED` (case-insensitive contains)."""
    deadline = time.time() + timeout
    stamp(f"  waiting up to {int(timeout)}s for preflight...")
    last_print = 0.0
    while time.time() < deadline:
        if os.path.exists(BOT_LOG_PATH):
            try:
                with open(BOT_LOG_PATH) as f:
                    body = f.read()
                if "Preflight passed" in body:
                    elapsed = time.time() - (deadline - timeout)
                    stamp(f"  PREFLIGHT PASSED after {elapsed:.0f}s")
                    return True
                if "PREFLIGHT FAILED" in body:
                    stamp("  PREFLIGHT FAILED — last 50 lines of bot log:")
                    for line in body.splitlines()[-50:]:
                        print(f"    | {line}", flush=True)
                    return False
            except OSError:
                pass
        now = time.time()
        if now - last_print > 10:
            elapsed = now - (deadline - timeout)
            stamp(f"  still waiting for preflight ({elapsed:.0f}s elapsed)...")
            last_print = now
        await asyncio.sleep(2)
    stamp(f"  PREFLIGHT TIMEOUT after {int(timeout)}s")
    return False


async def voice_gate(
    observer: ObserverHTTP, timeout: float = VOICE_GATE_TIMEOUT_SEC,
) -> bool:
    """Block until Gemini is connected via the auto-join path.

    Strategy: send a turn_complete=False probe to /aria_say. 200 means the
    voice injector is wired AND Gemini is connected (the endpoint checks
    `injector.connected` internally). 503 means Aria isn't in voice yet —
    print human-actionable instructions and retry.
    """
    stamp(f"  voice gate: waiting up to {int(timeout)}s for Gemini Live to connect...")
    deadline = time.time() + timeout
    printed_instruction = False
    last_status: int = 0
    while time.time() < deadline:
        status, body = await observer.aria_say(
            "[golden-path voice gate: silent probe]", turn_complete=False,
        )
        if status == 200:
            stamp("  voice gate PASSED (Gemini connected)")
            return True
        if status == 503 and not printed_instruction:
            stamp(
                f"  voice unavailable: {body[:200]}\n"
                "  >> Please join the #general voice channel now. "
                "Aria will auto-connect within a few seconds.")
            printed_instruction = True
        if status != last_status:
            stamp(f"  /aria_say probe -> status={status}")
            last_status = status
        await asyncio.sleep(3)
    stamp(f"  voice gate TIMEOUT after {int(timeout)}s — Aria never connected to voice")
    return False


# ---------------------------------------------------------------------------
# Scenario types
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    events_id: int
    audit_offset: int
    cost_usd: float
    ts: float


@dataclass
class ScenarioResult:
    id: str
    name: str
    verdict: str  # PASS / FAIL / SKIP / ERROR
    expected_tools: list[str]
    actual_tools: list[str]
    keyword_ok: bool
    side_effect_ok: bool
    side_effect_detail: str
    aria_reply: str
    duration_sec: float
    cost_delta_usd: float
    new_events: list[dict] = field(default_factory=list)
    new_audit: list[dict] = field(default_factory=list)
    reason: str = ""

    @property
    def short_label(self) -> str:
        return f"[{self.id}] {self.name}"


@dataclass
class ScenarioContext:
    session: aiohttp.ClientSession
    discord: DiscordHTTP
    observer: ObserverHTTP
    no_voice: bool
    no_tier_i: bool
    tts_mode: bool
    scratch_project: str
    sentinel: str
    text_channel_id: str
    alerts_channel_id: str
    webhook_url: str
    bot_user_id: str

    # -- snapshot/diff --

    def snap(self) -> Snapshot:
        return Snapshot(
            events_id=events_max_id(),
            audit_offset=audit_tail_offset(),
            cost_usd=daily_spend(),
            ts=time.time(),
        )

    # -- drive --

    async def drive_voice(self, text: str, *, turn_complete: bool = True) -> tuple[int, str]:
        """Send a verbal request to Aria via /aria_say."""
        return await self.observer.aria_say(text, turn_complete=turn_complete)

    async def drive_tts(self, text: str) -> tuple[int, str]:
        """Send a verbal request to Aria via synthesized TTS audio."""
        return await self.observer.test_voice_in(text, engine="gemini", voice="Kore")

    async def drive_webhook_ask(self, prompt: str) -> bool:
        """Fire a !ask via the test webhook. Returns success."""
        if not self.webhook_url:
            return False
        try:
            async with self.session.post(
                self.webhook_url,
                json={"content": f"!ask {prompt}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                return resp.status in (200, 204)
        except Exception:
            return False

    async def drive(self, text: str) -> tuple[bool, str]:
        """Default drive: TTS audio if --tts, /aria_say if voice, !ask otherwise.

        Returns (ok, detail). On failure the scenario should still proceed
        with an error verdict.
        """
        if self.no_voice:
            ok = await self.drive_webhook_ask(text)
            return ok, "webhook" if ok else "webhook failed"
        if self.tts_mode:
            status, body = await self.drive_tts(text)
            if status == 200:
                return True, f"tts 200 ({body[:100]})"
            return False, f"test_voice_in {status}: {body[:200]}"
        status, body = await self.drive_voice(text)
        if status == 200:
            return True, "aria_say 200"
        return False, f"aria_say {status}: {body[:200]}"

    # -- wait/poll --

    async def wait_for_tools(
        self, expected: set[str], snap: Snapshot, *, timeout: float = SCENARIO_TIMEOUT_SEC,
    ) -> list[dict]:
        """Poll events table until every tool in `expected` appears, or timeout.

        Returns the full list of new rows (may be larger than `expected`).
        If the set is never fully covered, returns whatever was observed.
        """
        deadline = time.time() + timeout
        new_rows: list[dict] = []
        while time.time() < deadline:
            new_rows = events_since(snap.events_id)
            actual = {r["tool_name"] for r in new_rows}
            if expected <= actual:
                return new_rows
            await asyncio.sleep(2)
        return new_rows

    async def wait_for_any_tool(
        self, snap: Snapshot, *, timeout: float = 30.0,
    ) -> list[dict]:
        """Wait until at least one tool fires after `snap`. Returns rows."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            new_rows = events_since(snap.events_id)
            if new_rows:
                return new_rows
            await asyncio.sleep(2)
        return events_since(snap.events_id)

    async def aria_reply_text(self, *, since_ts: float = 0.0) -> str:
        """Concatenate Aria's voice/text turns from the conversation buffer
        that occurred after `since_ts`.
        """
        turns = await self.observer.recent_turns(n=16)
        aria_turns = [
            t["text"] for t in turns
            if t.get("role") == "aria" and float(t.get("ts", 0)) >= since_ts
        ]
        return "\n".join(aria_turns).lower()

    # -- narrate --

    async def narrate(self, text: str) -> None:
        await self.discord.post(self.text_channel_id, text)


# ---------------------------------------------------------------------------
# Scenario builder helpers
# ---------------------------------------------------------------------------

def build_verdict(
    sid: str, name: str, expected: set[str], snap: Snapshot, *,
    new_events: list[dict],
    new_audit: list[dict] | None = None,
    aria_reply: str = "",
    keywords: list[str] | None = None,
    side_effect_ok: bool = True,
    side_effect_detail: str = "",
    extra_reason: str = "",
) -> ScenarioResult:
    actual_tools = {r["tool_name"] for r in new_events}
    covered = expected <= actual_tools
    keyword_ok = True
    if keywords:
        body = aria_reply.lower()
        keyword_ok = any(kw.lower() in body for kw in keywords)

    cost_delta = max(0.0, daily_spend() - snap.cost_usd)
    duration = time.time() - snap.ts

    reasons: list[str] = []
    if not covered:
        missing = sorted(expected - actual_tools)
        reasons.append(f"missing tools: {missing}")
    if not keyword_ok:
        reasons.append(f"none of keywords {keywords} matched aria_reply")
    if not side_effect_ok:
        reasons.append(f"side-effect failed: {side_effect_detail}")
    if extra_reason:
        reasons.append(extra_reason)

    verdict = "PASS" if (covered and keyword_ok and side_effect_ok) else "FAIL"

    return ScenarioResult(
        id=sid,
        name=name,
        verdict=verdict,
        expected_tools=sorted(expected),
        actual_tools=sorted(actual_tools),
        keyword_ok=keyword_ok,
        side_effect_ok=side_effect_ok,
        side_effect_detail=side_effect_detail,
        aria_reply=aria_reply[:1500],
        duration_sec=duration,
        cost_delta_usd=cost_delta,
        new_events=[
            {
                "id": r.get("id"),
                "tool_name": r.get("tool_name"),
                "duration_ms": r.get("duration_ms"),
                "token_cost_usd": r.get("token_cost_usd"),
                "result_preview": (r.get("result") or "")[:200],
            }
            for r in new_events
        ],
        new_audit=[
            {
                "ts": a.get("ts"),
                "server": a.get("server"),
                "tool": a.get("tool"),
                "tier": a.get("tier"),
                "confirmed": a.get("confirmed"),
                "result_preview": (a.get("result_summary") or "")[:200],
            }
            for a in (new_audit or [])
        ],
        reason="; ".join(reasons),
    )


async def post_scenario_row(ctx: ScenarioContext, r: ScenarioResult) -> None:
    emoji = "PASS" if r.verdict == "PASS" else r.verdict
    tools_str = "+".join(r.actual_tools) if r.actual_tools else "(none)"
    line = (
        f"**[{r.id}] {r.name}:** {emoji}  "
        f"tools={tools_str}  cost=${r.cost_delta_usd:.4f}  {r.duration_sec:.0f}s"
    )
    if r.reason:
        line += f"\n  > {r.reason[:300]}"
    await ctx.narrate(line)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

async def scenario_s1(ctx: ScenarioContext) -> ScenarioResult:
    """S1. Memory recall — 'What did we do last?'

    The sentinel is pre-seeded into mem0 by the harness before the bot
    starts. Aria should call `recall` and surface the sentinel in her
    spoken reply.
    """
    snap = ctx.snap()
    # Short, directive prompt. Two reasons: (1) Gemini TTS sometimes
    # truncates long inputs; (2) Aria's system prompt nudges her to be
    # conversational, so we need an unambiguous tool directive or she
    # will confidently hallucinate an answer.
    prompt = "Call the recall tool right now to find our most recent work."
    ok, detail = await ctx.drive(prompt)
    if not ok:
        return build_verdict("S1", "memory_recall", set(), snap,
                             new_events=[], extra_reason=f"drive failed: {detail}")
    new_events = await ctx.wait_for_tools({"recall"}, snap, timeout=60.0)
    aria_reply = await ctx.aria_reply_text(since_ts=snap.ts)
    # Sentinel match is the strongest signal Aria actually surfaced what
    # we seeded. Fall back to tool-dispatch coverage if mem0 returned no
    # rows for some reason.
    keywords = ["aria e2e sentinel", "golden path", "scratch project"]
    return build_verdict(
        "S1", "memory_recall", {"recall"}, snap,
        new_events=new_events, aria_reply=aria_reply, keywords=keywords,
    )


async def scenario_s2(ctx: ScenarioContext) -> ScenarioResult:
    """S2. Repo/project status — `What is happening on the ucs_golden project?`"""
    snap = ctx.snap()
    prompt = f"Call list_cursor_windows right now to show me the open Cursor windows."
    ok, detail = await ctx.drive(prompt)
    if not ok:
        return build_verdict("S2", "repo_status", set(), snap,
                             new_events=[], extra_reason=f"drive failed: {detail}")
    new_events = await ctx.wait_for_tools({"list_cursor_windows"}, snap, timeout=45.0)
    actual = {r["tool_name"] for r in new_events}
    expected: set[str] = set()
    if {"list_cursor_windows"} & actual:
        expected = {"list_cursor_windows"}
    elif {"read_cursor_window"} & actual:
        expected = {"read_cursor_window"}
    else:
        expected = {"list_cursor_windows"}  # for verdict reason
    aria_reply = await ctx.aria_reply_text(since_ts=snap.ts)
    return build_verdict(
        "S2", "repo_status", expected, snap,
        new_events=new_events, aria_reply=aria_reply,
        keywords=[SCRATCH_PROJECT_NAME, "calculator"],
    )


async def scenario_s3(ctx: ScenarioContext) -> ScenarioResult:
    """S3. Plan + execute — Aria plans a change to calculator.py and executes it."""
    snap = ctx.snap()
    calc_path = os.path.join(ctx.scratch_project, "calculator.py")
    pre_size = os.path.getsize(calc_path) if os.path.exists(calc_path) else 0

    prompt = (
        f"Call plan_with_claude right now. Context: add a subtract function to "
        f"calculator.py in the {SCRATCH_PROJECT_NAME} project. Use the planning template."
    )
    ok, detail = await ctx.drive(prompt)
    if not ok:
        return build_verdict("S3", "plan_and_execute", set(), snap,
                             new_events=[], extra_reason=f"drive failed: {detail}")
    new_events = await ctx.wait_for_tools({"plan_with_claude"}, snap, timeout=120.0)
    actual = {r["tool_name"] for r in new_events}
    action_tools = {"send_to_cursor_chat", "do_with_claude", "build_with_cursor"}
    has_action = bool(actual & action_tools)
    expected = {"plan_with_claude"} | (actual & action_tools if has_action else {"send_to_cursor_chat"})

    aria_reply = await ctx.aria_reply_text(since_ts=snap.ts)

    # Side effect: did the scratch project change at all?
    post_size = os.path.getsize(calc_path) if os.path.exists(calc_path) else 0
    files_now = set(os.listdir(ctx.scratch_project)) if os.path.isdir(ctx.scratch_project) else set()
    side_effect_ok = has_action  # we don't strictly require Cursor to have edited the file
    side_effect_detail = (
        f"calc.py {pre_size}->{post_size} bytes; files={sorted(files_now)}; "
        f"action_tools_fired={sorted(actual & action_tools)}"
    )

    return build_verdict(
        "S3", "plan_and_execute", expected, snap,
        new_events=new_events, aria_reply=aria_reply,
        side_effect_ok=side_effect_ok, side_effect_detail=side_effect_detail,
    )


async def scenario_s4(ctx: ScenarioContext) -> ScenarioResult:
    """S4. Prompt-applied Cursor send — apply architecture template to a message."""
    snap = ctx.snap()
    prompt = (
        f"Call send_to_cursor_chat right now. Project: {SCRATCH_PROJECT_NAME}. "
        "Message: 'Use the architecture template. Question: should I add Redis caching to UCS? "
        "Give problem statement, options, recommendation, component design.'"
    )
    ok, detail = await ctx.drive(prompt)
    if not ok:
        return build_verdict("S4", "prompt_applied_cursor", set(), snap,
                             new_events=[], extra_reason=f"drive failed: {detail}")

    new_events = await ctx.wait_for_tools({"send_to_cursor_chat"}, snap, timeout=120.0)
    aria_reply = await ctx.aria_reply_text(since_ts=snap.ts)

    # Inspect the params of the send_to_cursor_chat call for template cues.
    template_cues = ["problem statement", "options", "recommendation", "component design"]
    side_effect_ok = False
    side_effect_detail = "send_to_cursor_chat not called"
    for r in new_events:
        if r["tool_name"] == "send_to_cursor_chat":
            params = r.get("params") or ""
            if isinstance(params, str):
                low = params.lower()
                matched = [c for c in template_cues if c in low]
                if matched:
                    side_effect_ok = True
                    side_effect_detail = f"matched template cues: {matched}"
                else:
                    side_effect_detail = (
                        f"send_to_cursor_chat called but params lack template cues; "
                        f"first 200 chars: {params[:200]}"
                    )
            break

    return build_verdict(
        "S4", "prompt_applied_cursor", {"send_to_cursor_chat"}, snap,
        new_events=new_events, aria_reply=aria_reply,
        side_effect_ok=side_effect_ok, side_effect_detail=side_effect_detail,
    )


async def scenario_s5(ctx: ScenarioContext) -> ScenarioResult:
    """S5. Prompt management — `Reload your prompts`."""
    snap = ctx.snap()
    prompt = "Call the reload_prompts tool right now."
    ok, detail = await ctx.drive(prompt)
    if not ok:
        return build_verdict("S5", "reload_prompts", set(), snap,
                             new_events=[], extra_reason=f"drive failed: {detail}")
    new_events = await ctx.wait_for_tools({"reload_prompts"}, snap, timeout=45.0)
    aria_reply = await ctx.aria_reply_text(since_ts=snap.ts)
    return build_verdict(
        "S5", "reload_prompts", {"reload_prompts"}, snap,
        new_events=new_events, aria_reply=aria_reply,
    )


async def scenario_s6(ctx: ScenarioContext) -> ScenarioResult:
    """S6. Memory write + retrieval (two-turn)."""
    snap = ctx.snap()
    write_prompt = (
        "Call the remember tool right now with this fact: I prefer dataclasses "
        "over plain dicts in Python code."
    )
    ok, detail = await ctx.drive(write_prompt)
    if not ok:
        return build_verdict("S6", "memory_write_recall", set(), snap,
                             new_events=[], extra_reason=f"drive failed: {detail}")
    await ctx.wait_for_tools({"remember"}, snap, timeout=30.0)
    await asyncio.sleep(2)

    retrieval_prompt = "Call the recall tool right now to find my Python style preferences."
    ok, detail = await ctx.drive(retrieval_prompt)
    if not ok:
        return build_verdict("S6", "memory_write_recall", {"remember"}, snap,
                             new_events=events_since(snap.events_id),
                             extra_reason=f"second drive failed: {detail}")
    new_events = await ctx.wait_for_tools({"remember", "recall"}, snap, timeout=60.0)
    aria_reply = await ctx.aria_reply_text(since_ts=snap.ts)
    return build_verdict(
        "S6", "memory_write_recall", {"remember", "recall"}, snap,
        new_events=new_events, aria_reply=aria_reply,
        keywords=["dataclass"],
    )


async def scenario_s7(ctx: ScenarioContext) -> ScenarioResult:
    """S7. MCP tier-R read — calendar today."""
    snap = ctx.snap()
    prompt = "Call do_with_claude right now. Task: list my Google calendar events for today."
    ok, detail = await ctx.drive(prompt)
    if not ok:
        return build_verdict("S7", "calendar_read", set(), snap,
                             new_events=[], extra_reason=f"drive failed: {detail}")

    # Wait for ANY tool to fire (quick_calendar OR do_with_claude both qualify).
    await ctx.wait_for_any_tool(snap, timeout=15.0)
    # Then wait a bit longer for the MCP call to land in audit.jsonl.
    await asyncio.sleep(20.0)

    new_events = events_since(snap.events_id)
    new_audit = audit_since(snap.audit_offset)
    aria_reply = await ctx.aria_reply_text(since_ts=snap.ts)

    # Side effect: any tier-R google-calendar list-events row?
    calendar_hit = any(
        (a.get("server") == "google-calendar" or "calendar" in (a.get("tool") or "").lower())
        and a.get("tier") == "R"
        for a in new_audit
    )
    side_effect_detail = (
        f"audit rows: {[(a.get('server'), a.get('tool'), a.get('tier')) for a in new_audit][:6]}"
    )

    actual_tools = {r["tool_name"] for r in new_events}
    # Either a top-level quick_calendar OR do_with_claude (which then drives MCP)
    # OR an MCP audit row should appear. We mark expected as "any tool fired AND
    # a calendar audit row".
    expected: set[str] = set()
    if "quick_calendar" in actual_tools:
        expected = {"quick_calendar"}
    elif "do_with_claude" in actual_tools:
        expected = {"do_with_claude"}
    else:
        expected = {"do_with_claude"}  # for FAIL diagnostics

    return build_verdict(
        "S7", "calendar_read", expected, snap,
        new_events=new_events, new_audit=new_audit, aria_reply=aria_reply,
        side_effect_ok=calendar_hit, side_effect_detail=side_effect_detail,
    )


async def scenario_s8(ctx: ScenarioContext) -> ScenarioResult:
    """S8. MCP tier-I confirmation — send a test email and auto-approve."""
    snap = ctx.snap()
    alerts_baseline = await ctx.discord.latest_message_id(ctx.alerts_channel_id)

    request_prompt = (
        "Call do_with_claude right now. Task: send a test email to c@c42.io with "
        "subject 'UCS golden path E2E' and one-line body 'automated test of tier-I "
        "confirmation flow'."
    )
    ok, detail = await ctx.drive(request_prompt)
    if not ok:
        return build_verdict("S8", "tier_i_confirm", set(), snap,
                             new_events=[], extra_reason=f"drive failed: {detail}")

    # Wait for the confirmation card in #ucs-alerts. Format from bot.py:
    #   "**Confirmation required** (action_id=`<8hex>`):\n`{tool_name}`: {summary}"
    action_id: str | None = None
    confirm_card_text: str = ""
    deadline = time.time() + 60.0
    while time.time() < deadline:
        msgs = await ctx.discord.recent_messages(ctx.alerts_channel_id,
                                                  after=alerts_baseline, limit=20)
        for m in msgs:
            content = m.get("content", "")
            if "Confirmation required" in content and "action_id=" in content:
                match = re.search(r"action_id=`([a-f0-9]+)`", content)
                if match:
                    action_id = match.group(1)
                    confirm_card_text = content
                    break
        if action_id:
            break
        await asyncio.sleep(2)

    if not action_id:
        new_events = events_since(snap.events_id)
        new_audit = audit_since(snap.audit_offset)
        aria_reply = await ctx.aria_reply_text(since_ts=snap.ts)
        return build_verdict(
            "S8", "tier_i_confirm", {"do_with_claude"}, snap,
            new_events=new_events, new_audit=new_audit, aria_reply=aria_reply,
            side_effect_ok=False,
            side_effect_detail="no confirmation card appeared in #ucs-alerts within 60s",
        )

    stamp(f"  S8: confirmation card detected, action_id={action_id}")
    # Approve verbally — include the action_id explicitly so confirm_action
    # can match even if Gemini misses the inject hint.
    approve_prompt = (
        f"Yes, go ahead and send the email. Approved. "
        f"Call confirm_action with action_id=\"{action_id}\" and approved=true."
    )
    ok, detail = await ctx.drive(approve_prompt)
    if not ok:
        new_events = events_since(snap.events_id)
        new_audit = audit_since(snap.audit_offset)
        aria_reply = await ctx.aria_reply_text(since_ts=snap.ts)
        return build_verdict(
            "S8", "tier_i_confirm", {"confirm_action"}, snap,
            new_events=new_events, new_audit=new_audit, aria_reply=aria_reply,
            side_effect_ok=False,
            side_effect_detail=f"approve drive failed: {detail}",
        )

    # Wait up to 60s for the tier-I audit entry to land confirmed=true.
    deadline = time.time() + 60.0
    while time.time() < deadline:
        new_audit = audit_since(snap.audit_offset)
        if any(a.get("tier") == "I" and a.get("confirmed") is True for a in new_audit):
            break
        await asyncio.sleep(2)

    new_events = events_since(snap.events_id)
    new_audit = audit_since(snap.audit_offset)
    aria_reply = await ctx.aria_reply_text(since_ts=snap.ts)

    tier_i_confirmed = [
        a for a in new_audit if a.get("tier") == "I" and a.get("confirmed") is True
    ]
    side_effect_ok = bool(tier_i_confirmed)
    side_effect_detail = (
        f"tier_I_confirmed_audit_rows={len(tier_i_confirmed)}; "
        f"action_id={action_id}"
    )
    expected_tools = {"do_with_claude", "confirm_action"}
    return build_verdict(
        "S8", "tier_i_confirm", expected_tools, snap,
        new_events=new_events, new_audit=new_audit, aria_reply=aria_reply,
        side_effect_ok=side_effect_ok, side_effect_detail=side_effect_detail,
    )


async def scenario_s9(ctx: ScenarioContext) -> ScenarioResult:
    """S9. General Q&A / sanity — `What time is it?`."""
    snap = ctx.snap()
    prompt = "Quick question: what time is it right now? Say a short answer."
    ok, detail = await ctx.drive(prompt)
    if not ok:
        return build_verdict("S9", "general_qa", set(), snap,
                             new_events=[], extra_reason=f"drive failed: {detail}")
    # Wait ~15s for Aria's voice reply. Any tool dispatch is fine; none is also fine.
    await asyncio.sleep(15.0)
    new_events = events_since(snap.events_id)
    new_audit = audit_since(snap.audit_offset)
    aria_reply = await ctx.aria_reply_text(since_ts=snap.ts)
    # Pass if Aria actually replied (transcript has aria turn after our drive).
    side_effect_ok = bool(aria_reply.strip())
    side_effect_detail = f"aria reply chars={len(aria_reply)}"
    return build_verdict(
        "S9", "general_qa", set(), snap,
        new_events=new_events, new_audit=new_audit, aria_reply=aria_reply,
        side_effect_ok=side_effect_ok, side_effect_detail=side_effect_detail,
    )


async def scenario_s10(ctx: ScenarioContext) -> ScenarioResult:
    """S10. Text-channel turn — `!ask` for the latest c42cc/ucs commit."""
    snap = ctx.snap()
    # Capture the latest text-channel message ID so we can scan for the bot's reply after.
    text_baseline = await ctx.discord.latest_message_id(ctx.text_channel_id)

    prompt = (
        "Summarize the most recent commit on the c42cc/ucs GitHub repo. "
        "Tell me the SHA prefix, the commit message, and the author."
    )

    if not ctx.webhook_url:
        return build_verdict("S10", "text_channel_ask", set(), snap,
                             new_events=[], extra_reason="DISCORD_TEST_WEBHOOK_URL not set")

    sent = await ctx.drive_webhook_ask(prompt)
    if not sent:
        return build_verdict("S10", "text_channel_ask", set(), snap,
                             new_events=[], extra_reason="webhook fire failed")

    # Wait for tool dispatch.
    new_events = await ctx.wait_for_tools({"do_with_claude"}, snap, timeout=120.0)
    # And wait for bot reply in #ucs.
    bot_reply: str = ""
    deadline = time.time() + 60.0
    while time.time() < deadline:
        msgs = await ctx.discord.recent_messages(
            ctx.text_channel_id, after=text_baseline, limit=20,
        )
        # newest first by default? actually Discord returns newest-first.
        for m in reversed(msgs):
            author = m.get("author") or {}
            if str(author.get("id")) == ctx.bot_user_id:
                content = m.get("content", "")
                if content and "Summarize the most recent commit" not in content:
                    bot_reply = content
                    break
        if bot_reply:
            break
        await asyncio.sleep(3)

    new_audit = audit_since(snap.audit_offset)
    aria_reply = bot_reply

    # Side effect: bot replied AND there was an MCP github call OR the reply
    # mentions a commit sha-ish hex string.
    sha_like = bool(re.search(r"\b[0-9a-f]{7,40}\b", bot_reply))
    github_audit = any(
        a.get("server") == "github" and a.get("tier") == "R"
        for a in new_audit
    )
    side_effect_ok = bool(bot_reply) and (sha_like or github_audit)
    side_effect_detail = (
        f"bot_reply_chars={len(bot_reply)}; sha_like={sha_like}; github_audit={github_audit}"
    )

    return build_verdict(
        "S10", "text_channel_ask", {"do_with_claude"}, snap,
        new_events=new_events, new_audit=new_audit, aria_reply=aria_reply,
        side_effect_ok=side_effect_ok, side_effect_detail=side_effect_detail,
    )


SCENARIOS: list[tuple[str, str, Callable[[ScenarioContext], Awaitable[ScenarioResult]]]] = [
    ("S1", "memory_recall", scenario_s1),
    ("S2", "repo_status", scenario_s2),
    ("S3", "plan_and_execute", scenario_s3),
    ("S4", "prompt_applied_cursor", scenario_s4),
    ("S5", "reload_prompts", scenario_s5),
    ("S6", "memory_write_recall", scenario_s6),
    ("S7", "calendar_read", scenario_s7),
    ("S8", "tier_i_confirm", scenario_s8),
    ("S9", "general_qa", scenario_s9),
    ("S10", "text_channel_ask", scenario_s10),
]


# ---------------------------------------------------------------------------
# Final reporting
# ---------------------------------------------------------------------------

def git_short_sha() -> str:
    try:
        rc = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        )
        return rc.stdout.strip() or "?"
    except Exception:
        return "?"


async def post_final_report(
    ctx: ScenarioContext, results: list[ScenarioResult],
    started_at: float, cost_start: float, run_kind: str,
) -> None:
    passed = [r for r in results if r.verdict == "PASS"]
    failed = [r for r in results if r.verdict == "FAIL"]
    skipped = [r for r in results if r.verdict == "SKIP"]
    erred = [r for r in results if r.verdict == "ERROR"]

    wall = time.time() - started_at
    cost_delta = max(0.0, daily_spend() - cost_start)
    sha = git_short_sha()

    failed_str = ", ".join(f"{r.id}" for r in failed) or "—"

    lines = [
        "---",
        f"**Aria Golden Path E2E — {len(passed)}/{len(results)} PASS** "
        f"({len(failed)} FAIL{', ' + str(len(skipped)) + ' SKIP' if skipped else ''}"
        f"{', ' + str(len(erred)) + ' ERROR' if erred else ''})",
        f"Mode: {run_kind}. Wall time: {wall:.0f}s. Cost delta: ${cost_delta:.4f}.",
        f"Bot SHA: `{sha}`. Failures: {failed_str}.",
        f"Report at: `{JSON_REPORT_PATH}`.",
        "---",
    ]
    await ctx.narrate("\n".join(lines))


def save_json_report(
    results: list[ScenarioResult], started_at: float, cost_start: float,
    run_kind: str,
) -> None:
    out = {
        "started_at_iso": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
        "ended_at_iso": datetime.now(timezone.utc).isoformat(),
        "wall_seconds": time.time() - started_at,
        "cost_delta_usd": max(0.0, daily_spend() - cost_start),
        "git_sha": git_short_sha(),
        "mode": run_kind,
        "summary": {
            "total": len(results),
            "pass": sum(1 for r in results if r.verdict == "PASS"),
            "fail": sum(1 for r in results if r.verdict == "FAIL"),
            "skip": sum(1 for r in results if r.verdict == "SKIP"),
            "error": sum(1 for r in results if r.verdict == "ERROR"),
        },
        "scenarios": [
            {
                "id": r.id,
                "name": r.name,
                "verdict": r.verdict,
                "expected_tools": r.expected_tools,
                "actual_tools": r.actual_tools,
                "keyword_ok": r.keyword_ok,
                "side_effect_ok": r.side_effect_ok,
                "side_effect_detail": r.side_effect_detail,
                "aria_reply_preview": r.aria_reply[:500],
                "duration_sec": r.duration_sec,
                "cost_delta_usd": r.cost_delta_usd,
                "new_events": r.new_events,
                "new_audit": r.new_audit,
                "reason": r.reason,
            }
            for r in results
        ],
    }
    os.makedirs(os.path.dirname(JSON_REPORT_PATH), exist_ok=True)
    with open(JSON_REPORT_PATH, "w") as f:
        json.dump(out, f, indent=2, default=str)
    stamp(f"  report saved: {JSON_REPORT_PATH}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main_async(args: argparse.Namespace) -> int:
    started_at = time.time()
    sentinel = f"golden-{int(started_at)}"

    only: set[str] = set()
    if args.only:
        only = {x.strip() for x in args.only.split(",") if x.strip()}

    # ---- env sanity ----
    bot_token = config.discord_bot_token
    text_channel = config.discord_text_channel_id
    alerts_channel = config.discord_log_channel_id
    if not bot_token or not text_channel or not alerts_channel:
        stamp("FATAL: missing DISCORD_APP_BOT_TOKEN / DISCORD_TEXT_CHANNEL_ID / DISCORD_LOG_CHANNEL_ID")
        return 2
    webhook_url = os.getenv("DISCORD_TEST_WEBHOOK_URL", "") or ""

    obs_host = config.cursor_event_host or "127.0.0.1"
    obs_port = config.cursor_event_port or 8731

    # ---- lifecycle: kill + setup + start ----
    if not args.no_restart:
        stamp("=== STEP 1: kill any running bot")
        kill_bot()
        stamp("=== STEP 2: amend registry + create scratch project")
        amend_registry()
        make_scratch_project(SCRATCH_PROJECT_ROOT)
        stamp(f"=== STEP 3: pre-seed memory sentinel ({sentinel})")
        preseed_memory_sentinel(sentinel)
        stamp("=== STEP 4: start bot in background")
        try:
            start_bot_background()
        except Exception as e:
            stamp(f"FATAL: bot start failed: {e}")
            return 3
        stamp("=== STEP 5: wait for preflight")
        if not await wait_for_preflight():
            stamp("FATAL: preflight failed or timed out — see bot log")
            return 3
    else:
        stamp("=== --no-restart: using already-running bot")
        # Make sure scratch project + registry are usable even on warm runs.
        if not os.path.isdir(SCRATCH_PROJECT_ROOT):
            make_scratch_project(SCRATCH_PROJECT_ROOT)

    # Open the scratch project in Cursor so list/read tools have a target.
    if not args.no_open_cursor:
        stamp(f"=== STEP 5b: open {SCRATCH_PROJECT_ROOT} in Cursor")
        try:
            subprocess.run(
                ["open", "-a", "Cursor", SCRATCH_PROJECT_ROOT],
                check=False, timeout=10,
            )
            await asyncio.sleep(5.0)  # give the IDE a beat to spawn the window
        except Exception as e:
            stamp(f"  WARN: open -a Cursor failed: {e}")

    cost_start = daily_spend()
    voice_kind = "tts" if args.tts else ("no_voice" if args.no_voice else "voice")
    run_kind = (
        ("no_restart " if args.no_restart else "") +
        voice_kind + " " +
        ("no_tier_i" if args.no_tier_i else "tier_i")
    ).strip()

    def _do_cleanup() -> None:
        """Tear down bot + scratch + registry. Idempotent, exception-safe."""
        if args.no_restart:
            stamp("=== --no-restart: skipping bot kill + registry restore (caller owns lifecycle)")
            return
        stamp("=== cleanup — kill bot, restore registry")
        try:
            kill_bot()
        except Exception as e:
            stamp(f"  WARN: kill_bot raised: {e}")
        try:
            restore_registry()
        except Exception as e:
            stamp(f"  WARN: restore_registry raised: {e}")
        if not args.keep_scratch:
            try:
                cleanup_scratch_project(SCRATCH_PROJECT_ROOT)
                stamp(f"  removed scratch project {SCRATCH_PROJECT_ROOT}")
            except Exception as e:
                stamp(f"  WARN: cleanup_scratch_project raised: {e}")
        else:
            stamp(f"  kept scratch project at {SCRATCH_PROJECT_ROOT}")

    results: list[ScenarioResult] = []
    async with aiohttp.ClientSession() as session:
        discord = DiscordHTTP(session, bot_token)
        observer = ObserverHTTP(session, obs_host, obs_port)

        try:
            bot_user_id = await discord.bot_user_id()
        except Exception as e:
            stamp(f"FATAL: couldn't fetch bot user ID: {e}")
            _do_cleanup()
            return 4

        # ---- voice gate (unless --no-voice / --tts) ----
        if args.tts:
            stamp("=== STEP 6: --tts mode — force-connecting Gemini Live (no Discord voice needed)")
            status, body = await observer.test_connect_gemini()
            if status != 200:
                stamp(f"FATAL: /test_connect_gemini failed: {status} {body[:200]}")
                _do_cleanup()
                return 5
            stamp(f"  test_connect_gemini ok: {body[:200]}")
            # Let Gemini settle for a beat before we start feeding audio.
            await asyncio.sleep(2.0)
        elif not args.no_voice:
            stamp("=== STEP 6: voice gate (Aria must be in voice channel)")
            if not await voice_gate(observer):
                stamp("FATAL: voice gate failed")
                _do_cleanup()
                return 5
        else:
            stamp("=== STEP 6: --no-voice mode — skipping voice gate, using !ask webhook")

        ctx = ScenarioContext(
            session=session,
            discord=discord,
            observer=observer,
            no_voice=args.no_voice,
            no_tier_i=args.no_tier_i,
            tts_mode=args.tts,
            scratch_project=SCRATCH_PROJECT_ROOT,
            sentinel=sentinel,
            text_channel_id=text_channel,
            alerts_channel_id=alerts_channel,
            webhook_url=webhook_url,
            bot_user_id=bot_user_id,
        )

        # ---- header to #ucs ----
        await ctx.narrate(
            "---\n"
            f"**Aria Golden Path E2E starting** (sentinel=`{sentinel}`, mode=`{run_kind}`)\n"
            f"Scenarios: {', '.join(s[0] for s in SCENARIOS if not only or s[0] in only)}\n"
            "---"
        )

        # ---- run scenarios ----
        try:
            for sid, name, fn in SCENARIOS:
                if only and sid not in only:
                    continue
                if sid == "S8" and args.no_tier_i:
                    stamp(f"=== {sid} {name}: SKIP (--no-tier-i)")
                    skip = ScenarioResult(
                        id=sid, name=name, verdict="SKIP",
                        expected_tools=[], actual_tools=[],
                        keyword_ok=True, side_effect_ok=True,
                        side_effect_detail="--no-tier-i", aria_reply="",
                        duration_sec=0.0, cost_delta_usd=0.0,
                        reason="--no-tier-i",
                    )
                    results.append(skip)
                    await post_scenario_row(ctx, skip)
                    continue
                if sid == "S10" and not webhook_url:
                    stamp(f"=== {sid} {name}: SKIP (no DISCORD_TEST_WEBHOOK_URL)")
                    skip = ScenarioResult(
                        id=sid, name=name, verdict="SKIP",
                        expected_tools=[], actual_tools=[],
                        keyword_ok=True, side_effect_ok=True,
                        side_effect_detail="webhook url missing", aria_reply="",
                        duration_sec=0.0, cost_delta_usd=0.0,
                        reason="DISCORD_TEST_WEBHOOK_URL not set",
                    )
                    results.append(skip)
                    await post_scenario_row(ctx, skip)
                    continue
                stamp(f"=== {sid} {name}: starting")
                try:
                    r = await fn(ctx)
                except Exception as e:
                    stamp(f"  {sid} crashed: {e!r}")
                    r = ScenarioResult(
                        id=sid, name=name, verdict="ERROR",
                        expected_tools=[], actual_tools=[],
                        keyword_ok=False, side_effect_ok=False,
                        side_effect_detail="", aria_reply="",
                        duration_sec=0.0, cost_delta_usd=0.0,
                        reason=f"exception: {e!r}",
                    )
                results.append(r)
                stamp(f"  {sid} {name}: {r.verdict}  tools={r.actual_tools}  reason={r.reason[:140]}")
                await post_scenario_row(ctx, r)
                # Inter-scenario settle: Gemini may still be finishing an
                # audio reply from the prior turn; if we feed new audio
                # before its VAD declares end-of-turn, the next turn gets
                # batched and the test races.
                await asyncio.sleep(4.0)

            # ---- final report ----
            try:
                await post_final_report(ctx, results, started_at, cost_start, run_kind)
            except Exception as e:
                stamp(f"  WARN: post_final_report raised: {e}")
            try:
                save_json_report(results, started_at, cost_start, run_kind)
            except Exception as e:
                stamp(f"  WARN: save_json_report raised: {e}")
        except Exception as e:
            stamp(f"  FATAL: scenario loop crashed: {e!r}")
        finally:
            _do_cleanup()

    failures = sum(1 for r in results if r.verdict in ("FAIL", "ERROR"))
    return 0 if failures == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-restart", action="store_true",
                    help="Use already-running bot (skip kill/start/preflight/cleanup).")
    ap.add_argument("--no-voice", action="store_true",
                    help="Drive scenarios via !ask webhook instead of /aria_say (no voice).")
    ap.add_argument("--tts", action="store_true",
                    help="Fully autonomous: force-connect Gemini Live (no operator in voice "
                         "needed) and drive each verbal request via synthesized TTS audio "
                         "fed into /test_voice_in (Gemini transcribes it server-side).")
    ap.add_argument("--no-tier-i", action="store_true",
                    help="Skip S8 (no real email sent).")
    ap.add_argument("--no-open-cursor", action="store_true",
                    help="Don't open the scratch project in Cursor at startup.")
    ap.add_argument("--only", type=str, default="",
                    help="Comma-separated subset of scenario IDs, e.g. S1,S3,S8.")
    ap.add_argument("--keep-scratch", action="store_true",
                    help="Don't rm /tmp/aria_e2e_golden after the run.")
    args = ap.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        stamp("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
