"""UCS Intelligence Layer — Model Router, Injection Engine, Intelligence Loop.

Active only when UCS_ENABLED=true. When the flag is off, this module is never
imported on the hot path. When on, models.yaml is authoritative for model
selection (the .env fields govern the legacy path).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import yaml

from .config import config
from .db import log_loop_execution
from .prompts import load_template

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class ModelSpec:
    name: str
    provider: str
    model_id: str
    api_key_env: str
    capabilities: list[str] = field(default_factory=list)
    cost_per_m_input: float = 0.0
    cost_per_m_output: float = 0.0
    context_window: int = 200_000


@dataclass
class LoopProfile:
    max_iterations: int = 30
    verification: bool = False


@dataclass
class LoopResult:
    text: str
    status: str
    iterations: int
    total_tokens_in: int
    total_tokens_out: int
    total_cost: float
    latency_ms: int
    model_id: str = ""
    context_truncated: bool = False
    turns_dropped: int = 0


# ---------------------------------------------------------------------------
# Model Router
# ---------------------------------------------------------------------------

class ModelRouter:
    """Config-driven model selection from models.yaml."""

    def __init__(self, config_path: str | None = None):
        path = config_path or config.models_config
        with open(path) as f:
            raw = yaml.safe_load(f)

        self._models: dict[str, ModelSpec] = {}
        for name, spec in raw.get("models", {}).items():
            self._models[name] = ModelSpec(
                name=name,
                provider=spec.get("provider", ""),
                model_id=spec.get("model_id", ""),
                api_key_env=spec.get("api_key_env", ""),
                capabilities=spec.get("capabilities", []),
                cost_per_m_input=spec.get("cost_per_m_input", 0.0),
                cost_per_m_output=spec.get("cost_per_m_output", 0.0),
                context_window=spec.get("context_window", 200_000),
            )

        self._defaults: dict[str, str] = raw.get("defaults", {})
        self._fallbacks: dict[str, list[str]] = raw.get("fallback_chains", {})
        self._profiles: dict[str, LoopProfile] = {}
        for pname, pspec in raw.get("loop_profiles", {}).items():
            self._profiles[pname] = LoopProfile(
                max_iterations=pspec.get("max_iterations", 30),
                verification=pspec.get("verification", False),
            )

        self._clients: dict[str, Any] = {}

    def get(self, name: str) -> ModelSpec:
        if name not in self._models:
            raise KeyError(f"Unknown model: {name}. Available: {list(self._models)}")
        return self._models[name]

    def default_for(self, role: str) -> ModelSpec:
        name = self._defaults.get(role)
        if not name or name not in self._models:
            raise KeyError(f"No default model for role '{role}'. Defaults: {self._defaults}")
        return self._models[name]

    def profile(self, name: str) -> LoopProfile:
        if name not in self._profiles:
            raise KeyError(f"Unknown loop profile: {name}. Available: {list(self._profiles)}")
        return self._profiles[name]

    def fallback_for(self, name: str) -> ModelSpec | None:
        chain = self._fallbacks.get(name, [])
        for candidate in chain:
            if candidate in self._models:
                return self._models[candidate]
        return None

    async def call(
        self,
        model_name: str,
        messages: list[dict],
        system: str = "",
        tools: list[dict] | None = None,
        max_tokens: int = 8192,
    ) -> dict[str, Any]:
        """Unified model call. Dispatches to the right provider SDK.

        No silent fallback — if the provider errors, the exception propagates.
        Fallback decisions belong to the caller who knows whether the
        conversation shape is provider-agnostic.
        """
        spec = self.get(model_name)
        return await self._call_provider(spec, messages, system, tools, max_tokens)

    async def _call_provider(
        self,
        spec: ModelSpec,
        messages: list[dict],
        system: str,
        tools: list[dict] | None,
        max_tokens: int,
    ) -> dict[str, Any]:
        if spec.provider == "anthropic":
            return await self._call_anthropic(spec, messages, system, tools, max_tokens)
        elif spec.provider == "google":
            return await self._call_google(spec, messages, system, max_tokens)
        else:
            raise ValueError(f"Unsupported provider: {spec.provider}")

    async def _call_anthropic(
        self, spec: ModelSpec, messages: list, system: str,
        tools: list | None, max_tokens: int,
    ) -> dict[str, Any]:
        import anthropic
        if "anthropic" not in self._clients:
            api_key = os.getenv(spec.api_key_env, "")
            self._clients["anthropic"] = anthropic.Anthropic(api_key=api_key)
        client = self._clients["anthropic"]

        kwargs: dict[str, Any] = {
            "model": spec.model_id,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        response = await asyncio.to_thread(client.messages.create, **kwargs)

        content_blocks = []
        for block in response.content:
            if hasattr(block, "text") and block.text:
                content_blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                content_blocks.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": dict(block.input) if block.input else {},
                })

        return {
            "content": content_blocks,
            "raw_content": response.content,
            "stop_reason": response.stop_reason,
            "usage": {
                "input_tokens": response.usage.input_tokens if response.usage else 0,
                "output_tokens": response.usage.output_tokens if response.usage else 0,
            },
            "model_id": spec.model_id,
            "model_name": spec.name,
            "cost": (
                (response.usage.input_tokens / 1_000_000 * spec.cost_per_m_input
                 + response.usage.output_tokens / 1_000_000 * spec.cost_per_m_output)
                if response.usage else 0.0
            ),
        }

    async def _call_google(
        self, spec: ModelSpec, messages: list, system: str, max_tokens: int,
    ) -> dict[str, Any]:
        from google import genai
        if "google" not in self._clients:
            api_key = os.getenv(spec.api_key_env, "")
            self._clients["google"] = genai.Client(api_key=api_key)
        client = self._clients["google"]

        prompt_parts = []
        if system:
            prompt_parts.append(f"System: {system}\n\n")
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, str):
                prompt_parts.append(f"{role}: {content}")

        response = await asyncio.to_thread(
            client.models.generate_content,
            model=spec.model_id,
            contents="\n".join(prompt_parts),
        )

        text = response.text if hasattr(response, "text") else str(response)
        usage_meta = getattr(response, "usage_metadata", None)
        input_tokens = getattr(usage_meta, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage_meta, "candidates_token_count", 0) or 0

        return {
            "content": [{"type": "text", "text": text}],
            "raw_content": None,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "model_id": spec.model_id,
            "model_name": spec.name,
            "cost": (
                input_tokens / 1_000_000 * spec.cost_per_m_input
                + output_tokens / 1_000_000 * spec.cost_per_m_output
            ),
        }


# ---------------------------------------------------------------------------
# Injection Engine
# ---------------------------------------------------------------------------

class InjectionEngine:
    """Assembles payloads and manages context budget."""

    def __init__(self, router: ModelRouter):
        self._router = router

    def assemble_planning(
        self,
        system_prompt: str,
        task: str,
        history: list[dict[str, str]] | None = None,
        memory_context: str = "",
        model_name: str = "claude-opus",
    ) -> tuple[str, list[dict], bool, int]:
        """Build (system, messages, truncated, turns_dropped) for a planning call.

        Drops older history turns from the front to stay within the model's
        estimated context budget.  The task message is always preserved.
        """
        spec = self._router.get(model_name)
        # ~3 chars/token is a conservative English estimate; see B2 in the audit
        budget_chars_estimate = spec.context_window * 3

        messages: list[dict] = []
        if history:
            messages.extend(history)

        user_content = memory_context + task if memory_context else task
        messages.append({"role": "user", "content": user_content})

        total_chars = len(system_prompt) + sum(
            len(str(m.get("content", ""))) for m in messages
        )

        truncated = False
        turns_dropped = 0

        while total_chars > budget_chars_estimate and len(messages) > 1:
            dropped = messages.pop(0)
            total_chars -= len(str(dropped.get("content", "")))
            turns_dropped += 1
            truncated = True

        if truncated:
            log.warning(
                "Context budget exceeded for %s — dropped %d older turns",
                model_name, turns_dropped,
            )

        return system_prompt, messages, truncated, turns_dropped

    def trim_agent_messages(
        self,
        system_prompt: str,
        messages: list[dict],
        model_name: str = "claude-opus",
    ) -> tuple[bool, int]:
        """Trim the front of an agent conversation in-place.

        Drops complete turn-pairs (assistant + following user) from index 1
        onward, never splitting a tool_use / tool_result boundary.  messages[0]
        (the original task) is always preserved.

        Returns (truncated, turns_dropped).
        """
        spec = self._router.get(model_name)
        budget_chars_estimate = spec.context_window * 3

        total_chars = len(system_prompt) + sum(
            len(str(m.get("content", ""))) for m in messages
        )

        truncated = False
        turns_dropped = 0

        while total_chars > budget_chars_estimate and len(messages) > 2:
            if len(messages) > 2 and messages[1]["role"] == "assistant":
                pair = [messages.pop(1), messages.pop(1)]
            else:
                break
            for m in pair:
                total_chars -= len(str(m.get("content", "")))
            turns_dropped += 2
            truncated = True

        if truncated:
            log.warning(
                "Agent context budget exceeded for %s — dropped %d messages",
                model_name, turns_dropped,
            )

        return truncated, turns_dropped


# ---------------------------------------------------------------------------
# Intelligence Loop
# ---------------------------------------------------------------------------

class IntelligenceLoop:
    """Bounded execution cycles with model hot-swap."""

    def __init__(self, router: ModelRouter, injector: InjectionEngine):
        self._router = router
        self._injector = injector

    async def execute_planning(
        self,
        context: str,
        session_key: str,
        prompt_template: str = "planning",
        memories: list[dict] | None = None,
        history: list[dict[str, str]] | None = None,
        post_callback: Any = None,
        cancel_check: Any = None,
    ) -> LoopResult:
        """Planning call: single reasoning shot."""
        model_name = self._router._defaults.get("reasoning", "claude-opus")
        template = load_template(prompt_template)
        started_at = time.monotonic()

        memory_ctx = ""
        if memories:
            memory_ctx = "Relevant memories:\n" + "\n".join(
                f"- {m.get('memory', m.get('text', ''))}" for m in memories
            ) + "\n\n"

        system, messages, truncated, turns_dropped = self._injector.assemble_planning(
            system_prompt=template,
            task=context,
            history=history,
            memory_context=memory_ctx,
            model_name=model_name,
        )

        if cancel_check and cancel_check():
            return LoopResult(
                text="Task cancelled by user.", status="cancelled",
                iterations=0, total_tokens_in=0, total_tokens_out=0,
                total_cost=0.0, latency_ms=0,
                context_truncated=truncated, turns_dropped=turns_dropped,
            )

        response = await self._router.call(
            model_name, messages, system=system, max_tokens=8192,
        )

        text_parts = [b["text"] for b in response["content"] if b.get("type") == "text"]
        result_text = "\n".join(text_parts)

        latency = int((time.monotonic() - started_at) * 1000)

        if post_callback:
            asyncio.create_task(post_callback(result_text))

        return LoopResult(
            text=result_text,
            status="completed",
            iterations=1,
            total_tokens_in=response["usage"]["input_tokens"],
            total_tokens_out=response["usage"]["output_tokens"],
            total_cost=response["cost"],
            latency_ms=latency,
            model_id=response["model_id"],
            context_truncated=truncated,
            turns_dropped=turns_dropped,
        )

    async def execute_agent(
        self,
        task: str,
        session_key: str = "",
        memories: list[dict] | None = None,
        mcp_client: Any = None,
        post_callback: Any = None,
        alert_callback: Any = None,
        cancel_check: Any = None,
    ) -> LoopResult:
        """Agent loop: bounded iterations with MCP tool access.

        Supports mid-step suspension for risk-tier confirmation — the MCP
        client's confirm callback is threaded through identically to the
        legacy _do_with_claude path.
        """
        profile = self._router.profile("agent")
        model_name = self._router._defaults.get("reasoning", "claude-opus")
        system_prompt = load_template("do_with_claude_system")
        started_at = time.monotonic()

        spec = self._router.get(model_name)
        if mcp_client is None:
            return LoopResult(
                text="MCP client not available", status="error",
                iterations=0, total_tokens_in=0, total_tokens_out=0,
                total_cost=0.0, latency_ms=0, model_id=spec.model_id,
            )

        tools = mcp_client.list_tools_anthropic()

        memory_ctx = ""
        if memories:
            memory_ctx = "Relevant memories:\n" + "\n".join(
                f"- {m.get('memory', m.get('text', ''))}" for m in memories
            ) + "\n\n"

        messages: list[dict[str, Any]] = [{"role": "user", "content": memory_ctx + task}]

        total_in = 0
        total_out = 0
        total_cost = 0.0
        max_tokens_budget = 50000
        iteration = 0
        truncated = False
        turns_dropped = 0

        while iteration < profile.max_iterations:
            if cancel_check and cancel_check():
                return LoopResult(
                    text="Task cancelled by user.", status="cancelled",
                    iterations=iteration, total_tokens_in=total_in,
                    total_tokens_out=total_out, total_cost=total_cost,
                    latency_ms=int((time.monotonic() - started_at) * 1000),
                    model_id=spec.model_id,
                    context_truncated=truncated, turns_dropped=turns_dropped,
                )

            iteration += 1

            trunc, dropped = self._injector.trim_agent_messages(
                system_prompt, messages, model_name=model_name,
            )
            if trunc:
                truncated = True
                turns_dropped += dropped

            response = await self._router.call(
                model_name, messages, system=system_prompt,
                tools=tools, max_tokens=4096,
            )

            total_in += response["usage"]["input_tokens"]
            total_out += response["usage"]["output_tokens"]
            total_cost += response["cost"]

            has_tool_use = any(
                b.get("type") == "tool_use" for b in response["content"]
            )

            if response["stop_reason"] == "end_turn" or not has_tool_use:
                text_parts = [b["text"] for b in response["content"] if b.get("type") == "text"]
                result = "\n".join(text_parts)
                if post_callback:
                    asyncio.create_task(post_callback(result))
                return LoopResult(
                    text=result, status="completed", iterations=iteration,
                    total_tokens_in=total_in, total_tokens_out=total_out,
                    total_cost=total_cost,
                    latency_ms=int((time.monotonic() - started_at) * 1000),
                    model_id=response["model_id"],
                    context_truncated=truncated, turns_dropped=turns_dropped,
                )

            messages.append({"role": "assistant", "content": response["raw_content"]})

            tool_results = []
            for block in response["content"]:
                if block.get("type") != "tool_use":
                    continue
                tool_result = await mcp_client.call_tool(
                    block["name"],
                    block.get("input", {}),
                    session_key=session_key,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": str(tool_result)[:4000],
                })

            messages.append({"role": "user", "content": tool_results})

            if total_out > max_tokens_budget:
                if alert_callback:
                    asyncio.create_task(alert_callback(
                        f"do_with_claude token budget exceeded ({total_out} tokens)"
                    ))
                return LoopResult(
                    text=f"Token budget exceeded ({total_out} tokens).",
                    status="token_budget", iterations=iteration,
                    total_tokens_in=total_in, total_tokens_out=total_out,
                    total_cost=total_cost,
                    latency_ms=int((time.monotonic() - started_at) * 1000),
                    model_id=spec.model_id,
                    context_truncated=truncated, turns_dropped=turns_dropped,
                )

        final_text = f"Task reached iteration limit ({profile.max_iterations}). Partial progress made."
        if alert_callback:
            asyncio.create_task(alert_callback(final_text))

        return LoopResult(
            text=final_text, status="iteration_limit", iterations=iteration,
            total_tokens_in=total_in, total_tokens_out=total_out,
            total_cost=total_cost,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            model_id=spec.model_id,
            context_truncated=truncated, turns_dropped=turns_dropped,
        )


# ---------------------------------------------------------------------------
# Singleton access
# ---------------------------------------------------------------------------

_router: ModelRouter | None = None
_loop: IntelligenceLoop | None = None


def get_router() -> ModelRouter:
    global _router
    if _router is None:
        _router = ModelRouter()
    return _router


def get_loop() -> IntelligenceLoop:
    global _loop
    if _loop is None:
        router = get_router()
        injector = InjectionEngine(router)
        _loop = IntelligenceLoop(router, injector)
    return _loop
