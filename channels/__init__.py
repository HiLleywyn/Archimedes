"""channels -- pluggable transports for the Archimedes agent.

The agent (``arch.core.ArchAgent``) is transport-agnostic. A channel owns
the actual conversation surface -- the Discord gateway, a web socket, a
CLI loop -- and hands inbound messages to the agent through a small,
fixed contract (``Channel.dispatch`` returns an ``ArchResponse``). The
channel then renders that response in its native form: a Discord embed
plus a view, an HTTP body, a printed paragraph.

Two transports ship today:

  * ``DiscordChannel`` (the existing bot, refactored to call into the agent)
  * a CLI-style ``NullChannel`` used only by tests, kept tiny

Future web / voice channels plug in here. The session-key convention
mirrors the OpenClaw "channels" pattern: every routable conversation gets
a stable id (``arch:discord:channel:<id>`` etc.), so a thread, a guild
channel, and a DM stay in their own bubbles without leaking history.
"""
from __future__ import annotations

from channels.base import Channel, ChannelDispatchError, NullChannel
from channels.policy import (
    DMPolicy, GuildPolicy, PolicyDecision, evaluate_dm, evaluate_guild,
)
from channels.session import (
    dm_session_key, channel_session_key, thread_session_key,
)

__all__ = [
    "Channel", "ChannelDispatchError", "NullChannel",
    "DMPolicy", "GuildPolicy", "PolicyDecision",
    "evaluate_dm", "evaluate_guild",
    "dm_session_key", "channel_session_key", "thread_session_key",
]
