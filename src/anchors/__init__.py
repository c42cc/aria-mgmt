"""Deterministic ground-truth anchor system.

Each anchor re-consults the source of truth a tool touched, emits
structured facts, and produces a binary verdict that *floors* the LLM
judge.  The floor rule: ``final = min(anchor, llm_judge)``.
"""

from .registry import anchor_for

__all__ = ["anchor_for"]
