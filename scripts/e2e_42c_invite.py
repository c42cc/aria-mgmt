#!/usr/bin/env python3
"""True end-to-end test of the "make an account + text a friend" pipeline.

Subject: Corbin (the user himself). By default this SENDS A REAL iMessage to
Corbin's phone and actually provisions a real 42c.pw account (htpasswd upsert +
Fly redeploy), so it is a genuine end-to-end exercise of every primitive Aria
uses for the headline request:

    "make an account for <person>, then text them the link + creds with a joke"

Pipeline (each step is asserted PASS/FAIL):

  1. resolve recipient   contacts_people search "Corbin"  (or --to / ARIA_TEST_IMESSAGE_TO)
  2. read recent thread  messages_chat read  -> a snippet to personalize the message
  3. create account      create_42c_account(username, password)  [real apr1 + deploy + curl]
  4. send the invite     messages_chat create: joke + link + creds  -> Corbin's phone
  5. verify creds        the new username/password authenticate against the live site

The script brings up an apple-only MCP client in-process (fast; skips the
gmail/github/gcal fleet) and calls the exact same tools the bot uses.

FLAGS
  --to "<handle>"      recipient iMessage handle (phone/email); overrides lookup
  --username z         42c.pw username  (default: z)
  --password zed       42c.pw password  (default: zed)
  --no-deploy          stage the credential but skip the Fly redeploy (dry run)
  --no-send            do everything except actually sending the iMessage
  --cleanup            remove the test credential line from .htpasswd afterwards
                       (local only; the live cred persists until the next deploy)

USAGE
  # Safe dry run first (no deploy, no real text):
  .venv/bin/python scripts/e2e_42c_invite.py --no-deploy --no-send

  # The real thing (creates the account, texts your phone):
  .venv/bin/python scripts/e2e_42c_invite.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_PHONE_RE = re.compile(r"\+?\d[\d\-\s().]{6,}\d")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class Step:
    """One asserted pipeline step with a PASS/FAIL verdict."""

    def __init__(self, name: str):
        self.name = name
        self.ok: bool | None = None
        self.detail = ""

    def passed(self, detail: str = "") -> "Step":
        self.ok, self.detail = True, detail
        print(f"  [PASS] {self.name}" + (f" — {detail}" if detail else ""))
        return self

    def failed(self, detail: str = "") -> "Step":
        self.ok, self.detail = False, detail
        print(f"  [FAIL] {self.name}" + (f" — {detail}" if detail else ""))
        return self


def _extract_handle(text: str) -> str:
    """Pull the first phone/email handle out of an MCP contacts/messages blob."""
    email = _EMAIL_RE.search(text or "")
    if email:
        return email.group(0)
    phone = _PHONE_RE.search(text or "")
    if phone:
        return re.sub(r"[\s().\-]", "", phone.group(0))
    return ""


async def _start_apple_mcp():
    """Start an apple-only MCP client in-process and wire it as the module global."""
    from src import mcp as mcp_mod

    mcp_mod.MCP_SERVERS = {"apple": mcp_mod.MCP_SERVERS["apple"]}
    client = mcp_mod.MCPClient()
    await client.start_all()
    mcp_mod.mcp_client = client  # so do_with_claude can find it if needed
    return client


async def main() -> int:
    ap = argparse.ArgumentParser(description="42c.pw invite E2E (subject: Corbin)")
    ap.add_argument("--to", default="", help="recipient iMessage handle (phone/email)")
    ap.add_argument("--subject", default="Corbin", help="contact name to look up / personalize")
    ap.add_argument("--username", default="z")
    ap.add_argument("--password", default="zed")
    ap.add_argument("--no-deploy", action="store_true", help="skip the Fly redeploy")
    ap.add_argument("--no-send", action="store_true", help="skip the real iMessage send")
    ap.add_argument("--cleanup", action="store_true", help="remove the test htpasswd line at the end")
    args = ap.parse_args()

    started = time.monotonic()
    steps: list[Step] = []
    print(f"\n=== 42c.pw invite E2E — subject={args.subject} "
          f"deploy={not args.no_deploy} send={not args.no_send} ===\n")

    print("Starting apple MCP server (npx mcp-macos)…")
    try:
        client = await _start_apple_mcp()
    except Exception as e:
        print(f"  [FAIL] could not start apple MCP: {e}")
        return 1
    if "messages_chat" not in client._tools or "contacts_people" not in client._tools:
        print(f"  [FAIL] apple MCP missing tools; have: {sorted(client._tools)}")
        return 1
    print(f"  apple MCP up — tools: {sorted(client._tools)}\n")

    # -- Step 1: resolve recipient (non-fatal — read + account steps still run)
    s1 = Step("resolve recipient")
    steps.append(s1)
    handle = (args.to or os.getenv("ARIA_TEST_IMESSAGE_TO", "")).strip()
    method = "flag/env" if handle else ""
    contact_blob = ""
    if not handle:
        try:
            contact_blob = await client.call_tool(
                "contacts_people", {"action": "search", "search": args.subject}
            )
        except Exception as e:
            contact_blob = f"(contacts search error: {e})"
        extracted = _extract_handle(contact_blob)
        if extracted:
            handle, method = extracted, "contacts"
    if not handle:
        de = os.getenv("DISCORD_EMAIL", "").strip()
        if de:
            handle, method = de, "DISCORD_EMAIL fallback"
    if handle:
        s1.passed(f"recipient={handle} (via {method})")
    else:
        # Contacts automation is commonly denied (TCC -1743); this is a known
        # macOS permission gap, not a code failure. The send step will report it.
        s1.failed("unresolved — set ARIA_TEST_IMESSAGE_TO or --to (Contacts automation likely denied)")

    # -- Step 2: read recent messages for a personal snippet ----------------
    # Read UNFILTERED (action:read with no contact) so it goes straight to
    # chat.db via Full Disk Access. Reading by contact NAME would trigger the
    # Contacts automation path, which is commonly denied (TCC -1743).
    s2 = Step("read recent messages")
    steps.append(s2)
    snippet = ""
    try:
        thread = await client.call_tool("messages_chat", {"action": "read"})
        low = thread.lower()
        if any(k in low for k in ("permission denied", "not authorized",
                                  "full disk", "operation not permitted")):
            s2.failed(f"messages read blocked: {thread[:160]}")
        else:
            m = re.search(r"Last:\s*([^\n]+)", thread)
            if m:
                raw = m.group(1).split("\\n")[0].split(" - Date")[0]
                snippet = re.sub(r"\s+", " ", raw).strip()[:60]
            s2.passed(f"read {len(thread)} chars"
                      + (f"; recent: {snippet!r}" if snippet else "; (no snippet)"))
    except Exception as e:
        s2.failed(f"messages read error: {e}")

    # -- Step 3: create the 42c.pw account ----------------------------------
    s3 = Step("create 42c.pw account")
    steps.append(s3)
    from src.tools import _create_42c_account

    acct_raw = await _create_42c_account(
        username=args.username,
        password=args.password,
        label=f"E2E invite for {args.subject}",
        deploy=not args.no_deploy,
    )
    try:
        acct = json.loads(acct_raw)
    except Exception:
        acct = {"error": acct_raw[:200]}
    if acct.get("error"):
        s3.failed(f"create_42c_account error: {acct['error']} {acct.get('detail','')}")
        return _finish(steps, started)
    url = acct.get("url", "")
    if args.no_deploy:
        s3.passed(f"staged (not deployed) user={acct.get('username')} url={url}")
    elif acct.get("verified"):
        s3.passed(f"deployed + verified user={acct.get('username')} url={url}")
    else:
        s3.failed(f"deployed but creds did not verify against {url} (check Fly logs)")

    # -- Step 4: send the invite iMessage -----------------------------------
    s4 = Step("send invite iMessage")
    steps.append(s4)
    greeting = f"Hey, it's Aria ({args.subject}'s AI assistant)."
    if snippet:
        joke = (f"saw a \u201c{snippet}\u201d fly by in the chats \u2014 anyway, "
                f"here's a peek at what Corbin's building:")
    else:
        joke = "here's a peek at what Corbin's building:"
    body = (
        f"{greeting} {joke}\n\n"
        f"Login: {url}\n"
        f"Username: {acct.get('username')}\n"
        f"Password: {acct.get('password')}"
    )
    if not handle:
        s4.failed("no recipient resolved — skipped send (set ARIA_TEST_IMESSAGE_TO or --to)")
    elif args.no_send:
        s4.passed(f"SKIPPED (--no-send). Would send to {handle}:\n---\n{body}\n---")
    else:
        try:
            send_res = await asyncio.wait_for(
                client.call_tool(
                    "messages_chat", {"action": "create", "to": handle, "text": body}
                ),
                timeout=45,
            )
            low = send_res.lower()
            if any(k in low for k in ("did not respond", "not authorized", "failed",
                                      "error", "permission", "-1743")):
                s4.failed(
                    f"send blocked: {send_res[:200]} "
                    "[likely macOS Automation > Messages not granted to this Python — "
                    "see preflight 'messages_send']"
                )
            else:
                s4.passed(f"sent to {handle}: {send_res[:120]}")
        except asyncio.TimeoutError:
            s4.failed(
                "send timed out (45s) — macOS Automation > Messages is not granted "
                "(a one-time approval prompt is waiting/unanswered). See preflight 'messages_send'."
            )
        except Exception as e:
            s4.failed(f"send error: {e}")

    # -- Step 5: verify creds authenticate ----------------------------------
    s5 = Step("verify creds authenticate")
    steps.append(s5)
    if args.no_deploy:
        s5.passed("SKIPPED (not deployed)")
    else:
        from src.tools import _verify_42c_login
        ok = await asyncio.to_thread(
            _verify_42c_login, url, acct.get("username", ""), acct.get("password", "")
        )
        s5.passed(f"{url} accepts the new creds") if ok else s5.failed(
            f"{url} rejected the new creds"
        )

    # -- optional cleanup ---------------------------------------------------
    if args.cleanup:
        from src.config import config
        from src.tools import _upsert_htpasswd  # noqa: F401  (path import sanity)
        htpasswd = os.path.join(config.c42_public_dir, ".htpasswd")
        try:
            lines = [ln.rstrip("\n") for ln in open(htpasswd) if ln.strip()]
            kept = [ln for ln in lines if ln.split(":", 1)[0] != args.username]
            with open(htpasswd, "w") as f:
                f.write("\n".join(kept) + "\n")
            print(f"\n  cleanup: removed '{args.username}' from local .htpasswd "
                  f"(live cred persists until next deploy)")
        except Exception as e:
            print(f"\n  cleanup failed (non-fatal): {e}")

    return _finish(steps, started)


def _finish(steps: list[Step], started: float) -> int:
    passed = sum(1 for s in steps if s.ok)
    total = len(steps)
    elapsed = time.monotonic() - started
    print(f"\n=== RESULT: {passed}/{total} steps passed in {elapsed:.0f}s ===")
    report = {
        "passed": passed,
        "total": total,
        "elapsed_sec": round(elapsed, 1),
        "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail} for s in steps],
    }
    out = ROOT / "scripts" / "e2e_42c_invite_last_report.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"Report: {out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    # os._exit after the report avoids the noisy asyncgen/MCP-stdio teardown
    # traceback that anyio emits when the event loop tears down cross-task.
    _code = asyncio.run(main())
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)
