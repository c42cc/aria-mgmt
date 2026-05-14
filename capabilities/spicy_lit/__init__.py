"""SpicyLit: voice-first adult story experience powered by Grok Voice Agent API."""

from .grok_voice import GrokVoiceSession
from .pipeline import generate_outline
from .db import init_table, save_outline, get_latest_outline
from .prompts import STORY, JOI, VALID_MODES

__all__ = [
    "GrokVoiceSession",
    "generate_outline",
    "init_table",
    "save_outline",
    "get_latest_outline",
    "STORY",
    "JOI",
    "VALID_MODES",
]
