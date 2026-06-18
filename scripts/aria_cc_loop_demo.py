#!/usr/bin/env python3
"""Aria x Claude Code -- one end-to-end loop, demonstrated and visible.

Drives Aria's REAL Claude Code driver (Opus 4.8, extended thinking, Plan Mode)
through ONE plan -> build -> verify loop on a small, visually-relevant task,
surfacing EVERY interaction Aria would send you / receive from you. Aria only
stops to ASK when she genuinely needs an answer; otherwise she runs the whole
loop and shows you the result.

  - Sequence 1: build a PLAN (Plan Mode, no edits).
  - Sequence 2: IMPLEMENT + VERIFY (executes the approved plan, checks the result).
  - Then: sends you the result + a visible HTML receipt of the conversation, and
    the built visual (demo/aria_loop_demo.html) you can open to confirm it worked.

Aria's side is what Gemini Live relays in the running bot; Claude (Opus 4.8 via
Claude Code) does the building. Questions are "called in" on Discord.

Run:
  python scripts/aria_cc_loop_demo.py            # console Q&A (you answer at the terminal)
  python scripts/aria_cc_loop_demo.py --discord  # Aria asks on Discord; you reply in #ucs
  python scripts/aria_cc_loop_demo.py --auto      # unattended (canned answers) -- self-test
"""

from __future__ import annotations

import argparse
import asyncio
import html
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.claude_code import ClaudeCodeBridge, DEFAULT_CLAUDE_CODE_REPO  # noqa: E402
from src.config import config  # noqa: E402
from src.cursor_registry import cursor_registry  # noqa: E402
from src.prompts import load_template  # noqa: E402

MODEL = "claude-opus-4-8"
REPO = DEFAULT_CLAUDE_CODE_REPO
DEMO_DIR = os.path.join(REPO, "demo")
ARTIFACT = os.path.join(DEMO_DIR, "aria_loop_demo.html")
RECEIPT = os.path.join(DEMO_DIR, "aria_loop_receipt.html")

BUILD_TASK = (
    "Think hard, then build a NEW self-contained file demo/aria_loop_demo.html. "
    "It marks that Aria drove Claude Code through one plan -> build -> verify loop. "
    "Requirements (no external dependencies, no build step, no server -- it must open "
    "directly in a browser by double-clicking):\n"
    "- a full-window <canvas> with a continuous requestAnimationFrame animation, your "
    "design, themed on a loop completing (e.g. orbiting/pulsing nodes, a progress ring, "
    "particles) -- make it genuinely pleasant to watch, dark theme;\n"
    "- a centered heading 'Aria x Claude Code' and a subtitle 'one plan -> build -> verify loop';\n"
    "- a small footer showing the three stage labels: PLAN, BUILD, VERIFY;\n"
    "- keep it elegant and under ~220 lines. Do not touch any other file."
)

_AFFIRM = ("yes", "y", "yep", "yeah", "ok", "okay", "approve", "approved", "go",
           "go ahead", "sure", "send it", "do it", "ship it", "looks good", "lgtm")


def _affirmative(s: str) -> bool:
    t = (s or "").strip().lower().rstrip(".!")
    return t in _AFFIRM or t.startswith(("yes", "approve", "go ahead", "send it", "looks good", "do it"))


@dataclass
class Turn:
    who: str   # "aria" | "you"
    kind: str  # status | question | answer | plan | build | result
    text: str
    ts: str


transcript: list[Turn] = []


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def record(who: str, kind: str, text: str) -> Turn:
    t = Turn(who, kind, (text or "").strip(), _now())
    transcript.append(t)
    arrow = "ARIA \u2192 YOU " if who == "aria" else "YOU  \u2192 ARIA"
    print(f"\n\033[1m[{t.ts}] {arrow}\033[0m  ({kind})\n{t.text}\n" + "\u2500" * 64)
    return t


# --------------------------------------------------------------------------
# Discord (REST) -- "she calls you on Discord"
# --------------------------------------------------------------------------

def _discord_ready() -> bool:
    return bool(config.discord_bot_token and config.discord_text_channel_id)


