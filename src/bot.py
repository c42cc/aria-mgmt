"""Main entry point. The whole loop."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import discord
from discord.ext import commands

from .config import config
from .cursor_bridge import CursorBridge
from .db import init_db
from .gemini_session import GeminiSession
from .memory import init_memory
from .tools import handle_tool_call, init_tools

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
cursor_bridge = CursorBridge()
gemini = GeminiSession(tool_handler=handle_tool_call)


@bot.event
async def on_ready():
    log.info("Logged in as %s (id=%s)", bot.user, bot.user.id)
    init_db()
    init_memory()
    init_tools(cursor_bridge)
    await cursor_bridge.start()
    await gemini.connect()
    log.info("All systems initialized")


@bot.command()
async def join(ctx: commands.Context):
    """Join the voice channel."""
    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return
    if ctx.author.voice and ctx.author.voice.channel:
        await ctx.author.voice.channel.connect()
        await ctx.send(f"Joined {ctx.author.voice.channel.name}")
    else:
        await ctx.send("You're not in a voice channel.")


@bot.command()
async def leave(ctx: commands.Context):
    """Leave the voice channel."""
    if not _is_authorized(ctx.author.id):
        await ctx.send("Not authorized.")
        return
    if ctx.voice_clients:
        await ctx.voice_clients[0].disconnect()
        await ctx.send("Left voice channel.")


@bot.command()
async def status(ctx: commands.Context):
    """Check active Cursor sessions."""
    result = await handle_tool_call("cursor_status", {})
    await ctx.send(f"```json\n{result}\n```")


def _is_authorized(user_id: int) -> bool:
    if not config.authorized_user_ids:
        return True
    return str(user_id) in config.authorized_user_ids


def main():
    if not config.discord_bot_token:
        log.error("DISCORD_BOT_TOKEN not set. Copy .env.example to .env and fill in your keys.")
        sys.exit(1)
    bot.run(config.discord_bot_token)


if __name__ == "__main__":
    main()
