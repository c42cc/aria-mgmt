"""Environment loading and configuration defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    # Discord
    discord_bot_token: str = os.getenv("DISCORD_BOT_TOKEN", "")
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
    google_api_key: str = os.getenv("GOOGLE_API_KEY", "")
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    cursor_api_key: str = os.getenv("CURSOR_API_KEY", "")

    # Models
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-3.1-live")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-opus-4-6")
    cursor_model: str = os.getenv("CURSOR_MODEL", "composer-2")

    # Cost guardrails
    daily_spend_cap_usd: float = float(os.getenv("DAILY_SPEND_CAP_USD", "20"))
    per_session_claude_calls_max: int = int(os.getenv("PER_SESSION_CLAUDE_CALLS_MAX", "15"))
    per_session_cursor_runs_max: int = int(os.getenv("PER_SESSION_CURSOR_RUNS_MAX", "5"))

    # Paths
    data_dir: str = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    prompts_dir: str = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts")
    projects_registry: str = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "projects", "registry.md"
    )
    cursor_wrapper_dir: str = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "cursor_wrapper"
    )


config = Config()