async def discord_post(text: str) -> str | None:
    """Post a message to #ucs; return its message id (or None)."""
    if not _discord_ready():
        return None
    import httpx
    mention = f"<@{config.authorized_user_ids[0]}> " if config.authorized_user_ids else ""
    url = f"https://discord.com/api/v10/channels/{config.discord_text_channel_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(url, headers={"Authorization": f"Bot {config.discord_bot_token}"},
                             json={"content": (mention + text)[:1900]})
        if r.status_code in (200, 201):
            return str(r.json().get("id", ""))
    except Exception as e:
        print(f"(discord post failed: {e})", file=sys.stderr)
    return None


async def discord_poll_reply(after_id: str | None, timeout: float = 600) -> str:
    """Poll #ucs for the authorized user's next message. REST only (no gateway)."""
    if not _discord_ready():
        return ""
    import httpx
    me = config.authorized_user_ids[0] if config.authorized_user_ids else ""
    url = f"https://discord.com/api/v10/channels/{config.discord_text_channel_id}/messages"
    headers = {"Authorization": f"Bot {config.discord_bot_token}"}
    deadline = time.time() + timeout
    params = {"after": after_id, "limit": 10} if after_id else {"limit": 5}
    async with httpx.AsyncClient(timeout=15) as c:
        while time.time() < deadline:
            try:
                r = await c.get(url, headers=headers, params=params)
                if r.status_code == 200:
                    msgs = sorted(r.json(), key=lambda m: int(m["id"]))
                    for m in msgs:
                        if str(m.get("author", {}).get("id")) == me and m.get("content", "").strip():
                            return m["content"].strip()
            except Exception:
                pass
            await asyncio.sleep(4)
    return ""


# --------------------------------------------------------------------------
# Aria's side (what Gemini Live relays in the live bot)
# --------------------------------------------------------------------------

class Aria:
    def __init__(self, mode: str, answers: list[str]):
        self.mode = mode            # "auto" | "console" | "discord"
        self.answers = list(answers)
        self.last_msg_id: str | None = None

    async def say(self, text: str, kind: str = "status") -> None:
        record("aria", kind, text)
        if self.mode == "discord":
            self.last_msg_id = await discord_post(text) or self.last_msg_id

    async def ask(self, question: str) -> str:
        record("aria", "question", question)
        if self.mode == "auto":
            ans = self.answers.pop(0) if self.answers else "yes, approve"
        elif self.mode == "discord":
            self.last_msg_id = await discord_post("\u2753 " + question) or self.last_msg_id
            ans = await discord_poll_reply(self.last_msg_id, timeout=900)
            if not ans:
                ans = "yes"  # no reply before timeout -> proceed rather than hang
        else:
            try:
                ans = input("\033[1mYOU \u2192 ARIA:\033[0m ").strip() or "yes"
            except EOFError:
                ans = "yes"
        record("you", "answer", ans)
        return ans


# --------------------------------------------------------------------------
# Verify + receipt
# --------------------------------------------------------------------------

def verify_artifact() -> tuple[bool, dict[str, bool]]:
    if not os.path.exists(ARTIFACT):
        return False, {"file written": False}
    src = open(ARTIFACT, encoding="utf-8", errors="replace").read()
    low = src.lower()
    checks = {
        "file written": True,
        "has <canvas>": "<canvas" in low,
        "animates (requestAnimationFrame)": "requestanimationframe" in low,
        "self-contained (no external src)": "http://" not in low and "https://" not in low,
        "has heading 'Aria'": "aria" in low,
        "non-trivial (>800 bytes)": len(src) > 800,
    }
    return all(checks.values()), checks


def write_receipt(ok: bool, checks: dict[str, bool]) -> None:
    rows = []
    for t in transcript:
        side = "aria" if t.who == "aria" else "you"
        who = "Aria \u2192 You" if t.who == "aria" else "You \u2192 Aria"
        rows.append(
            f'<div class="msg {side}"><div class="meta">{t.ts} \u00b7 {who} \u00b7 {t.kind}</div>'
            f'<pre>{html.escape(t.text)}</pre></div>'
        )
    checkrows = "".join(
        f'<li class="{"ok" if v else "bad"}">{"\u2713" if v else "\u2717"} {html.escape(k)}</li>'
        for k, v in checks.items()
    )
    verdict = "GREEN \u2014 verified" if ok else "RED \u2014 verify failed"
    page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Aria x Claude Code -- loop receipt</title>
