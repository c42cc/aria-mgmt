"""Gemini 3.1 Live WebSocket session with bidirectional audio and function calling."""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
from typing import Any, Callable, Coroutine

from google import genai
from google.genai import types

from .config import config
from .prompts import load_template

log = logging.getLogger(__name__)


TOOL_DECLARATIONS = [
    types.FunctionDeclaration(
        name="plan_with_claude",
        description="Send a planning request to Claude Opus 4.6 for analysis, planning, architecture, or debugging strategy.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "prompt_template": types.Schema(type="STRING", description="Name of template in prompts/ (e.g. 'refactor', 'architecture', 'bug-analysis'). Defaults to 'planning'."),
                "context": types.Schema(type="STRING", description="Assembled context: user's request, file contents, prior plan, feedback."),
                "session_key": types.Schema(type="STRING", description="Discord thread ID (groups related planning calls)."),
            },
            required=["context", "session_key"],
        ),
    ),
    types.FunctionDeclaration(
        name="package_audit_findings",
        description=(
            "Turn the recent voice dialogue between you and Corbin into "
            "structured audit findings appended to "
            "`<workspace_root>/audit_findings.md`. Call this ONLY when "
            "Corbin explicitly asks you to package, wrap up, or write up "
            "what you just discussed during a UI audit review — never on "
            "every utterance. The tool reads the conversation buffer "
            "itself; you do not pass the dialogue as an argument. It "
            "posts a plain-English summary to #ucs and returns the "
            "finding titles. It does NOT send anything to Cursor — after "
            "this returns, ask Corbin \"Send to Cursor?\" and on yes "
            "call cursor_send (or wrap in propose_action for a "
            "tap-to-approve card)."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "agent_id": types.Schema(
                    type="STRING",
                    description=(
                        "Agent handle from cursor_agents — the Cursor "
                        "agent whose workspace owns audit_findings.md."
                    ),
                ),
                "scope_hint": types.Schema(
                    type="STRING",
                    description=(
                        "Optional short phrase from Corbin bounding "
                        "which part of the dialogue to package "
                        "(e.g. \"the date picker stuff\"). Leave empty "
                        "for the whole recent review."
                    ),
                ),
                "n_recent_turns": types.Schema(
                    type="INTEGER",
                    description=(
                        "How many recent conversation turns to read "
                        "(default 20, plenty for a short review)."
                    ),
                ),
                "session_key": types.Schema(
                    type="STRING",
                    description="Discord thread/channel id.",
                ),
            },
            required=["agent_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="build_with_cursor",
        description="Start a Cursor agent to build/edit code on a project using an approved plan.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "project": types.Schema(type="STRING", description="Project name from projects/registry.md."),
                "instruction": types.Schema(type="STRING", description="Approved plan + implementation instructions for Cursor."),
                "background": types.Schema(type="BOOLEAN", description="Run in background (default true). Returns session_id immediately."),
            },
            required=["project", "instruction"],
        ),
    ),
    types.FunctionDeclaration(
        name="query_cursor",
        description="Send a message to a running Cursor build session (e.g. answering a question it asked).",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "session_id": types.Schema(type="STRING"),
                "message": types.Schema(type="STRING"),
            },
            required=["session_id", "message"],
        ),
    ),
    types.FunctionDeclaration(
        name="cursor_status",
        description=(
            "Compact health summary across the Cursor fleet: registry size, "
            "agent status counts (running/waiting/finished/errored), SDK source "
            "counts, active DB sessions, daily spend. For per-agent detail use "
            "cursor_agents."
        ),
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="do_with_claude",
        description="Execute a complex multi-step task using Claude Opus 4.6 with tool access. Use for email, calendar, file management, research, sending iMessages, looking up contacts, or any non-coding task that requires reasoning and actions. For a compound request like 'make an account for X and text them', hand the whole thing to do_with_claude — it can create the 42c.pw account, look up the contact, read prior texts, and send the message in one go.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "task": types.Schema(type="STRING", description="Natural language description of what to do."),
                "session_key": types.Schema(type="STRING", description="Discord thread ID."),
            },
            required=["task", "session_key"],
        ),
    ),
    types.FunctionDeclaration(
        name="start_task",
        description=(
            "Start a DURABLE BACKGROUND task and let Corbin walk away. Use when he "
            "says 'do X in the background', 'work on this while I'm gone', 'kick off "
            "X', or for any longer job he doesn't want to wait on the line for. It "
            "returns immediately with a task number; the work continues out-of-band "
            "and outlives this call. I ping him when it finishes or hits a wall. For "
            "a quick thing he's waiting on right now, use do_with_claude instead."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "goal": types.Schema(type="STRING", description="What the task should accomplish, in natural language."),
                "session_key": types.Schema(type="STRING", description="Discord thread ID."),
            },
            required=["goal"],
        ),
    ),
    types.FunctionDeclaration(
        name="task_status",
        description=(
            "Check on a durable task — answers 'how's X going?', 'is that done "
            "yet?', 'what's the status of task 4?'. Reads the task object directly "
            "(not the chat), so it works even if the task was started in another "
            "session. With no id, reports the active tasks (or the most recent)."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "task_id": types.Schema(type="STRING", description="The task number to check; omit for active/most-recent."),
                "session_key": types.Schema(type="STRING", description="Discord thread ID."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="run_playbook",
        description=(
            "Run a named PLAYBOOK — an ordered list of tasks — end to end in the "
            "background. Use when Corbin says 'run my morning playbook', 'kick off "
            "the <name> playbook', or names a saved routine. I work through the "
            "steps in order, ping him as each finishes, and stop to ask only if one "
            "hits a wall. This is the 'name it and walk away' payoff. Use "
            "list_playbooks if unsure of the name."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "name": types.Schema(type="STRING", description="The playbook name (e.g. 'morning', 'example')."),
                "session_key": types.Schema(type="STRING", description="Discord thread ID."),
            },
            required=["name"],
        ),
    ),
    types.FunctionDeclaration(
        name="list_playbooks",
        description="List the available playbooks (saved ordered task routines Corbin can run).",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "session_key": types.Schema(type="STRING", description="Discord thread ID."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="create_42c_account",
        description="Create a login account on the 42c.pw website (shared HTTP Basic Auth) so someone can see what Corbin is working on. Adds the credential and redeploys so it goes live (~1-2 min), then returns the login URL plus the username and password to share. Use only for a standalone 'make an account for X' request; for 'make an account AND text them', route to do_with_claude instead so it does both.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "username": types.Schema(type="STRING", description="The account username/login (a short handle)."),
                "password": types.Schema(type="STRING", description="The account password."),
                "label": types.Schema(type="STRING", description="Optional note about who it's for, e.g. the person's name."),
            },
            required=["username", "password"],
        ),
    ),
    types.FunctionDeclaration(
        name="propose_action",
        description=(
            "Recommend a consequential approach to Corbin that he approves with ONE tap, "
            "then it runs autonomously with no further per-command confirmation. Use this "
            "instead of doing big/expensive/destructive/ambiguous things unannounced, and "
            "instead of asking him to confirm individual commands. For small or clearly-"
            "intended actions, just do them — don't propose. The proposal is pushed to his "
            "phone; you get an immediate ack and it runs when he approves."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "title": types.Schema(type="STRING", description="Short name of the recommended approach."),
                "why": types.Schema(type="STRING", description="1-2 sentences of context: what's going on and why this is the move."),
                "task": types.Schema(type="STRING", description="The full task to execute autonomously on approval."),
                "session_key": types.Schema(type="STRING", description="Discord thread/channel id."),
            },
            required=["title", "task"],
        ),
    ),
    types.FunctionDeclaration(
        name="remember",
        description="Store a durable fact in long-term memory (e.g. preferences, contacts, project details).",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "text": types.Schema(type="STRING", description="The fact to remember."),
            },
            required=["text"],
        ),
    ),
    types.FunctionDeclaration(
        name="recall",
        description="Search long-term memory for relevant facts.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "query": types.Schema(type="STRING", description="What to search for."),
            },
            required=["query"],
        ),
    ),
    types.FunctionDeclaration(
        name="confirm_action",
        description="User has approved or rejected a pending action that required confirmation. Call this after the user responds to a confirmation prompt.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "action_id": types.Schema(type="STRING", description="The ID of the pending action."),
                "approved": types.Schema(type="BOOLEAN", description="Whether the user approved."),
                "modifications": types.Schema(type="STRING", description="Optional changes the user requested before approving."),
            },
            required=["action_id", "approved"],
        ),
    ),
    types.FunctionDeclaration(
        name="cancel_current_task",
        description="Cancel the currently running task (build or multi-step action). Use when the user says stop, abort, cancel, or nevermind.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="quick_email_check",
        description="Fast read-only check of unread mail. Use for 'do I have any new emails' style questions; bypasses the full Claude loop.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="quick_calendar",
        description="Fast read-only check of upcoming calendar events. Use for 'what's on my calendar' style questions.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "days_ahead": types.Schema(type="INTEGER", description="Window in days (default 1 = today + tomorrow)."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="list_prompts",
        description="List all available prompt templates that define your behavior and tool personas. Use when the user asks what prompts you have or wants to see the prompt catalog.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="show_prompt",
        description="Read a prompt template and post the full text to the text channel. Use when the user asks to see a specific prompt. Speak a brief summary; the full text goes to the channel.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "name": types.Schema(type="STRING", description="Template name (e.g. 'gemini_system', 'planning', 'implementation')."),
            },
            required=["name"],
        ),
    ),
    types.FunctionDeclaration(
        name="edit_prompt",
        description="Edit a prompt template based on a natural-language instruction. Reads the current prompt, applies the change via Claude, saves it, and posts the new version to the text channel. Use when the user asks to change, update, or modify a prompt.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "name": types.Schema(type="STRING", description="Template name to edit."),
                "instruction": types.Schema(type="STRING", description="Natural-language description of the desired change."),
            },
            required=["name", "instruction"],
        ),
    ),
    types.FunctionDeclaration(
        name="rollback_prompt",
        description="Restore a prompt template to a previous version. Use when the user wants to undo a prompt edit. Call prompt_versions first to see available versions.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "name": types.Schema(type="STRING", description="Template name to rollback."),
                "version": types.Schema(type="INTEGER", description="Version number to restore."),
            },
            required=["name", "version"],
        ),
    ),
    types.FunctionDeclaration(
        name="prompt_versions",
        description="List all saved versions of a prompt template. Shows version numbers, when they were created, and how they originated (user edit, rollback, etc). Use before rollback_prompt.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "name": types.Schema(type="STRING", description="Template name to show history for."),
            },
            required=["name"],
        ),
    ),
    types.FunctionDeclaration(
        name="reload_prompts",
        description="Clear the prompt cache and reconnect your session so changes to your system prompt take effect immediately. Call after editing gemini_system. For other prompts, changes take effect on next use automatically.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="get_focused_app",
        description="Get the name and bundle ID of the frontmost Mac application. Use to check what app is currently focused before pasting.",
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="focus_app",
        description="Bring a Mac application to the front. Use when the user wants to paste into a specific app that isn't currently focused.",
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "app_name": types.Schema(type="STRING", description="Application name (e.g. 'Cursor', 'Notes', 'TextEdit', 'Slack')."),
            },
            required=["app_name"],
        ),
    ),
    types.FunctionDeclaration(
        name="dictate_into_focused_app",
        description=(
            "Type text into the frontmost Mac application by copying to clipboard and pasting. "
            "Use when the user says 'put this in', 'type into', 'paste into', or 'dictate into' an app. "
            "Call focus_app first if the target app is not already focused."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "text": types.Schema(type="STRING", description="The text to paste into the focused application."),
            },
            required=["text"],
        ),
    ),
    # ---- Unified Cursor agent surface ---------------------------------------
    # `agent_id` is the canonical handle. The CursorAgentRegistry routes SDK
    # agents (Aria-spawned) through the bridge and IDE agents (user-opened)
    # through osascript automatically. The JSONL tailer keeps state fresh
    # so follow-up questions ("what did it just say?") have an O(1) answer.
    types.FunctionDeclaration(
        name="cursor_agents",
        description=(
            "List every Cursor agent currently visible — IDE windows Corbin "
            "opened himself and SDK agents you spawned, on equal footing. Each "
            "entry carries `agent_id` (use it for follow-up calls), `source` "
            "(sdk|ide), `status`, `last_assistant_text`, and `pending_question`. "
            "Call this first when Corbin asks what's running."
        ),
        parameters=types.Schema(type="OBJECT", properties={}),
    ),
    types.FunctionDeclaration(
        name="cursor_threads",
        description=(
            "The way to answer 'what's going on in <project>?' or 'what is each "
            "thread?'. Lists the recent Cursor coding threads in a project "
            "(default live_visuals_4), each distilled into a plain-English card: "
            "a short label, what it set out to do, what it actually did, status, "
            "and any open question. Threads are Corbin's parallel Cursor agents; "
            "their real names are UUIDs, so read back the distilled labels — one "
            "tight line each (label — what it did — status). Reads the durable "
            "transcripts, so it is correct across restarts; do NOT answer thread "
            "questions from memory or watch events when you can call this."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "project": types.Schema(type="STRING", description="Project name (default 'live_visuals_4'), registry alias, or absolute workspace path."),
                "window_hours": types.Schema(type="NUMBER", description="Only threads active within this many hours (default 48)."),
                "limit": types.Schema(type="INTEGER", description="Max threads, newest first (default 12)."),
                "refresh": types.Schema(type="BOOLEAN", description="Re-distill even cached threads (default false)."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="cursor_read",
        description=(
            "Read the most recent transcript turns for ONE specific Cursor "
            "thread, to dig deeper after cursor_threads. Pass the thread handle "
            "as '<project>/<sid_prefix>' (e.g. 'live_visuals_4/57480d46') to "
            "target one exact thread — including a dormant one — or a bare "
            "project/agent handle for its current session. Includes recent plan "
            "files for the workspace."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "agent_id": types.Schema(type="STRING", description="Thread handle, e.g. 'live_visuals_4/57480d46', or a workspace/agent handle."),
                "n_turns": types.Schema(type="INTEGER", description="Number of recent turns to return (default 5, max 25)."),
                "sid": types.Schema(type="STRING", description="Optional explicit transcript sid (full or prefix) if not encoded in agent_id."),
            },
            required=["agent_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="cursor_send",
        description=(
            "Universal send to a Cursor agent. Routes by source: SDK agents go "
            "through the bridge (no osascript, no focus contests); IDE agents go "
            "through the existing paste-and-send path. Use this instead of the "
            "legacy send_to_cursor_chat / approve_cursor_plan / reject_cursor_plan. "
            "`kind` shapes the body: chat (default), new_agent, approve, reject, cancel."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "agent_id": types.Schema(type="STRING", description="Agent handle from cursor_agents."),
                "message": types.Schema(type="STRING", description="Refined message body (required for chat/new_agent; ignored for approve/reject/cancel unless customizing)."),
                "kind": types.Schema(type="STRING", description="One of: chat (default), new_agent, approve, reject, cancel."),
                "note": types.Schema(type="STRING", description="Optional note appended to approve/reject."),
            },
            required=["agent_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="cursor_spawn",
        description=(
            "Spawn a fresh `@cursor/sdk` agent in `workspace_root` with `instruction`. "
            "Returns the canonical `agent_id` so you can immediately cursor_send or "
            "cursor_read it. Prefer this over build_with_cursor for new work — no "
            "osascript involved, the agent is addressable by handle from the start."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "workspace_root": types.Schema(type="STRING", description="Absolute workspace directory."),
                "instruction": types.Schema(type="STRING", description="What the agent should do (precise, well-formed prompt)."),
                "model": types.Schema(type="STRING", description="Optional Cursor model override (defaults to composer-2)."),
            },
            required=["workspace_root", "instruction"],
        ),
    ),
    # ---- Claude Code (Aria drives Claude Code on a repo) --------------------
    types.FunctionDeclaration(
        name="claude_code_spawn",
        description=(
            "Start a NEW Claude Code thread on a repo and hand it an instruction. "
            "This is how you wield Claude Code (the migrated live_visuals_4_CC by "
            "default). Defaults to Plan Mode, so it proposes a plan to review/edit "
            "before any change. Returns session_id; then claude_code_read to see the "
            "plan and claude_code_send (kind=approve) to execute. Before spawning, "
            "say what you'll tell it and let Corbin adjust the instruction."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "workspace_root": types.Schema(type="STRING", description="Project name or absolute path; omit for the managed live_visuals_4_CC repo."),
                "instruction": types.Schema(type="STRING", description="The task for the Claude Code thread."),
                "mode": types.Schema(type="STRING", description="plan (default) | acceptEdits | default."),
            },
            required=["instruction"],
        ),
    ),
    types.FunctionDeclaration(
        name="claude_code_send",
        description=(
            "Send a follow-up to a live Claude Code thread, or approve / reject / "
            "cancel it. kind=approve proceeds with the reviewed plan; kind=chat sends "
            "a message (e.g. relaying Corbin's answer to its question); kind=cancel "
            "stops it. Get the agent_id from claude_code_threads / cursor_agents."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "agent_id": types.Schema(type="STRING", description="Thread handle from claude_code_threads/cursor_agents."),
                "message": types.Schema(type="STRING", description="Message for kind=chat, or note for approve/reject."),
                "kind": types.Schema(type="STRING", description="chat | approve | reject | cancel. Default chat."),
            },
            required=["agent_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="claude_code_read",
        description=(
            "Read a Claude Code thread's latest turns + status — its proposed plan, "
            "progress, or pending question. Omit agent_id for the managed repo's thread."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "agent_id": types.Schema(type="STRING", description="Thread handle; omit for the managed repo."),
                "n_turns": types.Schema(type="INTEGER", description="Recent turns (default 5, max 25)."),
            },
            required=[],
        ),
    ),
    types.FunctionDeclaration(
        name="claude_code_threads",
        description=(
            "List the Claude Code threads Aria is driving, with status and any "
            "pending questions. Use to find a handle before claude_code_read / send."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "project": types.Schema(type="STRING", description="Optional project filter."),
            },
            required=[],
        ),
    ),
    types.FunctionDeclaration(
        name="ask_user",
        description=(
            "Ask Corbin an OPEN question and wait for his reply, returning the answer. "
            "Use when you need an open answer a yes/no can't carry — especially to "
            "relay a Claude Code thread's pending question and feed his answer back "
            "via claude_code_send. For a simple approve/skip, use propose_action."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "question": types.Schema(type="STRING", description="The question to put to Corbin."),
            },
            required=["question"],
        ),
    ),
    types.FunctionDeclaration(
        name="cursor_screenshot",
        description=(
            "Screenshot a Cursor IDE window. No-op for SDK agents (they have no window). "
            "Use to confirm UI state before approving a plan or to inspect an unexpected dialog."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "agent_id": types.Schema(type="STRING", description="Agent handle from cursor_agents."),
                "save_path": types.Schema(type="STRING", description="Optional absolute PNG path. Defaults to data/screenshots/."),
            },
            required=["agent_id"],
        ),
    ),
    # ---- Discord text history ----------------------------------------------
    # Read-only windows into Discord channel and thread history. Use to catch
    # up on what Corbin said, what Cursor build threads logged, what landed
    # in #ucs-alerts overnight, and so on.
    types.FunctionDeclaration(
        name="discord_recent_messages",
        description=(
            "Return the most recent messages from a Discord text channel or thread, "
            "oldest-first. `channel` accepts a numeric id, a channel name (with or "
            "without `#`), an alias (`ucs` for the text channel, `alerts` for "
            "#ucs-alerts, `spicy-lit`), or a thread name from discord_list_threads. "
            "Use this to catch up on history Corbin or Cursor wrote while you "
            "weren't listening, especially when he asks 'what did Cursor say in the "
            "build thread?' — call discord_list_threads first to find the thread."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "channel": types.Schema(type="STRING", description="Channel id, name, alias, or thread name. Defaults to 'ucs'."),
                "limit": types.Schema(type="INTEGER", description="How many recent messages to return (default 20, max 100)."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="discord_list_threads",
        description=(
            "List active threads under a parent Discord channel. The default `ucs` "
            "lists every active thread in the text channel, including build threads "
            "that Aria's cursor_spawn pipeline creates per SDK agent. Pair with "
            "discord_recent_messages(channel=<thread_name>) to read a specific "
            "thread's transcript."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "channel": types.Schema(type="STRING", description="Parent channel id, name, or alias. Defaults to 'ucs'."),
            },
        ),
    ),
    # ---- DGX Spark control --------------------------------------------------
    # Aria's voice handle on the two GB10 nodes (spark1, spark2). Backed by the
    # shared catalog in src/spark.py — the same code the CLI acceptance harness
    # runs. status is read-only and instant; verify is the heavy "prove it twice"
    # acceptance; setup is executable (re-provisions a node).
    types.FunctionDeclaration(
        name="spark_status",
        description=(
            "Read-only health of the DGX Spark nodes (the two GB10 boxes, spark1 and "
            "spark2): identity, GPU + unified memory, the user-level toolchain and "
            "versions, and the high-speed cluster-link state. Instant, free, no side "
            "effects. Leave node empty to check BOTH. Use for 'how are the sparks?', "
            "'are the sparks up?', 'check spark2'. Speak a short summary; the full "
            "JSON is the tool result."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "node": types.Schema(type="STRING", description="'spark1', 'spark2', or empty/'all' for both."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="spark_verify",
        description=(
            "Run the full Section-A acceptance on ONE spark node: every good-state gate "
            "is proven twice — a machine assertion over the live SSH output AND an "
            "independent Gemini reading of a real macOS-Terminal screenshot — and any "
            "disagreement is a loud FAIL. This opens Terminal windows + takes "
            "screenshots on the Mac and takes a few minutes; it posts a per-gate report "
            "to the text channel. Use only when the user wants the sparks PROVEN good, "
            "not just pinged — for a quick check use spark_status. Tell the user it'll "
            "take a few minutes."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "node": types.Schema(type="STRING", description="'spark1' or 'spark2'."),
                "role": types.Schema(type="STRING", description="Worker role 'A' or 'B'; defaults to the node's role (spark1=A, spark2=B)."),
                "only": types.Schema(type="STRING", description="Optional comma-separated gate ids (e.g. 'gpu,mcp'); default runs all."),
            },
            required=["node"],
        ),
    ),
    types.FunctionDeclaration(
        name="spark_setup",
        description=(
            "Executable: provision or repair a spark node by running setup_node.sh over "
            "SSH (idempotent — installs Claude Code + the toolchain, writes settings + "
            "identity, registers the filesystem MCP, seeds the API key). Use to fix a "
            "node that spark_verify flagged red or to bring a fresh node to the good "
            "state. Consequential — for anything beyond an obvious repair, offer it via "
            "propose_action so Corbin taps to approve."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "node": types.Schema(type="STRING", description="'spark1' or 'spark2'."),
                "role": types.Schema(type="STRING", description="Worker role 'A' or 'B'; defaults to the node's role."),
            },
            required=["node"],
        ),
    ),
    types.FunctionDeclaration(
        name="backup_model",
        description=(
            "Back up a Hugging Face model to encrypted cold storage. Launches a diskless "
            "modelvault backup on an ephemeral cloud VM that streams the model into GCS and "
            "self-deletes; it returns right away (it starts the job, it does not wait for the "
            "multi-terabyte transfer). Use when the user says 'back up <model>', e.g. 'back up "
            "huggingface.co/org/model'. Speak a short confirmation and offer to check on it later."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "url": types.Schema(type="STRING", description="Model URL or id, e.g. 'huggingface.co/org/model' or 'org/model'."),
            },
            required=["url"],
        ),
    ),
    types.FunctionDeclaration(
        name="spark_cc_sync",
        description=(
            "Stand up or UPDATE the live_visuals_4 Claude Code workspace on a spark node — "
            "rsync the repo + overlay the SAME .claude/.mcp.json control-plane we use on the "
            "Mac + rebuild the venvs and node_modules. Idempotent ('just update it'). Takes a "
            "few minutes; tell the user. Run before the first spark_run. It does NOT log claude "
            "in — that is a one-time `claude /login` on the node (see spark_cc_auth)."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "node": types.Schema(type="STRING", description="'spark1' or 'spark2' (default spark1)."),
                "mirror": types.Schema(type="BOOLEAN", description="Pristine re-mirror (rsync --delete). Default false."),
                "smoke_gate": types.Schema(type="BOOLEAN", description="Run the quality gate after bootstrap to record a baseline."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="spark_cc_auth",
        description=(
            "Check whether a spark node's claude is on the Max subscription. If not, tell the "
            "user to run, once, in their own terminal: `ssh -t <node>` then `claude` and "
            "`/login`. Use this before launching a run; set probe=true to confirm with a real "
            "round-trip (spends a tiny bit)."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "node": types.Schema(type="STRING", description="'spark1' or 'spark2' (default spark1)."),
                "probe": types.Schema(type="BOOLEAN", description="Spend one tiny subscription call to confirm a live round-trip."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="spark_run",
        description=(
            "Executable: launch the forensic AUDIT + COLLAPSE run on a spark node as a detached "
            "job that survives disconnects. It refreshes the collapse ledger, then performs the "
            "collapses wave-by-wave on a new branch, running the quality gate after each wave. "
            "Defaults to the audit reasoning policy: Opus 4.8, medium effort, no extended "
            "thinking. Returns a run id right away and I watch it in the background, reporting "
            "when it finishes — no need to stay connected. Requires spark_cc_sync done and the "
            "node logged in. Consequential — offer via propose_action so Corbin taps to approve."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "node": types.Schema(type="STRING", description="'spark1' or 'spark2' (default spark1)."),
                "branch": types.Schema(type="STRING", description="Branch to create/use; default collapse/<date>."),
                "mode": types.Schema(type="STRING", description="Claude permission mode: 'bypassPermissions' (default), 'acceptEdits', or 'plan'."),
                "effort": types.Schema(type="STRING", description="Adaptive reasoning effort low/medium/high/xhigh/max; default medium for the audit."),
                "extended_thinking": types.Schema(type="BOOLEAN", description="Enable extended thinking; default false for the audit."),
            },
        ),
    ),
    types.FunctionDeclaration(
        name="spark_run_status",
        description=(
            "Read-only check on a detached spark run (from spark_run): running or finished, exit "
            "code, branch and commit count, the last thing it said / current tool, and notional "
            "cost. Safe to call anytime; speak a short summary."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "node": types.Schema(type="STRING", description="'spark1' or 'spark2' (default spark1)."),
                "run_id": types.Schema(type="STRING", description="The run id from spark_run."),
            },
            required=["run_id"],
        ),
    ),
    types.FunctionDeclaration(
        name="spark_run_fetch",
        description=(
            "Pull a finished spark run's results back to the Mac: the run log, the refreshed "
            "ledger, and an importable git bundle of the collapse branch. Use once a run is done "
            "to bring the work home so it can be pushed."
        ),
        parameters=types.Schema(
            type="OBJECT",
            properties={
                "node": types.Schema(type="STRING", description="'spark1' or 'spark2' (default spark1)."),
                "run_id": types.Schema(type="STRING", description="The run id from spark_run."),
                "branch": types.Schema(type="STRING", description="Branch to bundle; default = the run's current branch."),
            },
            required=["run_id"],
        ),
    ),
]

TranscriptEntry = collections.namedtuple("TranscriptEntry", ["role", "text", "ts"])


class GeminiSession:
    """Manages a Gemini Live session with audio streaming and tool dispatch."""

    def __init__(
        self,
        tool_handler: Callable[..., Coroutine] | None = None,
        transcript_callback: Callable[[str, str], Coroutine] | None = None,
        orphan_callback: Callable[[str, str, str], Coroutine] | None = None,
    ):
        """
        transcript_callback(role, text) is invoked once per *completed* turn
        with role in {"user", "aria"} and the full transcribed text. Used
        by bot.py to record the turn into the shared ConversationBuffer
        and mirror it to the voice-channel text chat.

        orphan_callback(tool_name, fc_id, result_text) is invoked when a
        tool dispatch finishes but the session has already closed so the
        result cannot be sent back to Gemini. This is the loud-failure
        signal for L1 — a side-effect happened (MCP write/send) but the
        model never heard about it. The bot routes this to #ucs-alerts.
        """
        self.tool_handler = tool_handler
        self.transcript_callback = transcript_callback
        self.orphan_callback = orphan_callback
        # Barge-in sink. bot.py sets this to voice_bridge.flush_playback so the
        # instant Gemini reports `interrupted`, the sidecar drops its buffered
        # audio in lockstep with us clearing _audio_out_queue — the two halves
        # of "stop talking NOW". None on the local-mic path (SpeakerOutput owns
        # playback there).
        self.interrupt_callback: Callable[[], Coroutine] | None = None
        self._client: genai.Client | None = None
        self._session: Any = None
        self._session_ctx: Any = None
        self._audio_out_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._receive_task: asyncio.Task | None = None
        self._connected = False

        # Idle gate: set() means the model is NOT currently producing a turn
        # (no model_turn parts in-flight, last turn_complete fired). Cleared
        # when a model_turn arrives, set again on turn_complete or interrupted.
        # External callers (e.g. /aria_say) use `wait_until_idle()` to avoid
        # batching their inject into a turn the model is already generating.
        # Defaults to set so the very first inject after connect doesn't block
        # forever waiting for a turn that hasn't started yet.
        self._idle_event: asyncio.Event = asyncio.Event()
        self._idle_event.set()

        self._transcript_buffer: collections.deque[TranscriptEntry] = collections.deque(maxlen=100)

        # Per-turn accumulators; flushed to the buffer and the callback
        # when Gemini signals turn_complete (or the session is closing).
        self._user_turn_acc: str = ""
        self._aria_turn_acc: str = ""

        self._pending_confirmations: dict[str, asyncio.Event] = {}
        self._confirmation_results: dict[str, dict] = {}

        self._lifecycle_lock = asyncio.Lock()
        self._served_fc_ids: set[str] = set()

        # Track in-flight dispatch tasks so we can await them on close.
        # Without this, _do_close cancels the receive loop but a side-
        # effecting tool can still be running unobserved, lose its
        # response, and leave the model unaware of what happened. The
        # close path now awaits these with a bounded timeout (L1 fix).
        self._dispatch_tasks: set[asyncio.Task] = set()
        # Realtime input (audio / audio_stream_end / activity) MUST be suppressed
        # while a tool call is in flight: Gemini Live closes the socket with 1008
        # ("Operation is not implemented…") if any send_realtime_input arrives
        # between a tool_call and its send_tool_response (forensic 2026-06-16,
        # confirmed on the Google AI dev forum). A counter (not a bool) so
        # concurrent dispatches each gate and ungate correctly.
        self._pending_tool_calls: int = 0

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        """Establish Gemini Live WebSocket connection.

        Idempotent: returns immediately if already connected with a live
        receive task. All callers are safe to call without external guards.
        """
        async with self._lifecycle_lock:
            if self._connected and self._receive_task and not self._receive_task.done():
                log.debug("connect() called but already connected — skipping")
                return
            await self._do_connect()
            self._receive_task = asyncio.create_task(self._receive_loop())
        log.info("Gemini Live session connected (model=%s)", config.gemini_model)

    async def _do_connect(self) -> None:
        """Create a new Gemini Live session. Caller must hold _lifecycle_lock."""
        if not config.google_api_key:
            raise RuntimeError("GEMINI_API_KEY not set")

        self._user_turn_acc = ""
        self._aria_turn_acc = ""

        self._client = genai.Client(api_key=config.google_api_key)

        live_config = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name="Kore",
                    ),
                ),
            ),
            tools=TOOL_DECLARATIONS,
            system_instruction=types.Content(
                parts=[types.Part(text=load_template("gemini_system"))]
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

        self._session_ctx = self._client.aio.live.connect(
            model=config.gemini_model, config=live_config
        )
        self._session = await self._session_ctx.__aenter__()
        self._connected = True

    async def send_audio(self, pcm_bytes: bytes) -> None:
        """Send PCM audio chunk (16kHz mono int16) to Gemini.

        Dropped while a tool call is pending — Gemini Live rejects realtime
        input during the tool_call→tool_response window and closes with 1008.
        """
        if not self._session or not self._connected or self._pending_tool_calls > 0:
            return
        try:
            await self._session.send_realtime_input(
                audio=types.Blob(data=pcm_bytes, mime_type="audio/pcm;rate=16000")
            )
        except Exception:
            log.exception("Failed to send audio to Gemini")
            self._connected = False

    async def signal_audio_end(self) -> None:
        """Signal end of the current user audio stream.

        Production use is rare — the Discord voice path streams continuously
        and Gemini's automatic VAD detects end-of-turn from silence. This
        method exists for tests (`scripts/e2e_aria_golden.py --tts` via the
        observer's /test_voice_in endpoint) that synthesize a finite TTS
        utterance and then need to deterministically end the user turn so
        Aria responds without waiting for VAD timeout.
        """
        if not self._session or not self._connected or self._pending_tool_calls > 0:
            return
        try:
            await self._session.send_realtime_input(audio_stream_end=True)
        except Exception:
            log.exception("Failed to signal audio_stream_end to Gemini")

    async def inject_text(self, text: str, turn_complete: bool = True) -> None:
        """Inject text into the Gemini session context.

        turn_complete=True: Gemini responds immediately (use for questions, confirmations).
        turn_complete=False: Added to context silently (use for session resume, background info).
        """
        if not self._session or not self._connected:
            return
        try:
            await self._session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]),
                turn_complete=turn_complete,
            )
        except Exception:
            log.exception("Failed to inject text into Gemini session")

    async def wait_until_idle(self, timeout: float = 15.0) -> bool:
        """Block until the model is not actively generating a turn.

        Returns True when idle (or we never went non-idle), False on timeout.
        Use this BEFORE calling `inject_text(..., turn_complete=True)` from
        an out-of-band path like the /aria_say HTTP endpoint — without it,
        an inject that arrives mid-turn (e.g. while the voice-join preamble
        is still being spoken) gets batched into the in-progress generation
        and never produces its own audible response.
        """
        if not self._connected:
            return True
        if self._idle_event.is_set():
            return True
        try:
            await asyncio.wait_for(self._idle_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def get_audio(self) -> bytes:
        """Get the next audio chunk from the output queue."""
        return await self._audio_out_queue.get()

    def _drain_audio_out_queue(self) -> int:
        """Drop every buffered outbound audio chunk; return how many.

        Barge-in: Gemini emits a whole turn's audio faster than realtime, so up
        to maxsize chunks of Aria's speech sit queued here when the user cuts in.
        Left in place they keep flowing to the sidecar and she talks over him for
        seconds. Synchronous + non-blocking so the receive loop empties it inline,
        before the next chunk can reach the pump."""
        dropped = 0
        while True:
            try:
                self._audio_out_queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        return dropped

    def get_transcript_context(self, max_turns: int = 5) -> str:
        """Get recent transcript for session reconnect context."""
        recent = list(self._transcript_buffer)[-max_turns:]
        if not recent:
            return ""
        lines = [f"{e.role}: {e.text}" for e in recent if e.text]
        return "\n".join(lines)

    def get_recent_transcript(self, max_turns: int = 3) -> list[dict[str, str]]:
        """Structured recent transcript for session record capture."""
        return [
            {"role": e.role, "text": e.text}
            for e in list(self._transcript_buffer)[-max_turns:]
            if e.text
        ]

    async def _flush_turn_accumulators(self) -> None:
        """Emit accumulated per-turn transcripts to the callback, then reset.

        Called on every `turn_complete` (and `interrupted`) signal from
        Gemini. The callback is invoked at most once per role per turn
        with the full transcript text. If the callback raises, we log
        and continue — a misbehaving downstream must not break voice.
        """
        user_text = self._user_turn_acc.strip()
        aria_text = self._aria_turn_acc.strip()
        self._user_turn_acc = ""
        self._aria_turn_acc = ""

        if not self.transcript_callback:
            return

        if user_text:
            try:
                await self.transcript_callback("user", user_text)
            except Exception:
                log.exception("transcript_callback failed for user turn")
        if aria_text:
            try:
                await self.transcript_callback("aria", aria_text)
            except Exception:
                log.exception("transcript_callback failed for aria turn")

    async def wait_for_confirmation(self, action_id: str, timeout: float = 60.0) -> dict:
        """Wait for a confirm_action tool call with the given action_id."""
        event = asyncio.Event()
        self._pending_confirmations[action_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return self._confirmation_results.pop(action_id, {"approved": False})
        except asyncio.TimeoutError:
            return {"approved": False, "timeout": True}
        finally:
            self._pending_confirmations.pop(action_id, None)

    async def _receive_loop(self) -> None:
        """Main receive loop: process audio, transcriptions, and tool calls from Gemini."""
        backoff = 1.0
        max_backoff = 30.0

        while True:
            try:
                if not self._session or not self._connected:
                    break

                async for msg in self._session.receive():
                    if not self._connected:
                        return
                    backoff = 1.0

                    # Opt-in debug trace: ARIA_GEMINI_RECV_DEBUG=1 logs every
                    # Gemini Live message shape. Off by default. Useful for
                    # diagnosing TTS-driven E2E runs where audio is sent but
                    # tool calls are missing (e.g. Aria hallucinating instead
                    # of calling the requested tool).
                    if os.getenv("ARIA_GEMINI_RECV_DEBUG"):
                        kinds: list[str] = []
                        if msg.server_content:
                            sc_dbg = msg.server_content
                            if sc_dbg.model_turn:
                                kinds.append(f"model_turn[parts={len(sc_dbg.model_turn.parts)}]")
                            if sc_dbg.input_transcription and sc_dbg.input_transcription.text:
                                kinds.append(f"input_tx[{sc_dbg.input_transcription.text[:40]!r}]")
                            if sc_dbg.output_transcription and sc_dbg.output_transcription.text:
                                kinds.append(f"output_tx[{sc_dbg.output_transcription.text[:40]!r}]")
                            if getattr(sc_dbg, "turn_complete", False):
                                kinds.append("turn_complete")
                            if getattr(sc_dbg, "interrupted", False):
                                kinds.append("interrupted")
                        if msg.tool_call:
                            kinds.append(f"tool_call[{len(msg.tool_call.function_calls)} fcs]")
                        if msg.setup_complete is not None:
                            kinds.append("setup_complete")
                        log.info("[recv] %s", ",".join(kinds) or "empty")

                    if msg.server_content:
                        sc = msg.server_content
                        if sc.model_turn:
                            # Model just started (or is still) generating a
                            # turn — gate any defer-aware injectors out
                            # until turn_complete fires below.
                            self._idle_event.clear()
                            for part in sc.model_turn.parts:
                                if part.inline_data and part.inline_data.data:
                                    # AUDIO TELEMETRY (stage A): prove Gemini is
                                    # actually emitting voice bytes. Silence here
                                    # means the model produced no audio (preview
                                    # 503/429/modality), not a transport break.
                                    _n = len(part.inline_data.data)
                                    self._audio_out_chunks = getattr(self, "_audio_out_chunks", 0) + 1
                                    self._audio_out_bytes = getattr(self, "_audio_out_bytes", 0) + _n
                                    if self._audio_out_chunks == 1 or self._audio_out_chunks % 100 == 0:
                                        log.info(
                                            "AUDIO[A gemini-out]: chunk #%d (%d bytes; %d total, ~%.1fs @24k)",
                                            self._audio_out_chunks, _n, self._audio_out_bytes,
                                            self._audio_out_bytes / 48000.0,
                                        )
                                    try:
                                        self._audio_out_queue.put_nowait(part.inline_data.data)
                                    except asyncio.QueueFull:
                                        try:
                                            self._audio_out_queue.get_nowait()
                                        except asyncio.QueueEmpty:
                                            pass
                                        self._audio_out_queue.put_nowait(part.inline_data.data)

                                if part.text:
                                    self._transcript_buffer.append(
                                        TranscriptEntry("assistant", part.text, time.time())
                                    )
                                    self._aria_turn_acc += part.text

                        if sc.input_transcription and sc.input_transcription.text:
                            self._transcript_buffer.append(
                                TranscriptEntry("user", sc.input_transcription.text, time.time())
                            )
                            self._user_turn_acc += sc.input_transcription.text

                        if sc.output_transcription and sc.output_transcription.text:
                            self._transcript_buffer.append(
                                TranscriptEntry("assistant", sc.output_transcription.text, time.time())
                            )
                            self._aria_turn_acc += sc.output_transcription.text

                        interrupted = getattr(sc, "interrupted", False)
                        if interrupted:
                            # Barge-in: the user spoke over Aria. Silence her on
                            # the beat — drop OUR buffered audio AND tell the
                            # sidecar to drop its FFmpeg/AudioResource buffer.
                            # Either half alone still leaves seconds of speech
                            # draining out while he says "stop".
                            dropped = self._drain_audio_out_queue()
                            if dropped:
                                log.info("barge-in: dropped %d buffered audio chunk(s)", dropped)
                            if self.interrupt_callback:
                                try:
                                    await self.interrupt_callback()
                                except Exception:
                                    log.exception("interrupt_callback (sidecar flush) failed")
                        if getattr(sc, "turn_complete", False) or interrupted:
                            await self._flush_turn_accumulators()
                            self._idle_event.set()

                    elif msg.tool_call:
                        seen_in_turn: set[str] = set()
                        for fc in msg.tool_call.function_calls:
                            if fc.id in self._served_fc_ids:
                                log.info("Skipping already-served fc.id=%s (%s)", fc.id, fc.name)
                                continue

                            dedup_key = f"{fc.name}:{json.dumps(dict(fc.args) if fc.args else {}, sort_keys=True)}"
                            if dedup_key in seen_in_turn:
                                log.info("Skipping duplicate in-turn call: %s", fc.name)
                                continue
                            seen_in_turn.add(dedup_key)
                            self._served_fc_ids.add(fc.id)

                            log.info("Gemini tool call: %s(%s)", fc.name, fc.id)

                            if fc.name == "confirm_action":
                                self._pending_tool_calls += 1
                                try:
                                    args = dict(fc.args) if fc.args else {}
                                    action_id = args.get("action_id", "")
                                    if action_id in self._pending_confirmations:
                                        self._confirmation_results[action_id] = {
                                            "approved": args.get("approved", False),
                                            "modifications": args.get("modifications"),
                                        }
                                        self._pending_confirmations[action_id].set()
                                    await self._session.send_tool_response(
                                        function_responses=types.FunctionResponse(
                                            name=fc.name,
                                            response={"result": "confirmation recorded"},
                                            id=fc.id,
                                        )
                                    )
                                finally:
                                    self._pending_tool_calls = max(0, self._pending_tool_calls - 1)
                                continue

                            if self.tool_handler:
                                # Gate realtime input until this tool's response
                                # is sent — the done-callback fires after
                                # _dispatch_tool_call returns (post
                                # send_tool_response), which reopens the gate.
                                self._pending_tool_calls += 1
                                task = asyncio.create_task(self._dispatch_tool_call(fc))
                                self._dispatch_tasks.add(task)
                                task.add_done_callback(self._dispatch_tasks.discard)
                                task.add_done_callback(self._on_dispatch_done)

            except asyncio.CancelledError:
                log.info("Gemini receive loop cancelled")
                self._connected = False
                return

            except Exception:
                log.exception("Gemini receive loop error — reconnecting in %.0fs", backoff)
                self._connected = False

                if self._session:
                    try:
                        await self._session.close()
                    except Exception:
                        pass
                    self._session = None
                if self._session_ctx:
                    try:
                        await self._session_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
                    self._session_ctx = None

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

                try:
                    await self._do_connect()
                    context = self.get_transcript_context(max_turns=3)
                    if context:
                        await self.inject_text(
                            f"Session resumed. Recent context:\n{context}",
                            turn_complete=False,
                        )
                    log.info("Gemini session reconnected after error")
                except Exception:
                    log.exception("Gemini reconnect failed")
                    self._connected = False
                    return

    def _on_dispatch_done(self, _task: asyncio.Task) -> None:
        """A dispatched tool call finished (response sent, or orphaned) —
        reopen the realtime-input gate so audio can resume."""
        self._pending_tool_calls = max(0, self._pending_tool_calls - 1)

    async def _dispatch_tool_call(self, fc: Any) -> None:
        """Run a tool handler and send the response back to Gemini.

        Runs as a separate task so the receive loop stays free to process
        confirm_action while tier-I/X tools await user approval. Tracked in
        `self._dispatch_tasks` so close() can await in-flight dispatches.
        """
        try:
            result = await self.tool_handler(
                fc.name, dict(fc.args) if fc.args else {}
            )
        except Exception as e:
            log.exception("Tool handler error for %s", fc.name)
            result = f'{{"error": "{e}"}}'

        if not self._session or not self._connected:
            # The session went away while the tool was running. The side
            # effect may have already happened (Gmail send, calendar create,
            # filesystem write). Surface this loudly — Gemini will never see
            # the result, but the operator must.
            log.error(
                "ORPHAN TOOL RESULT: %s (id=%s) completed but session closed "
                "before response could be sent. Result preview: %s",
                fc.name, getattr(fc, "id", "?"), str(result)[:300],
            )
            if self.orphan_callback:
                try:
                    await self.orphan_callback(fc.name, getattr(fc, "id", ""), str(result))
                except Exception:
                    log.exception("orphan_callback failed for %s", fc.name)
            return

        try:
            await self._session.send_tool_response(
                function_responses=types.FunctionResponse(
                    name=fc.name,
                    response={"result": result},
                    id=fc.id,
                )
            )
        except Exception:
            log.exception("Failed to send tool response for %s", fc.name)
            if self.orphan_callback:
                try:
                    await self.orphan_callback(fc.name, getattr(fc, "id", ""), str(result))
                except Exception:
                    log.exception("orphan_callback failed (send-tool-response branch) for %s", fc.name)

    async def reconnect(self) -> None:
        """Gracefully close and reopen the session with fresh prompts.

        Preserves recent transcript context across the reconnect so
        the conversation feels continuous.
        """
        async with self._lifecycle_lock:
            from .prompts import clear_cache
            clear_cache()
            self._served_fc_ids.clear()
            context = self.get_transcript_context(max_turns=5)
            await self._do_close()
            await self._do_connect()
            self._receive_task = asyncio.create_task(self._receive_loop())
        if context:
            await self.inject_text(
                f"Session resumed after prompt reload. Recent context:\n{context}",
                turn_complete=False,
            )
        log.info("Gemini session reconnected after prompt reload")

    async def close(self) -> None:
        """Close the Gemini session. Idempotent."""
        async with self._lifecycle_lock:
            await self._do_close()

    async def _do_close(self) -> None:
        """Internal close. Caller must hold _lifecycle_lock."""
        if not self._connected and not self._session and not self._session_ctx:
            return
        # If a tier-X/I confirmation is pending, the receive loop still
        # needs to read a confirm_action function call before we can
        # safely close. Setting _connected=False up front would drop
        # that incoming function call on the floor and force the
        # confirmation to time out → declined. Defer the flip until
        # after the in-flight wait below, when confirmations are
        # explicitly accounted for.
        had_pending_confirmations = bool(self._pending_confirmations)
        if not had_pending_confirmations:
            self._connected = False
        # Release anyone awaiting wait_until_idle so they don't hang past
        # session teardown.
        self._idle_event.set()
        try:
            await self._flush_turn_accumulators()
        except Exception:
            log.exception("Error flushing turn accumulators on close")

        # Wait briefly for in-flight tool dispatches to finish so their
        # results can be sent back to Gemini before the session closes.
        # Bounded by 5s normally — beyond that we accept orphan-tool-
        # result loss and surface it via orphan_callback. Without this
        # wait, every in-flight tool at close time becomes a silent
        # loss (L1).
        #
        # Pending tier-X/I confirmations extend the wait to ~65s — they
        # are bounded by the 60s wait_for_confirmation timeout, plus a
        # small margin for the close path itself. Without this, a
        # premature close (e.g. from the idle watchdog) would race the
        # confirmation window and turn every text-initiated tier-X tool
        # into a declined timeout.
        in_flight = {t for t in self._dispatch_tasks if not t.done()}
        close_grace = 65.0 if had_pending_confirmations else 5.0
        if in_flight or had_pending_confirmations:
            if in_flight:
                log.info(
                    "Waiting up to %.0fs for %d in-flight tool dispatch(es) before close%s",
                    close_grace, len(in_flight),
                    " (pending confirmations)" if had_pending_confirmations else "",
                )
            elif had_pending_confirmations:
                log.info(
                    "Waiting up to %.0fs for %d pending tier-X/I confirmation(s) before close",
                    close_grace, len(self._pending_confirmations),
                )
            # We may also need to wait on bare pending-confirmation
            # events even when no dispatch task is in flight (the loop
            # awaits the event directly).
            confirm_tasks: set[asyncio.Task] = {
                asyncio.create_task(ev.wait(), name=f"close_wait_confirm_{aid}")
                for aid, ev in self._pending_confirmations.items()
                if not ev.is_set()
            }
            try:
                wait_set = in_flight | confirm_tasks
                if wait_set:
                    _, pending_after = await asyncio.wait(
                        wait_set, timeout=close_grace
                    )
                else:
                    pending_after = set()
            finally:
                for t in confirm_tasks:
                    if not t.done():
                        t.cancel()
            for t in pending_after:
                if t in in_flight:
                    log.error(
                        "Tool dispatch did not finish within close window — cancelling. "
                        "Side effect may have completed without a model-visible response."
                    )
                    t.cancel()
            # Now flip _connected so the receive loop and tool senders
            # observe the close.
            self._connected = False
        # Always ensure _connected is False from here on; covers the
        # path where there were no in-flight tasks at all.
        self._connected = False

        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._session_ctx:
            try:
                await self._session_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._session_ctx = None
            self._session = None
        elif self._session:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
        log.info("Gemini session closed")
