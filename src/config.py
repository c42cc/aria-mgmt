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

    # ── The house (Phase 4) — Home Assistant endpoint ──────────────────────
    # Aria controls the physical house through ONE substrate: Home Assistant.
    # Empty = the house endpoint reports "not configured" (honest, never a silent
    # fallback). Set both to point at your HA instance (local-first, over
    # Tailscale). The token is a long-lived HA access token. The conductor turns
    # speech into (device, action); the endpoint actuates HA's REST API and
    # verifies against GROUND TRUTH (re-reads the entity state) — the LLM is never
    # on the actuation hot path.
    hass_url: str = os.getenv("HASS_URL", "").strip().rstrip("/")
    hass_token: str = field(default=os.getenv("HASS_TOKEN", "").strip(), repr=False)
    hass_timeout_sec: float = float(os.getenv("HASS_TIMEOUT_SEC", "15"))

    # ── Spark endpoint — a local open-source model as an executor ───────────
    # The DGX Spark returns "as an endpoint, never as core" (ABSENCES.md): a loop
    # with endpoint `spark` runs on a model served locally on the Spark (vLLM
    # serves the Anthropic Messages API natively, so the anthropic SDK with this
    # base_url drives it unchanged). Empty = the endpoint reports "not configured".
    spark_base_url: str = os.getenv("SPARK_BASE_URL", "").strip().rstrip("/")
    spark_model: str = os.getenv("SPARK_MODEL", "local-brain").strip()
    spark_max_tokens: int = int(os.getenv("SPARK_MAX_TOKENS", "4096"))

    # ── Cost / time guardrails ─────────────────────────────────────────────
    daily_spend_cap_usd: float = float(os.getenv("DAILY_SPEND_CAP_USD", "20"))
    anthropic_timeout_sec: float = float(os.getenv("ANTHROPIC_TIMEOUT_SEC", "120"))
    conductor_max_tokens: int = int(os.getenv("ARIA_CONDUCTOR_MAX_TOKENS", "1500"))
    # Conversation memory (the durable transcript fed to the conductor each turn).
    # How many recent turns of the current thread to load as context (the model's
    # long context + caching carry it; compaction is the future lever if a single
    # thread ever outgrows this). And the default thread name.
    context_window_turns: int = int(os.getenv("ARIA_CONTEXT_TURNS", "200"))
    default_thread: str = os.getenv("ARIA_THREAD", "main").strip() or "main"
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

    @property
    def conversation_db_path(self) -> Path:
        return self.data_dir / "aria.db"


config = Config()
