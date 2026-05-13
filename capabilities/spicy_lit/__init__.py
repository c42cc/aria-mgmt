"""SpicyLit: voice-first adult story experience powered by Grok Voice Agent API."""

from .grok_voice import GrokVoiceSession
from .pipeline import generate_outline
from .db import init_table, save_outline, get_latest_outline

__all__ = [
    "GrokVoiceSession",
    "generate_outline",
    "init_table",
    "save_outline",
    "get_latest_outline",
]
