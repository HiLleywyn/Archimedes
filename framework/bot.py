"""framework/bot.py -- the Archimedes bot class.

Owns the shared services (database, Redis short-term store, memory sidecar,
tool registry, training logger), loads the cogs, and tracks the ids of its
own AI replies so the chat cog can detect when a user replies to one.
"""
from __future__ import annotations

import collections
import logging

import discord
from discord.ext import commands

from config import Config
from framework.context import ArchimedesContext
from framework.db import Database

log = logging.getLogger(__name__)

# Cogs loaded on startup, in order.
COGS: tuple[str, ...] = (
    "cogs.meta",
    "cogs.chat",
    "cogs.archimedes",
    "cogs.ai_admin",
    "cogs.sidecar",
    "cogs.productivity",
)


class ArchimedesBot(commands.Bot):
    """The standalone AI chat bot."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        super().__init__(
            command_prefix=Config.PREFIX,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True,
            ),
        )
        self.db = Database(Config.DATABASE_URL)
        self.short_term = None  # ai.redis_store.ShortTermStore
        self.memory = None      # ai.memory.MemoryService
        self.tools = None       # ai.tools.ToolRegistry
        self.training = None    # ai.training.TrainingLogger
        # Bounded ring of message ids the bot sent as AI replies. Used to
        # tell "user replied to one of my answers" from "user replied to
        # someone else" without a DB round-trip.
        self._ai_message_ids: collections.deque[int] = collections.deque(maxlen=4000)
        # Thread ids Archimedes spawned for conversations. Any message in one is
        # treated as a continuation, even without an explicit reply.
        self._ai_thread_ids: collections.deque[int] = collections.deque(maxlen=2000)

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def setup_hook(self) -> None:
        from ai.lua_plugins import load_plugins
        from ai.memory import MemoryService
        from ai.redis_store import ShortTermStore
        from ai.tools import build_default_registry
        from ai.training import TrainingLogger

        await self.db.connect()
        self.short_term = ShortTermStore()
        await self.short_term.connect()
        self.memory = MemoryService(self.db, self.short_term)
        self.training = TrainingLogger(self.db)
        self.tools = build_default_registry()
        load_plugins(self.tools)

        for ext in COGS:
            try:
                await self.load_extension(ext)
                log.info("loaded cog: %s", ext)
            except Exception:  # noqa: BLE001
                log.exception("failed to load cog: %s", ext)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s) in %d guild(s)",
                 self.user, getattr(self.user, "id", "?"), len(self.guilds))

    async def get_context(self, message, *, cls=ArchimedesContext):
        return await super().get_context(message, cls=cls)

    async def close(self) -> None:
        from ai.client import close_client

        log.info("Shutting down...")
        try:
            await close_client()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.short_term is not None:
                await self.short_term.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.db.close()
        except Exception:  # noqa: BLE001
            pass
        await super().close()

    # ── AI-reply bookkeeping ──────────────────────────────────────────────────
    def remember_ai_message(self, message_id: int) -> None:
        self._ai_message_ids.append(int(message_id))

    def is_ai_message(self, message_id: int | None) -> bool:
        return message_id is not None and int(message_id) in self._ai_message_ids

    def remember_ai_thread(self, thread_id: int) -> None:
        self._ai_thread_ids.append(int(thread_id))

    def is_ai_thread(self, channel) -> bool:
        return getattr(channel, "id", None) in self._ai_thread_ids

    # ── error handling ────────────────────────────────────────────────────────
    async def on_command_error(self, ctx, error) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.reply_error("This command only works in a server.")
            return
        if isinstance(error, (commands.CheckFailure, commands.MissingPermissions)):
            await ctx.reply_error(str(error) or "You can't use that command.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply_error(f"Missing argument: `{error.param.name}`.")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.reply_error(f"Bad argument: {error}")
            return
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.reply_cooldown(error.retry_after)
            return
        log.exception("command error in %s", getattr(ctx.command, "name", "?"),
                      exc_info=error)
        try:
            await ctx.reply_error("Something broke running that. Try again.")
        except discord.HTTPException:
            pass
