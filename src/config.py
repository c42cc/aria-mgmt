"""Configuration — loaded once from .env into a frozen dataclass.

One home for every knob. Nothing else reads os.environ. Secrets live in .env
(here, a symlink to the main checkout's .env so the worktree never duplicates a
key — review 3.5). Model IDs are pinned to verified-live values; preflight
asserts each one actually resolves before the user can hit it (review 3.2).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

_REPO = Path(__file__).resolve().parent.parent
# Load THIS worktree's .env (a symlink to the real one). Explicit path, no search.
load_dotenv(_REPO / ".env")


@dataclass(frozen=True)
class Config:
    repo: Path = _REPO

    # ── Secrets (repr=False so a key never leaks into a log or traceback) ───
    anthropic_api_key: str = field(default=os.getenv("ANTHROPIC_API_KEY", "").strip(), repr=False)
    gemini_api_key: str = field(
        default=(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip(), repr=False
    )

    # ── Models (verified live 2026-06-21 — see docs/aria-v2/preflight.md) ───
    # claude-opus-4-8 is current ($5/$25 per MTok, adaptive-thinking-only).
    # The conductor reasons here; this is the one place Opus is spent.
    reasoning_model: str = os.getenv("ARIA_REASONING_MODEL", "claude-opus-4-8")
    # The cheap/fast tier for routine extraction (NOT used in Phase 0; verify
    # the id before wiring it — never pin an unverified model).
    fast_model: str = os.getenv("ARIA_FAST_MODEL", "claude-haiku-4-5")
    # The voice layer (Phase 1). gemini-3.1-flash-live-preview is current.
    voice_model: str = os.getenv("ARIA_VOICE_MODEL", "gemini-3.1-flash-live-preview")

    # ── Engine (Claude Code via the Agent SDK — a managed subprocess) ───────
    # Billing is an explicit, deliberate decision (review 3.1). Today headless
    # Agent SDK still draws from the subscription (the June-15 credit split was
    # paused), so 'subscription' is the default and cheapest; 'api' pays PAYG.
    claude_code_billing: str = os.getenv("ARIA_CLAUDE_CODE_BILLING", "subscription").strip()
    claude_code_max_budget_usd: float = float(os.getenv("ARIA_CC_MAX_BUDGET_USD", "5"))
    claude_code_timeout_sec: float = float(os.getenv("ARIA_CC_TIMEOUT_SEC", "1200"))
    # The go-gate already captured the human approval, so the post-go build runs
    # autonomously within the named workspace. 'bypassPermissions' lets it edit +
    # run tests without an interactive prompt that nothing would answer headless.
    # This is the co-located Phase-0 scope; Phase 3 adds per-endpoint scoping.
    claude_code_permission_mode: str = os.getenv("ARIA_CC_PERMISSION_MODE", "bypassPermissions").strip()

    # ── Cost / time guardrails ─────────────────────────────────────────────
    daily_spend_cap_usd: float = float(os.getenv("DAILY_SPEND_CAP_USD", "20"))
    anthropic_timeout_sec: float = float(os.getenv("ANTHROPIC_TIMEOUT_SEC", "120"))
    conductor_max_tokens: int = int(os.getenv("ARIA_CONDUCTOR_MAX_TOKENS", "1500"))
    # Tiering (review 2.2): routine/interview turns use the fast model; the
    # nuanced post-build REPORT stays on Opus. ~1.8x faster turns, routing + the
    # guards verified to hold on the fast model. Set false to force all-Opus.
    conductor_tier_routine: bool = os.getenv("ARIA_CONDUCTOR_TIER", "true").lower() == "true"

    # ── Paths ──────────────────────────────────────────────────────────────
    prompts_dir: Path = _REPO / "prompts"
    loops_dir: Path = _REPO / "loops"
    data_dir: Path = _REPO / "data"
    projects_registry: Path = _REPO / "projects" / "registry.md"

    @property
    def outcome_log_path(self) -> Path:
        return self.data_dir / "outcomes.jsonl"

    @property
    def trace_dir(self) -> Path:
        return self.data_dir / "traces"

    @property
    def memory_path(self) -> Path:
        return self.data_dir / "memory.json"


config = Config()
