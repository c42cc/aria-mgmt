"""Environment loading and configuration defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    # Discord
    discord_bot_token: str = os.getenv("DISCORD_APP_BOT_TOKEN", "")
    discord_guild_id: str = os.getenv("DISCORD_GUILD_ID", "")
    discord_voice_channel_id: str = os.getenv("DISCORD_VOICE_CHANNEL_ID", "")
    discord_text_channel_id: str = os.getenv("DISCORD_TEXT_CHANNEL_ID", "")
    discord_log_channel_id: str = os.getenv("DISCORD_LOG_CHANNEL_ID", "")
    authorized_user_ids: list[str] = field(default_factory=lambda: [
        uid.strip()
        for uid in os.getenv("AUTHORIZED_USER_IDS", "").split(",")
        if uid.strip()
    ])

    # APIs
    google_api_key: str = os.getenv("GEMINI_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    cursor_api_key: str = os.getenv("CURSOR_API_KEY", "")

    # Voice sidecar (Bot #2: discord.js, DAVE-capable, voice-only)
    discord_voice_bot_token: str = os.getenv("DISCORD_VOICE_BOT_TOKEN", "")
    authorized_voice_user_id: str = os.getenv("AUTHORIZED_VOICE_USER_ID", "")

    # Models
    # Voice model. gemini-3.1-flash-live-preview. The native-audio family was
    # pinned here before (more natural voice, less 429-throttled), but the Live
    # API native-audio path is fundamentally unreliable for the tool loop: it
    # intermittently 500s on TTS, emits 0 audio bytes under demand, and — the
    # real blocker — does NOT reliably emit function calls, so spoken requests
    # never reached do_with_claude (forensic 2026-06-16). 3.1-flash-live
    # transcribes cleanly and calls tools correctly (verified live: voice ->
    # transcribe -> tool_call -> audio reply, no throttle). If 3.1 demand
    # throttling (429/503) recurs that is an ops concern — a model that cannot
    # call tools is not an acceptable fallback. Override via GEMINI_MODEL in .env.
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-opus-4-6")
    cursor_model: str = os.getenv("CURSOR_MODEL", "composer-2")
    # Cheap distillation model for high-volume, low-stakes summarization
    # (Cursor thread roster). This is the sanctioned "memory synthesis may
    # use Haiku" exception to the Opus-only rule — never Sonnet. The slug is
    # set only here / via .env.
    distill_model: str = os.getenv("CLAUDE_DISTILL_MODEL", "claude-haiku-4-5")

    # Cost guardrails
    daily_spend_cap_usd: float = float(os.getenv("DAILY_SPEND_CAP_USD", "20"))
    per_session_claude_calls_max: int = int(os.getenv("PER_SESSION_CLAUDE_CALLS_MAX", "15"))
    per_session_cursor_runs_max: int = int(os.getenv("PER_SESSION_CURSOR_RUNS_MAX", "5"))
    do_with_claude_max_iterations: int = int(os.getenv("DO_WITH_CLAUDE_MAX_ITERATIONS", "30"))
    # Per-iteration output-token ceiling for the do_with_claude loop. Raised from
    # the old hardcoded 4096 so artifact builds (a full minimalist HTML page with
    # inline CSS) come back in ONE response instead of being truncated mid-tag —
    # the throttle behind "the HTML she sent looked thin". The $5 loop cost cap
    # remains the real backstop, so this only buys quality, not runaway spend.
    do_with_claude_max_output_tokens: int = int(os.getenv("DO_WITH_CLAUDE_MAX_OUTPUT_TOKENS", "8192"))
    # Per-run cost ceiling for a Claude Code thread (ClaudeAgentOptions.max_budget_usd).
    # The run stops itself at this client-side estimate; the daily cap is the outer bound.
    claude_code_max_budget_usd: float = float(os.getenv("CLAUDE_CODE_MAX_BUDGET_USD", "10"))

    # Wall-clock bound on each Anthropic request inside the agent loop. Without
    # this the SDK default (600s timeout x 2 retries) lets one hung request
    # stall the whole loop for ~20 minutes with no feedback. With it, the loop's
    # worst case is iterations * timeout, and a stuck call fails loudly instead.
    anthropic_timeout_sec: float = float(os.getenv("ANTHROPIC_TIMEOUT_SEC", "120"))

    # SpicyLit / Grok
    grok_api_key: str = os.getenv("GROK_API_KEY", "")
    discord_spicylit_channel_id: str = os.getenv("DISCORD_SPICYLIT_CHANNEL_ID", "")

    # External Cursor observer (hooks-driven, watches other IDE windows)
    cursor_event_host: str = os.getenv("UCS_CURSOR_EVENT_HOST", "127.0.0.1")
    cursor_event_port: int = int(os.getenv("UCS_CURSOR_EVENT_PORT", "8731"))
    # A running Cursor thread quiet for this long emits a one-shot 'stalled' buzz.
    cursor_stall_minutes: int = int(os.getenv("UCS_CURSOR_STALL_MINUTES", "15"))

    # Lurk-in-voice: when true, the voice sidecar stays connected to the
    # voice channel after the authorized user leaves (Gemini and audio
    # pipelines are torn down; only the WebSocket persists). This kills the
    # "bling-bling" double join sound the user otherwise hears on every
    # rejoin (one Discord notification for their join, one for the bot's
    # auto-join 700ms later) at the cost of the bot looking permanently
    # present in the channel. Off by default — opt-in.
    aria_lurk_in_voice: bool = os.getenv("ARIA_LURK_IN_VOICE", "false").lower() == "true"

    # Approvals model. Corbin opted OUT of per-command confirmation: tier-I
    # (irreversible) and tier-X (executable) MCP tools execute autonomously and
    # are still recorded in data/audit.jsonl (the ground-truth record of what
    # fired). Set CONFIRM_RISKY_TOOLS=true to restore the per-command gate.
    # Human taps belong on high-level *approaches* (propose_action), not on
    # every individual command.
    confirm_risky_tools: bool = os.getenv("CONFIRM_RISKY_TOOLS", "false").lower() == "true"
    # How long a propose_action recommendation waits for a tap before expiring.
    proposal_timeout_sec: float = float(os.getenv("PROPOSAL_TIMEOUT_SEC", "1800"))

    # 42c.pw account provisioning. 42c.pw is gated by shared HTTP Basic Auth on
    # the alive-river Fly app; "creating an account" = upserting a credential
    # into c42_public/.htpasswd and redeploying so the new login goes live.
    # create_42c_account encapsulates that as one deterministic tool so Aria
    # never improvises htpasswd/openssl shell commands (the 42c.pw failure).
    c42_public_dir: str = os.getenv(
        "C42_PUBLIC_DIR",
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "c42_public"
        ),
    )
    c42_url: str = os.getenv("C42_URL", "https://42c.pw/")
    c42_deploy_timeout_sec: float = float(os.getenv("C42_DEPLOY_TIMEOUT_SEC", "360"))

    # Co-presence: file delivery + awareness of what's around her.
    # Discord's default upload cap is 25 MiB; over it, `deliver` returns a typed
    # blocker (host-and-link), never a silent failure or fabricated "sent".
    discord_upload_limit_mb: float = float(os.getenv("DISCORD_UPLOAD_LIMIT_MB", "25"))
    # The dirs her ambient awareness (surroundings_summary) and recent_artifacts
    # scan newest-first and NON-recursively — where her exports/artifacts land.
    # This is the deliberate inverse of the panther failure's blind recursive find.
    artifact_dirs: list[str] = field(default_factory=lambda: [
        d.strip() for d in os.getenv("ARIA_ARTIFACT_DIRS", "~/Desktop,~/Downloads").split(",")
        if d.strip()
    ])

    # Paths
    data_dir: str = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    prompts_dir: str = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts")
    models_config: str = os.path.join(os.path.dirname(os.path.dirname(__file__)), "models.yaml")
    projects_registry: str = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "projects", "registry.md"
    )
    cursor_wrapper_dir: str = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "cursor_wrapper"
    )
    cursor_user_data_dir: str = os.path.expanduser("~/.cursor")
    # Chrome DevTools Protocol port for driving the real Cursor IDE as an
    # actuator (src/cursor_ide_driver.py). Cursor must be launched with
    # `--remote-debugging-port=<this>` for IDE sends to fire for real; enable
    # it once with `ops/cursor_ide_debug.sh`. When the port is closed the
    # driver returns a typed blocker naming this fix — it never falls back to a
    # blind paste, and never claims an unverified send (the 06:18 forensic).
    cursor_cdp_port: int = int(os.getenv("CURSOR_CDP_PORT", "9223"))


config = Config()
