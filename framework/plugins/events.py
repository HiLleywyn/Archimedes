"""framework/plugins/events.py -- the plugin event system.

Plugins reach two kinds of event:

* **Gateway events** -- Discord activity (a message, a reaction, a member
  joining or leaving). :class:`PluginEventDispatcher` is a single cog that
  listens for these and hands each to :meth:`PluginManager.dispatch_event`.
* **Custom events** -- anything a plugin broadcasts with ``arch.emit``.
  :class:`PluginEventBus` is the registry the manager consults to fan one of
  these out to every plugin that subscribed to that name.

Both kinds are declared the same way: a plugin's ``M.events`` table maps an
event name to a handler. A name in :data:`EVENT_NAMES` is a gateway hook;
every other name is a custom event.

discord.py dispatches each gateway event to every registered cog
independently, so this dispatcher coexists with the chat and sidecar cogs'
own ``on_message`` listeners without disturbing them.
"""
from __future__ import annotations

import logging

from discord.ext import commands

log = logging.getLogger(__name__)

# The event names a plugin's M.events table may bind to Discord activity.
# Anything else in M.events is a custom event carried by arch.emit.
EVENT_NAMES = ("message", "reaction_add", "member_join", "member_leave")


def _epoch(value) -> int | None:
    """A datetime rendered as a UTC epoch, or ``None``."""
    if value is None:
        return None
    try:
        return int(value.timestamp())
    except (AttributeError, ValueError, OverflowError):
        return None


class PluginEventBus:
    """A registry of custom (``arch.emit``) event subscriptions."""

    def __init__(self) -> None:
        self._subs: dict[str, list[tuple[str, object]]] = {}

    def subscribe(self, plugin_id: str, name: str, handler) -> None:
        self._subs.setdefault(name, []).append((plugin_id, handler))

    def unsubscribe_plugin(self, plugin_id: str) -> None:
        """Drop every subscription a plugin holds. Called when it unloads."""
        for name in list(self._subs):
            kept = [(pid, h) for pid, h in self._subs[name] if pid != plugin_id]
            if kept:
                self._subs[name] = kept
            else:
                del self._subs[name]

    def subscribers(self, name: str) -> list[tuple[str, object]]:
        """Every ``(plugin_id, handler)`` subscribed to one custom event."""
        return list(self._subs.get(name, ()))


class PluginEventDispatcher(commands.Cog):
    """Fans Discord gateway events out to plugin handlers."""

    def __init__(self, bot, manager) -> None:
        self.bot = bot
        self.manager = manager

    @commands.Cog.listener()
    async def on_message(self, message) -> None:
        # Skip every bot, including this one -- a plugin reacting to bot
        # output is how feedback loops start.
        if message.author.bot:
            return
        guild = message.guild
        await self.manager.dispatch_event("message", {
            "guild_id": str(guild.id) if guild else "0",
            "guild_name": guild.name if guild else "",
            "channel_id": str(message.channel.id),
            "message_id": str(message.id),
            "author_id": str(message.author.id),
            "author_name": message.author.display_name,
            "content": message.content or "",
            "bot": False,
            "is_dm": guild is None,
        })

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload) -> None:
        me = getattr(self.bot, "user", None)
        if me is not None and payload.user_id == me.id:
            return
        member = payload.member
        await self.manager.dispatch_event("reaction_add", {
            "guild_id": str(payload.guild_id) if payload.guild_id else "0",
            "channel_id": str(payload.channel_id),
            "message_id": str(payload.message_id),
            "user_id": str(payload.user_id),
            "emoji": str(payload.emoji),
            "bot": bool(member.bot) if member is not None else False,
        })

    @commands.Cog.listener()
    async def on_member_join(self, member) -> None:
        await self.manager.dispatch_event("member_join", {
            "guild_id": str(member.guild.id),
            "guild_name": member.guild.name,
            "user_id": str(member.id),
            "user_name": member.display_name,
            "bot": bool(member.bot),
            "joined_at": _epoch(member.joined_at),
        })

    @commands.Cog.listener()
    async def on_member_remove(self, member) -> None:
        await self.manager.dispatch_event("member_leave", {
            "guild_id": str(member.guild.id),
            "guild_name": member.guild.name,
            "user_id": str(member.id),
            "user_name": member.display_name,
            "bot": bool(member.bot),
        })
