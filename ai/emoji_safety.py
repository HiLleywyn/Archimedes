"""ai/emoji_safety.py -- repair custom Discord emoji markup in AI output.

The model frequently writes a half-finished custom emoji (``<:name:1234``
with no closing ``>``) when it runs low on tokens, or hallucinates an emoji
id that does not exist on the server. Both render as literal garbage text
in Discord, so this module drops them before the reply is sent.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import discord

# A complete custom emoji: <:name:id> or <a:name:id>.
_COMPLETE_RE = re.compile(r"<(a?):([A-Za-z0-9_]{1,32}):(\d{15,22})>")
# An unclosed custom emoji run -- opening markup with no closing '>'.
_UNCLOSED_RE = re.compile(r"<a?:[A-Za-z0-9_]{1,32}:\d{0,22}(?![\d>])")


def repair_custom_emojis(text: str, guild: "discord.Guild | None" = None) -> str:
    """Strip broken / hallucinated custom emoji markup from ``text``.

    Unclosed markup is always removed. Closed markup is kept only when its
    snowflake id matches an emoji that actually exists on ``guild``; when no
    guild is supplied, closed markup is left untouched.
    """
    if not text:
        return text

    text = _UNCLOSED_RE.sub("", text)

    if guild is None:
        return text

    valid_ids = {int(e.id) for e in getattr(guild, "emojis", []) or []}

    def _keep(match: re.Match) -> str:
        return match.group(0) if int(match.group(3)) in valid_ids else ""

    return _COMPLETE_RE.sub(_keep, text)
