"""channels/session.py -- session-key derivation.

A session key identifies one conversation bubble: a guild channel, a DM
with a user, a thread under a parent channel. The key is the agent's
routing handle -- two messages with the same key share history; messages
with different keys live in their own contexts.

The convention mirrors OpenClaw's documented pattern so an operator
already familiar with it does not have to learn a new scheme:

  arch:<transport>:dm:<user_id>
  arch:<transport>:channel:<channel_id>
  arch:<transport>:thread:<thread_id>

A thread inherits its parent's behaviour but lives in its own bubble.
The transport segment lets two transports (Discord, web) coexist without
their session keys colliding even when their numeric ids happen to match.
"""
from __future__ import annotations


def dm_session_key(transport: str, user_id: int) -> str:
    return f"arch:{transport}:dm:{int(user_id)}"


def channel_session_key(transport: str, channel_id: int) -> str:
    return f"arch:{transport}:channel:{int(channel_id)}"


def thread_session_key(transport: str, thread_id: int) -> str:
    return f"arch:{transport}:thread:{int(thread_id)}"


def parse_session_key(key: str) -> tuple[str, str, str, str]:
    """Round-trip a session key back into ``(scheme, transport, kind, id)``.

    Returns blanks for an unparseable string rather than raising; the
    callers (logging, debug surfaces) prefer best-effort parsing.
    """
    parts = (key or "").split(":")
    if len(parts) != 4:
        return ("", "", "", "")
    return (parts[0], parts[1], parts[2], parts[3])