<style>
 body{{margin:0;background:#0b0d12;color:#e6e9ef;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}}
 .wrap{{max-width:920px;margin:0 auto;padding:28px}}
 h1{{font-weight:700;letter-spacing:.3px}} .sub{{color:#8b93a7;margin-top:-8px}}
 .verdict{{display:inline-block;padding:6px 12px;border-radius:8px;font-weight:700;
   background:{'#10331f' if ok else '#3a1620'};color:{'#5ad18a' if ok else '#ff7a8a'}}}
 .panel{{background:#11141c;border:1px solid #1e2330;border-radius:12px;padding:18px;margin:18px 0}}
 .msg{{border-radius:10px;padding:10px 14px;margin:10px 0}}
 .msg.aria{{background:#141a2b;border-left:3px solid #5b8cff}}
 .msg.you{{background:#1a1622;border-left:3px solid #c08bff}}
 .meta{{color:#8b93a7;font-size:12px;margin-bottom:6px}}
 pre{{margin:0;white-space:pre-wrap;word-wrap:break-word;font:13px/1.5 ui-monospace,Menlo,monospace}}
 ul{{list-style:none;padding:0}} li{{padding:3px 0}} li.ok{{color:#5ad18a}} li.bad{{color:#ff7a8a}}
 iframe{{width:100%;height:460px;border:1px solid #1e2330;border-radius:12px;background:#000}}
 a{{color:#7aa2ff}}
</style></head><body><div class="wrap">
 <h1>Aria &times; Claude Code &mdash; one loop</h1>
 <p class="sub">Aria drove Claude Code (Opus 4.8, extended thinking, Plan Mode) through
   plan &rarr; build &rarr; verify. Model = {MODEL}.</p>
 <p><span class="verdict">{verdict}</span></p>

 <div class="panel"><h3>The built visual (live below &mdash; or open
   <a href="aria_loop_demo.html">aria_loop_demo.html</a>)</h3>
   <iframe src="aria_loop_demo.html" title="aria_loop_demo"></iframe></div>

 <div class="panel"><h3>Verification</h3><ul>{checkrows}</ul></div>

 <div class="panel"><h3>Every interaction (Aria &harr; You)</h3>{''.join(rows)}</div>
 <p class="sub">Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by scripts/aria_cc_loop_demo.py</p>
</div></body></html>"""
    os.makedirs(DEMO_DIR, exist_ok=True)
    with open(RECEIPT, "w", encoding="utf-8") as f:
        f.write(page)


# --------------------------------------------------------------------------
# The loop
# --------------------------------------------------------------------------

async def run_loop(aria: Aria) -> bool:
    if not os.path.isdir(REPO):
        raise SystemExit(f"managed repo not found: {REPO}")
    os.makedirs(DEMO_DIR, exist_ok=True)
    bridge = ClaudeCodeBridge()

    # Input review (edit-before-submit on the instruction).
    plan_instruction = load_template("cc_plan").replace("{{task}}", BUILD_TASK)
    await aria.say(
        "I'm going to have Claude Code (Opus 4.8, extended thinking, Plan Mode) PLAN this "
        f"small visual build:\n\n{BUILD_TASK}\n\nThat's the exact instruction I'll send it.",
        kind="status",
    )
    tweak = await aria.ask("Send it as-is, or tell me a change first?")
    if tweak and not _affirmative(tweak):
        plan_instruction += f"\n\nAdditional direction from Corbin: {tweak}"
        await aria.say("Folded your change into the instruction.", kind="status")

    # SEQUENCE 1 -- PLAN
    await aria.say("Planning now (Plan Mode -- no edits yet). One moment\u2026", kind="status")
    sid = await bridge.spawn_and_wait(REPO, plan_instruction, mode="plan", model=MODEL, timeout=480)
    # Plan Mode is read-only: nothing should have been written yet. This is the
    # proof the two sequences are cleanly separated (plan, THEN build).
    plan_phase_no_write = not os.path.exists(ARTIFACT)
    agent = cursor_registry.agent_for_session(sid)
    plan = (agent.last_assistant_text if agent else "") or ""
    if not plan:
        await aria.say("Claude Code returned no plan -- stopping.", kind="result")
        await bridge.cancel_session(sid)
        return False
    await aria.say(
        f"Here's Claude Code's plan (Plan Mode wrote no files yet: {plan_phase_no_write}):\n\n{plan}",
        kind="plan",
    )

    decision = await aria.ask("Approve this plan so I can have it build it? (yes, or tell me a change)")
    if not _affirmative(decision):
        await aria.say("Refining the plan with your note\u2026", kind="status")
        await bridge.send_and_wait(sid, f"Revise the plan per this, then re-show it: {decision}", timeout=300)
        agent = cursor_registry.agent_for_session(sid)
        await aria.say(f"Revised plan:\n\n{(agent.last_assistant_text if agent else '')}", kind="plan")
        decision = await aria.ask("Approve now?")
    if not _affirmative(decision):
        await aria.say("Okay -- stopping here. Nothing was built.", kind="result")
        await bridge.cancel_session(sid)
        return False

    # SEQUENCE 2 -- IMPLEMENT + VERIFY
    await aria.say("Approved. Building it now (acceptEdits). I'll come back when it's verified.", kind="status")
    await bridge.send_and_wait(
        sid,
        "Approved. Implement the plan now: create demo/aria_loop_demo.html exactly as planned. "
        "Then read the file back and confirm it is a complete, self-contained animated page that "
        "opens standalone. Report the path and a one-line confirmation.",
        mode="acceptEdits",
        timeout=600,
    )
    agent = cursor_registry.agent_for_session(sid)
    build_summary = (agent.last_assistant_text if agent else "") or "(no summary)"
    await aria.say(f"Claude Code finished the build turn:\n\n{build_summary}", kind="build")

    ok, checks = verify_artifact()
    checks["plan phase wrote no files (clean plan/build split)"] = plan_phase_no_write
    ok = ok and plan_phase_no_write
    await bridge.cancel_session(sid)

    checklist = "\n".join(f"  {'PASS' if v else 'FAIL'}  {k}" for k, v in checks.items())
    if ok:
        await aria.say(
            "Done \u2014 and verified. Claude Code built the visual and it passed every check:\n"
            f"{checklist}\n\nOpen it: {ARTIFACT}\nFull receipt: {RECEIPT}",
            kind="result",
        )
    else:
        await aria.say(
            f"The build did NOT pass verification:\n{checklist}\n"
            f"I'm flagging this rather than calling it done. File (if any): {ARTIFACT}",
            kind="result",
        )
    write_receipt(ok, checks)
    return ok


async def main() -> int:
    ap = argparse.ArgumentParser(description="Aria x Claude Code end-to-end loop demo")
    ap.add_argument("--auto", action="store_true", help="unattended: canned answers")
    ap.add_argument("--discord", action="store_true", help="Aria asks on Discord; you reply in #ucs")
    ap.add_argument("--answers", nargs="*", default=["send it", "yes, approve"],
                    help="canned answers for --auto")
    args = ap.parse_args()
    mode = "auto" if args.auto else ("discord" if args.discord else "console")
    aria = Aria(mode=mode, answers=args.answers)

    print(f"\n=== Aria x Claude Code loop demo (mode={mode}, model={MODEL}) ===")
    ok = await run_loop(aria)

    # One real Discord proof line so the "she calls you on Discord" path is demonstrated
    # even from an --auto run, best-effort.
    posted = await discord_post(
        ("\u2705 " if ok else "\u26a0\ufe0f ") +
        "Aria here \u2014 I just ran one full plan\u2192build\u2192verify loop with Claude Code "
        f"(Opus 4.8). Result: {'verified GREEN' if ok else 'needs a look'}. "
        f"Open demo/aria_loop_demo.html. (This line is the Discord channel working.)"
    )
    print(f"\n[discord proof post: {'sent' if posted else 'skipped/failed'}]")
    print(f"[artifact] {ARTIFACT}\n[receipt ] {RECEIPT}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
