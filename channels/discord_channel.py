"""channels/discord_channel.py -- the Discord transport.

Bridges discord.py messages to ``ArchAgent``. The bot's existing
``cogs/chat`` cog still owns the heavy chat path (streaming, the tool
loop, history rolling, image/video tools); this channel is the lighter
front door that:

  * builds a ``ChannelContext`` from a ``discord.Message`` /
    ``discord.Interaction``, applying the DM and guild policies before the
    agent sees anything;
  * exposes ``announce`` so the scheduler and heartbeat can push
    unprompted replies into the right channel without going through the
    cog;
  * renders ``ArchResponse`` (with or without a card) as an embed plus
    optional view.

The class wraps a live ``ArchimedesBot`` rather than owning the gateway
itself -- the bot's existing reconnect, watchdog and cog loading stay
intact. The channel is constructed in ``setup_hook`` once the agent and
bot exist, and torn down in ``ArchimedesBot.close``.
"""
from __future__ import annotations

import logging
from typing import Any

import discord

from arch.core import ArchAgent, ChannelContext
from arch.dynamic_ui import ArchResponse, Card
from channels.base import Channel, ChannelDispatchError
from channels.policy import (
    PolicyDecision, evaluate_dm, evaluate_guild,
)
from channels.renderers import card_to_embed, card_to_view
from channels.session import (
    channel_session_key, dm_session_key, thread_session_key,
)
from config import Config

log = logging.getLogger(__name__)


class DiscordChannel(Channel):
    """Glue between an ``ArchimedesBot`` and an ``ArchAgent``."""

    name = "discord"

    def __init__(self, agent: ArchAgent, bot) -> None:
        super().__init__(agent)
        self.bot = bot
        # The bot's own user id is not stable until ``on_ready``; we cache
        # it lazily so the channel survives a reconnect without rewiring.
        self._dm_policy = agent.config.dm_policy
        self._guild_policy = agent.config.guild_policy

    async def start(self) -> None:
        # The gateway loop is owned by ``ArchimedesBot``; nothing to do here.
        # We do, however, register the agent's announcer so scheduled
        # tasks and the heartbeat have a Discord-shaped sink.
        self.agent.register_announcer(self._announce)

    async def stop(self) -> None:
        return None

    # ── inbound message routing ─────────────────────────────────────────────
    def context_for(self, message: discord.Message) -> ChannelContext:
        """Build a ``ChannelContext`` from a Discord message."""
        guild = message.guild
        channel = message.channel
        is_thread = isinstance(channel, discord.Thread)
        is_dm = guild is None
        if is_dm:
            session = dm_session_key(self.name, message.author.id)
        elif is_thread:
            session = thread_session_key(self.name, channel.id)
        else:
            session = channel_session_key(self.name, channel.id)
        return ChannelContext(
            session_key=session,
            transport=self.name,
            user_id=int(message.author.id),
            guild_id=int(guild.id) if guild else 0,
            channel_id=int(channel.id),
            display_name=getattr(message.author, "display_name", ""),
            is_dm=is_dm,
            is_thread=is_thread,
            metadata={
                "message_id": int(message.id),
                "parent_channel_id": int(getattr(channel, "parent_id", 0) or 0),
            },
        )

    def check_policy(self, ctx: ChannelContext) -> PolicyDecision:
        """Apply the configured DM/guild policies. The channel calls this
        before ``dispatch`` so a denied message never reaches the agent."""
        if ctx.is_dm:
            return evaluate_dm(
                self._dm_policy,
                user_id=ctx.user_id,
                owner_id=Config.OWNER_ID,
            )
        return evaluate_guild(self._guild_policy, guild_id=ctx.guild_id)

    async def dispatch(
        self, message_text: str, ctx: ChannelContext,
    ) -> ArchResponse:
        decision = self.check_policy(ctx)
        if decision is PolicyDecision.DENY:
            raise ChannelDispatchError("policy denied this conversation")
        return await super().dispatch(message_text, ctx)

    # ── outbound rendering ──────────────────────────────────────────────────
    async def render_to(
        self,
        target: discord.abc.Messageable,
        response: ArchResponse,
    ) -> discord.Message | None:
        """Render an ``ArchResponse`` into a Discord message.

        Falls back to plain text when the response has no card, and
        attaches a view when the card declares interactive elements.
        """
        if response.card is not None:
            embed = card_to_embed(response.card)
            view = card_to_view(response.card)
            content = response.text or None
            return await target.send(content=content, embed=embed, view=view)
        text = response.text or "..."
        return await target.send(text[:1990])

    async def _announce(
        self, ctx: ChannelContext, response: ArchResponse,
    ) -> None:
        """Deliver a scheduled or heartbeat reply to its channel.

        The agent does not know about Discord types; this hook converts
        the ``ChannelContext`` channel_id into a live ``discord.abc.Messageable``.
        A missing channel (the bot lost access since the task was scheduled)
        is logged and dropped rather than raised -- a missed reminder is a
        better failure mode than crashing the heartbeat loop.
        """
        cid = ctx.channel_id
        if not cid:
            log.debug(
                "Announce skipped: no channel_id for session %s",
                ctx.session_key,
            )
            return
        target = self.bot.get_channel(cid) or self.bot.get_partial_messageable(cid)
        if target is None:
            log.info("Announce: channel %s no longer reachable.", cid)
            return
        try:
            await self.render_to(target, response)
        except discord.HTTPException as exc:
            log.warning("Announce render failed in %s: %s", cid, exc)
