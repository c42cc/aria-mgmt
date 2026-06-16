#!/usr/bin/env python3
"""Read-only forensic pull of the last N hours of Discord messages.

Authenticates with the Aria app bot token (DISCORD_APP_BOT_TOKEN) and pages the
Discord REST API. It NEVER prints the token, NEVER sends a message, and makes no
write to Discord other than (optionally) opening a DM channel handle with the
authorized user (which sends no notification) so DM history can be read too.

Output: a chronological transcript of every human message and every Aria reply
in the window, written to data/discord_pull_<hours>h.md and .json.

Usage:
    ./.venv/bin/python scripts/pull_discord_72h.py            # last 72h
    PULL_HOURS=48 ./.venv/bin/python scripts/pull_discord_72h.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

API = "https://discord.com/api/v10"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")

# Channel types that can hold messages we care about.
PARENT_TYPES = {0, 2, 5, 15}          # text, voice(text chat), announcement, forum
ARCHIVE_PARENT_TYPES = {0, 5, 15}     # only these support archived-thread listing
THREAD_TYPES = {10, 11, 12}           # announcement/public/private threads


def main() -> int:
    load_dotenv()
    hours = int(os.environ.get("PULL_HOURS", "72"))
    token = os.getenv("DISCORD_APP_BOT_TOKEN", "").strip()
    if not token:
        print("FATAL: DISCORD_APP_BOT_TOKEN missing from environment/.env", file=sys.stderr)
        return 2

    guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
    auth_ids = {u.strip() for u in os.getenv("AUTHORIZED_USER_IDS", "").split(",") if u.strip()}
    vid = os.getenv("AUTHORIZED_VOICE_USER_ID", "").strip()
    if vid:
        auth_ids.add(vid)

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)

    client = httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bot {token}", "User-Agent": "AriaForensic/1.0 (read-only)"},
        timeout=30.0,
    )

    def get(path: str, params: dict | None = None):
        for _ in range(8):
            r = client.get(path, params=params)
            if r.status_code == 429:
                retry = float(r.json().get("retry_after", 1.0))
                time.sleep(retry + 0.4)
                continue
            if r.status_code in (400, 403, 404):
                return None  # no access / unsupported for this channel; skip quietly
            r.raise_for_status()
            return r.json()
        return None

    def post(path: str, body: dict):
        for _ in range(8):
            r = client.post(path, json=body)
            if r.status_code == 429:
                time.sleep(float(r.json().get("retry_after", 1.0)) + 0.4)
                continue
            if r.status_code >= 400:
                return None
            return r.json()
        return None

    print(f"[config] token len={len(token)} prefix={token[:4]}*** "
          f"guild={guild_id or '(discover)'} authorized={sorted(auth_ids) or '(none)'} "
          f"window={hours}h cutoff={cutoff.isoformat()}")

    me = get("/users/@me")
    if not me:
        print("FATAL: could not authenticate (bad token?)", file=sys.stderr)
        return 2
    bot_id = me["id"]
    print(f"[config] bot user: {me.get('username')} ({bot_id})")

    guilds = [{"id": guild_id}] if guild_id else (get("/users/@me/guilds") or [])
    print(f"[config] guilds: {[g['id'] for g in guilds]}")

    # ---- enumerate channels + threads -------------------------------------
    targets: dict[str, dict] = {}  # channel_id -> {name, kind, parent}

    for g in guilds:
        gid = g["id"]
        chans = get(f"/guilds/{gid}/channels") or []
        for c in chans:
            if c.get("type") in PARENT_TYPES:
                targets[c["id"]] = {"name": c.get("name", c["id"]), "kind": "channel", "parent": None}
        # active threads (across the whole guild)
        active = get(f"/guilds/{gid}/threads/active") or {}
        for t in active.get("threads", []):
            targets[t["id"]] = {"name": t.get("name", t["id"]), "kind": "thread", "parent": t.get("parent_id")}
        # archived public threads per parent
        for c in chans:
            if c.get("type") in ARCHIVE_PARENT_TYPES:
                arch = get(f"/channels/{c['id']}/threads/archived/public", {"limit": 100})
                for t in (arch or {}).get("threads", []):
                    targets[t["id"]] = {"name": t.get("name", t["id"]), "kind": "thread", "parent": c["id"]}
                arch_priv = get(f"/channels/{c['id']}/threads/archived/private", {"limit": 100})
                for t in (arch_priv or {}).get("threads", []):
                    targets[t["id"]] = {"name": t.get("name", t["id"]), "kind": "thread", "parent": c["id"]}

    # ---- DM channel with each authorized user (read-only; no message sent) -
    for uid in auth_ids:
        dm = post("/users/@me/channels", {"recipient_id": uid})
        if dm and dm.get("id"):
            targets[dm["id"]] = {"name": f"DM:{uid}", "kind": "dm", "parent": None}

    print(f"[scan] {len(targets)} channels/threads to scan")

    # ---- page messages within the window ----------------------------------
    rows: list[dict] = []
    empty_content = 0
    for cid, meta in targets.items():
        before = None
        while True:
            params = {"limit": 100}
            if before:
                params["before"] = before
            batch = get(f"/channels/{cid}/messages", params)
            if not batch:
                break
            stop = False
            for m in batch:
                ts = dt.datetime.fromisoformat(m["timestamp"])
                if ts < cutoff:
                    stop = True
                    continue
                author = m.get("author", {})
                content = (m.get("content") or "").strip()
                if not content and (m.get("attachments") or m.get("embeds")):
                    parts = []
                    for a in m.get("attachments", []):
                        parts.append(f"[attachment: {a.get('filename')}]")
                    if m.get("embeds"):
                        parts.append(f"[{len(m['embeds'])} embed(s)]")
                    content = " ".join(parts)
                if not content:
                    empty_content += 1
                rows.append({
                    "ts": ts.isoformat(),
                    "channel_id": cid,
                    "channel": meta["name"],
                    "kind": meta["kind"],
                    "author_id": author.get("id"),
                    "author": author.get("global_name") or author.get("username") or author.get("id"),
                    "is_bot": bool(author.get("bot")),
                    "is_authorized_human": author.get("id") in auth_ids,
                    "content": content,
                })
            before = batch[-1]["id"]
            if stop or len(batch) < 100:
                break

    rows.sort(key=lambda r: r["ts"])

    def fmt_local(iso: str) -> str:
        return dt.datetime.fromisoformat(iso).astimezone(LOCAL_TZ).strftime("%a %b %-d, %-I:%M %p")

    you = sum(1 for r in rows if r["is_authorized_human"])
    aria = sum(1 for r in rows if r["author_id"] == bot_id)
    other = len(rows) - you - aria

    # ---- write artifacts ---------------------------------------------------
    os.makedirs("data", exist_ok=True)
    json_path = f"data/discord_pull_{hours}h.json"
    md_path = f"data/discord_pull_{hours}h.md"
    with open(json_path, "w") as f:
        json.dump({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "hours": hours, "bot_id": bot_id, "counts": {"you": you, "aria": aria, "other": other},
                   "rows": rows}, f, indent=2)

    lines: list[str] = []
    lines.append(f"# Discord pull — last {hours}h (authoritative)\n")
    lines.append(f"Generated {dt.datetime.now(LOCAL_TZ).strftime('%a %b %-d %Y, %-I:%M %p %Z')}. "
                 f"Total {len(rows)} messages in window: **you={you}**, **Aria={aria}**, other={other}. "
                 f"Empty/no-text content: {empty_content}.\n")
    last_chan = None
    for r in rows:
        if r["channel"] != last_chan:
            lines.append(f"\n## {('#' if r['kind']=='channel' else '')}{r['channel']}  ({r['kind']}, id {r['channel_id']})\n")
            last_chan = r["channel"]
        who = "YOU" if r["is_authorized_human"] else ("ARIA" if r["author_id"] == bot_id else r["author"])
        body = r["content"].replace("\n", "\n      ")
        lines.append(f"- **[{fmt_local(r['ts'])}] {who}:** {body}")
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    print(f"\n[done] wrote {md_path} and {json_path}")
    print(f"[counts] you={you} aria={aria} other={other} empty_content={empty_content} total={len(rows)}")
    print("\n" + "\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
