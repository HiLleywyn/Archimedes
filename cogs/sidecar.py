"""cogs/sidecar.py -- passive learning, channel context, and the refresh loop.

Three background jobs:

  * Passive listener -- in channels opted in with ``.ai memory listen on``,
    ambient messages are logged to ``channel_context`` (so the AI knows what
    a channel has been talking about) and as episodes for recall.
  * Ambient chime-ins -- when enabled, Archimedes occasionally adds a one-liner
    to ongoing chatter in a passive channel.
  * Refresh loop -- every few hours, stale per-user memories are
    re-summarised and old trait events are pruned.
"""
from __future__ import annotations

import asyncio
import logging
import random

import discord
from discord.ext import commands, tasks

from config import Config
from ai.client import complete_default
from ai.memory import REFRESH_AFTER_HOURS, batch_refresh_guild, guild_scope
from ai.safety import sanitize_context_snippet

log = logging.getLogger(__name__)

# Keep at most this many recent context rows per channel.
_CHANNEL_CONTEXT_CAP = 40
# Chance (per qualifying message) that Archimedes chimes in when ambient is on.
_AMBIENT_CHANCE = 0.04


class Sidecar(commands.Cog):
    """Passive learning, channel context capture, and memory refresh."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._passive_cache: dict[tuple[int, int], bool] = {}
        self._refresh_loop.start()

    def cog_unload(self) -> None:
        self._refresh_loop.cancel()

    async def _is_passive(self, guild_id: int, channel_id: int) -> bool:
        """Is this channel opted in to passive learning? Cached per channel."""
        key = (guild_id, channel_id)
        if key in self._passive_cache:
            return self._passive_cache[key]
        row = await self.bot.db.fetch_val(
            "SELECT 1 FROM archimedes_passive_channels WHERE guild_id=$1 AND channel_id=$2",
            guild_id, channel_id,
        )
        self._passive_cache[key] = bool(row)
        return bool(row)

    def invalidate_passive_cache(self) -> None:
        self._passive_cache.clear()

    # ── passive listener ──────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        if message.content.startswith(Config.PREFIX):
            return
        bot_user = self.bot.user
        if bot_user and bot_user.mentioned_in(message):
            return  # mentions are handled by the chat cog
        if message.reference and self.bot.is_ai_message(message.reference.message_id):
            return  # replies to Archimedes are handled by the chat cog

        if not Config.PASSIVE_LEARNING:
            return
        if not await self._is_passive(message.guild.id, message.channel.id):
            return

        content = (message.content or "").strip()
        if not content:
            return
        summary = (
            f"{message.author.display_name}: "
            f"{sanitize_context_snippet(content, 200)}"
        )
        try:
            await self.bot.db.execute(
                "INSERT INTO channel_context (guild_id, channel_id, kind, summary) "
                "VALUES ($1,$2,'message',$3)",
                message.guild.id, message.channel.id, summary,
            )
            await self.bot.db.execute(
                "DELETE FROM channel_context WHERE guild_id=$1 AND channel_id=$2 "
                "AND id NOT IN (SELECT id FROM channel_context "
                "WHERE guild_id=$1 AND channel_id=$2 ORDER BY id DESC LIMIT $3)",
                message.guild.id, message.channel.id, _CHANNEL_CONTEXT_CAP,
            )
            if self.bot.memory is not None:
                await self.bot.memory.record_episode(
                    guild_scope(message.guild.id), summary,
                    tags=["passive", f"channel:{message.channel.id}"],
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("passive capture failed: %s", exc)

        # Optional ambient chime-in.
        if not Config.AMBIENT_REPLIES:
            return
        flags = await self.bot.db.get_ai_flags(message.guild.id)
        if not flags.get("ambient"):
            return
        if random.random() > _AMBIENT_CHANCE:
            return
        chat = self.bot.get_cog("ChatBrain")
        if chat is not None:
            try:
                await chat.maybe_ambient(message)
            except Exception as exc:  # noqa: BLE001
                log.debug("ambient reply failed: %s", exc)

    # ── refresh loop ──────────────────────────────────────────────────────────
    @tasks.loop(hours=REFRESH_AFTER_HOURS)
    async def _refresh_loop(self) -> None:
        """Refresh stale memories and prune old trait events across all guilds."""
        total = 0
        for guild in list(self.bot.guilds):
            try:
                total += await batch_refresh_guild(self.bot.db, guild.id, complete_default)
            except Exception as exc:  # noqa: BLE001
                log.debug("refresh failed for guild %s: %s", guild.id, exc)
            try:
                await self.bot.db.execute(
                    "DELETE FROM ai_user_events WHERE guild_id=$1 "
                    "AND created_at < NOW() - INTERVAL '7 days'",
                    guild.id,
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("event prune failed for guild %s: %s", guild.id, exc)
        if total:
            log.info("Memory refresh loop: refreshed %d user memories", total)

    @_refresh_loop.before_loop
    async def _before_refresh(self) -> None:
        await self.bot.wait_until_ready()
        # Stagger the first run so a cold boot is not dominated by model calls.
        await asyncio.sleep(random.uniform(600, 1200))


async def setup(bot) -> None:
    await bot.add_cog(Sidecar(bot))
