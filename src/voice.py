"""Voice transport — LiveKit Agents in front of the UNCHANGED brain.

Phase 1: the phone call. LiveKit owns what must be fast and is genuinely hard —
telephony (SIP), audio transport, semantic turn detection, barge-in, VAD. Aria's
brain stays exactly the same: `ConductorLLM` is a LiveKit `llm.LLM` whose every
"completion" is one `AriaBrain` turn (Claude owns the content; the voice layer
only renders it). On a confirmed go, the build runs in the BACKGROUND and reports
via the loop's channel, so a multi-second engine run never freezes the call
(review 2.2 — never dead air, never block the turn).

Run it (after provisioning — see docs/aria-v2/phase1-voice.md):
    python -m src.voice console      # local mic/speakers, no server (talk to her)
    python -m src.voice dev          # connect to LiveKit (room/telephony)

The ConductorLLM bridge is unit-smoke-tested without audio; the audio + phone
loop is verified by a human speaking (an agent cannot), after LiveKit + a number
are provisioned.
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    llm,
)

from .brain import AriaBrain
from .config import config
from .loops import load_loops
from .telemetry import Trace

log = logging.getLogger(__name__)


def _latest_user_text(chat_ctx: llm.ChatContext) -> str:
    for item in reversed(chat_ctx.items):
        if getattr(item, "type", "message") != "message":
            continue
        if getattr(item, "role", None) != "user":
            continue
        texts = [p for p in (item.content or []) if isinstance(p, str)]
        if texts:
            return " ".join(texts).strip()
    return ""


class ConductorLLM(llm.LLM):
    """A LiveKit LLM whose completion is one AriaBrain turn. Claude conducts."""

    def __init__(self, brain: AriaBrain) -> None:
        super().__init__()
        self.brain = brain

    @property
    def model(self) -> str:
        return config.reasoning_model

    @property
    def provider(self) -> str:
        return "aria-conductor"

    def chat(self, *, chat_ctx, tools=None, conn_options=DEFAULT_API_CONNECT_OPTIONS, **_kw):
        return _ConductorStream(self, chat_ctx=chat_ctx, tools=tools or [], conn_options=conn_options)


class _ConductorStream(llm.LLMStream):
    async def _run(self) -> None:
        brain: AriaBrain = self._llm.brain  # type: ignore[attr-defined]
        user_text = _latest_user_text(self._chat_ctx)
        if not user_text:
            return

        turn = await asyncio.to_thread(brain.user_turn, user_text)
        await self._say(turn.speak)

        ready = brain.ready_to_dispatch(turn)
        if turn.phase == "DISPATCH" and ready is None:
            corrective = await asyncio.to_thread(brain.dispatch_violation)
            await self._say(" " + corrective.speak)
        elif ready:
            loop, slots = ready
            # The build is long; never block the call. Run it in the background;
            # the outcome is logged and reported via the loop's channel.
            asyncio.create_task(self._background_build(loop, slots), name="aria_build")

    async def _say(self, text: str) -> None:
        self._event_ch.send_nowait(
            llm.ChatChunk(id=uuid.uuid4().hex, delta=llm.ChoiceDelta(role="assistant", content=text))
        )

    async def _background_build(self, loop, slots) -> None:
        brain: AriaBrain = self._llm.brain  # type: ignore[attr-defined]
        try:
            await asyncio.to_thread(brain.dispatch, loop, slots)
        except Exception:
            log.exception("background build failed")
        # report=text loops land in the outcome log + (Phase 1b) the text channel.
        # report=call_back loops will speak the report back on the call.


# ── LiveKit entrypoint (needs the voice extras + LiveKit creds; user-run) ────

def _build_server():
    from livekit.agents import Agent, AgentServer, AgentSession, JobContext, inference
    from livekit.plugins import silero

    server = AgentServer()

    @server.rtc_session()
    async def entrypoint(ctx: JobContext) -> None:
        brain = AriaBrain(loops=load_loops(), trace=Trace())
        session = AgentSession(
            vad=silero.VAD.load(),
            # LiveKit Inference (needs a LiveKit Cloud key). Swap for plugin
            # STT/TTS with provider keys if you prefer not to use Inference.
            stt=inference.STT("deepgram/nova-3", language="multi"),
            tts=inference.TTS("cartesia/sonic-3"),
            llm=ConductorLLM(brain),
        )
        await session.start(agent=Agent(instructions="Aria's words come from the conductor."), room=ctx.room)
        await session.say("Hey Corbin -- what do you need?")

    return server


def main() -> None:
    from livekit.agents import cli

    cli.run_app(_build_server())


if __name__ == "__main__":
    main()
