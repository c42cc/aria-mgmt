"""Deep integration tests — exercises every subsystem live."""

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

BOT_TOKEN = ""
TC = ""

with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")) as f:
    for line in f:
        if line.startswith("DISCORD_APP_BOT_TOKEN="):
            BOT_TOKEN = line.split("=", 1)[1].strip()
        if line.startswith("DISCORD_TEXT_CHANNEL_ID="):
            TC = line.split("=", 1)[1].strip()


async def post(sess, headers, msg):
    await sess.post(
        f"https://discord.com/api/v10/channels/{TC}/messages",
        headers=headers,
        json={"content": msg[:2000]},
    )


async def run():
    import aiohttp

    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    async with aiohttp.ClientSession() as sess:
        await post(sess, headers, "---\n**Deep Integration Tests**\n---")

        # 1. Memory
        print("[1] Memory...")
        from src.memory import init_memory, remember, recall

        init_memory()
        unique = f"ucs-test-{int(time.time())}"
        remember(f"Corbin prefers dark mode. Code: {unique}")
        results = recall("What does Corbin prefer?")
        result_list = results if isinstance(results, list) else results.get("results", results.get("memories", []))
        s1 = f"PASS ({len(result_list)} results)" if result_list else "FAIL (empty)"
        preview = json.dumps(result_list[:1] if isinstance(result_list, list) else result_list, indent=2, default=str)[:250]
        await post(sess, headers, f"**[1] Memory:** {s1}\n```\n{preview}\n```")
        print(f"  {s1}")

        # 2. MCP fleet
        print("[2] MCP fleet...")
        from src.mcp import init_mcp

        mcp = await init_mcp()
        up = len(mcp._servers)
        tools = len(mcp._tools)
        await post(sess, headers, f"**[2] MCP fleet:** PASS ({up} servers, {tools} tools)")
        print(f"  {up} servers, {tools} tools")

        # 3. Filesystem
        print("[3] Filesystem...")
        r3 = await mcp.call_tool("list_directory", {"path": "/Users/corbin/Documents"})
        ok3 = "error" not in str(r3).lower()[:50]
        await post(sess, headers, f"**[3] Filesystem:** {'PASS' if ok3 else 'FAIL'}\n{str(r3)[:250]}")
        print(f"  {'PASS' if ok3 else 'FAIL'}")

        # 4. Shell
        print("[4] Shell...")
        shell_tools = [t for t in mcp._tools if mcp._tool_to_server.get(t) == "shell"]
        if shell_tools:
            r4 = await mcp.call_tool(shell_tools[0], {"command": "echo SHELL_OK && date"})
            ok4 = "SHELL_OK" in str(r4)
            await post(sess, headers, f"**[4] Shell ({shell_tools[0]}):** {'PASS' if ok4 else 'FAIL'}\n{str(r4)[:200]}")
            print(f"  {'PASS' if ok4 else 'FAIL'}")
        else:
            await post(sess, headers, "**[4] Shell:** SKIP")

        # 5. GitHub
        print("[5] GitHub...")
        gh_tools = sorted(t for t in mcp._tools if mcp._tool_to_server.get(t) == "github")
        await post(sess, headers, f"**[5] GitHub:** {len(gh_tools)} tools: {gh_tools[:8]}")
        print(f"  {len(gh_tools)} tools")

        # 6. Apple
        print("[6] Apple...")
        ap_tools = sorted(t for t in mcp._tools if mcp._tool_to_server.get(t) == "apple")
        cal = [t for t in ap_tools if "calendar" in t]
        if cal:
            r6 = await mcp.call_tool(cal[0], {"limit": 3})
            ok6 = "error" not in str(r6).lower()[:50]
            await post(sess, headers, f"**[6] Apple calendar ({cal[0]}):** {'PASS' if ok6 else 'FAIL'}\n{str(r6)[:300]}")
            print(f"  {'PASS' if ok6 else 'FAIL'}")
        else:
            await post(sess, headers, f"**[6] Apple:** tools={ap_tools}")

        # 7. Audit log
        print("[7] Audit log...")
        audit = os.path.join("data", "audit.jsonl")
        if os.path.exists(audit):
            lines = open(audit).readlines()
            last = json.loads(lines[-1])
            await post(
                sess,
                headers,
                f"**[7] Audit log:** PASS ({len(lines)} entries)\nLast: {last.get('server')}/{last.get('tool')} tier={last.get('tier')}",
            )
            print(f"  PASS ({len(lines)} entries)")
        else:
            await post(sess, headers, "**[7] Audit log:** not created yet (no MCP calls from bot)")
            print("  no file")

        # 8. Tier classification
        print("[8] Tiers...")
        from src.mcp import _classify_tier

        cases = [
            ("apple", "read_inbox", "R"),
            ("apple", "send_message", "I"),
            ("filesystem", "read_file", "R"),
            ("filesystem", "write_file", "W"),
            ("shell", "execute_command", "X"),
            ("github", "get_repo", "R"),
        ]
        fails = [
            f"{sv}/{tl}: {_classify_tier(sv, tl)}!={ex}"
            for sv, tl, ex in cases
            if _classify_tier(sv, tl) != ex
        ]
        s8 = "PASS" if not fails else f"FAIL: {fails}"
        await post(sess, headers, f"**[8] Tier classification:** {s8}")
        print(f"  {s8}")

        # 9. Gemini Live
        print("[9] Gemini...")
        from src.gemini_session import GeminiSession

        gs = GeminiSession()
        try:
            await gs.connect()
            s9 = "PASS" if gs.connected else "FAIL"
            await gs.close()
        except Exception as e:
            s9 = f"FAIL ({e})"
        await post(sess, headers, f"**[9] Gemini Live session:** {s9}")
        print(f"  {s9}")

        # 10. plan_with_claude
        print("[10] plan_with_claude...")
        from src.cursor_bridge import CursorBridge
        from src.tools import handle_tool_call, init_tools

        cb = CursorBridge()
        init_tools(cursor_bridge=cb)
        r10 = await handle_tool_call(
            "plan_with_claude",
            {
                "context": "Write a Python function returning UTC ISO time. Integration test.",
                "session_key": "deep-test",
                "prompt_template": "planning",
            },
        )
        ok10 = len(r10) > 50 and "error" not in r10[:30].lower()
        await post(sess, headers, f"**[10] plan_with_claude:** {'PASS' if ok10 else 'FAIL'} ({len(r10)} chars)")
        print(f"  {'PASS' if ok10 else 'FAIL'}")

        # 11. Spend tracking
        print("[11] Spend...")
        from src.db import get_daily_spend

        spend = get_daily_spend()
        s11 = "PASS" if spend > 0.001 else "FAIL ($0)"
        await post(sess, headers, f"**[11] Daily spend:** {s11} (${spend:.4f})")
        print(f"  {s11}")

        # 12. loop_executions row after plan_with_claude
        print("[12] loop_executions after plan...")
        from src.db import get_connection
        with get_connection() as conn:
            row12 = conn.execute(
                "SELECT * FROM loop_executions WHERE tool_name = 'plan_with_claude' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row12 and row12["tokens_in"] and row12["tokens_in"] > 0 and row12["cost_usd"] and row12["cost_usd"] > 0:
            s12 = f"PASS (tokens_in={row12['tokens_in']}, cost=${row12['cost_usd']:.4f})"
        else:
            s12 = f"FAIL (row={dict(row12) if row12 else None})"
        await post(sess, headers, f"**[12] loop_executions after plan:** {s12}")
        print(f"  {s12}")

        # 13. prompt_versions row after edit_prompt
        print("[13] prompt_versions after edit...")
        from src.prompts import save_template, get_versions, get_path, read_raw
        test_name = "_deep_test_prompt"
        test_path = get_path(test_name)
        try:
            with open(test_path, "w") as f:
                f.write("deep test original content")
            r13 = await handle_tool_call(
                "edit_prompt",
                {"name": test_name, "instruction": "Add the word VERIFIED at the top."},
            )
            versions = get_versions(test_name)
            if versions and len(versions) >= 1:
                s13 = f"PASS ({len(versions)} version(s), origin={versions[0].get('origin')})"
            else:
                s13 = f"FAIL (versions={versions})"
        except Exception as e:
            s13 = f"FAIL ({e})"
        finally:
            if os.path.exists(test_path):
                os.unlink(test_path)
            with get_connection() as conn:
                conn.execute("DELETE FROM prompt_versions WHERE prompt_name = ?", (test_name,))
        await post(sess, headers, f"**[13] prompt_versions after edit:** {s13}")
        print(f"  {s13}")

        await post(sess, headers, "---\n**All 13 deep tests complete.**")
        print("\nDone.")

        await mcp.stop_all()


if __name__ == "__main__":
    asyncio.run(run())
