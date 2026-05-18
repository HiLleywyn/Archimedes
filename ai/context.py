"""ai/context.py -- single source of truth for AI prompt context.

``gather_chat_context`` fans out every DB lookup a chat turn needs and
returns a typed :class:`ChatContext`. ``build_system_prompt`` is the one
composer that turns that snapshot into the final system prompt. Every chat
entry point (mention / reply / ask / ambient) uses both, so there is one
place a new piece of context has to be added.
"""
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass, field

import discord

from ai import traits as trait_engine
from ai.prompts import AMBIENT_HINT, BASE_SYSTEM_INSTRUCTIONS, DEFAULT_CHAT_PROMPT
from ai.safety import sanitize_context_snippet

log = logging.getLogger(__name__)


class ChatMode(enum.Enum):
    ASK = "ask"
    MENTION = "mention"
    REPLY = "reply"
    AMBIENT = "ambient"


@dataclass
class ChatContext:
    """A typed snapshot of everything the model may see for one turn."""

    mode: ChatMode
    user_id: int
    guild_id: int
    display_name: str
    history: list[dict] = field(default_factory=list)
    user_memory: str = ""
    trait_context: str = ""
    facts_block: str = ""
    channel_context: str = ""
    emoji_context: str = ""
    persona_name: str = "Archimedes"
    ai_prompts: dict = field(default_factory=dict)
    extra_blocks: list[str] = field(default_factory=list)


async def _channel_context_block(db, guild_id: int, channel_id: int) -> str:
    """Recent ambient summaries for the current channel."""
    rows = await db.fetch_all(
        "SELECT summary FROM channel_context "
        "WHERE guild_id=$1 AND channel_id=$2 ORDER BY created_at DESC LIMIT 8",
        int(guild_id), int(channel_id),
    )
    if not rows:
        return ""
    lines = [f"- {sanitize_context_snippet(r['summary'], 160)}" for r in rows]
    return "Recent activity in this channel:\n" + "\n".join(reversed(lines))


async def _emoji_context_block(db, guild_id: int) -> str:
    """Indexed meanings of this server's custom emojis."""
    rows = await db.fetch_all(
        "SELECT name, description FROM guild_emoji_meanings "
        "WHERE guild_id=$1 AND description <> '' ORDER BY updated_at DESC LIMIT 25",
        int(guild_id),
    )
    if not rows:
        return ""
    lines = [f"- :{r['name']}: {sanitize_context_snippet(r['description'], 120)}" for r in rows]
    return "This server's custom emojis and what they mean:\n" + "\n".join(lines)


async def gather_chat_context(
    bot,
    *,
    mode: ChatMode,
    user_id: int,
    guild_id: int,
    channel: discord.abc.Messageable | None,
    member: discord.Member | None,
    display_name: str,
    user_message: str,
    history_key: str = "default",
) -> ChatContext:
    """Build a :class:`ChatContext` from live DB state."""
    db = bot.db
    channel_id = getattr(channel, "id", 0) or 0

    settings = await db.get_guild_settings(guild_id)
    history = await db.get_ai_history(user_id, guild_id, history_key, limit=12)
    user_memory = await db.get_ai_user_memory(user_id, guild_id)
    trait_context = await trait_engine.get_trait_context(db, user_id, guild_id)
    channel_block = await _channel_context_block(db, guild_id, channel_id) if channel_id else ""
    emoji_block = await _emoji_context_block(db, guild_id)

    facts_block = ""
    mem = getattr(bot, "memory", None)
    if mem is not None:
        try:
            facts_block = await mem.facts_for_prompt(user_id, guild_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("facts_for_prompt failed: %s", exc)

    ai_prompts = {
        "chat": settings.get("ai_promptchat") or "",
        "commentary": settings.get("ai_promptcommentary") or "",
        "events": settings.get("ai_promptevents") or "",
        "flavor": settings.get("ai_promptflavor") or "",
    }

    return ChatContext(
        mode=mode,
        user_id=user_id,
        guild_id=guild_id,
        display_name=display_name,
        history=history,
        user_memory=user_memory,
        trait_context=trait_context,
        facts_block=facts_block,
        channel_context=channel_block,
        emoji_context=emoji_block,
        persona_name=settings.get("ai_persona_name") or "Archimedes",
        ai_prompts=ai_prompts,
    )


def build_system_prompt(ctx: ChatContext, *, base_prompt: str | None = None) -> str:
    """Compose the final system prompt string from a :class:`ChatContext`."""
    persona = (base_prompt or ctx.ai_prompts.get("chat") or DEFAULT_CHAT_PROMPT).strip()
    if ctx.persona_name and ctx.persona_name != "Archimedes":
        persona = persona.replace("You are Archimedes", f"You are {ctx.persona_name}", 1)

    parts: list[str] = [persona, BASE_SYSTEM_INSTRUCTIONS]

    if ctx.mode is ChatMode.AMBIENT:
        parts.append(AMBIENT_HINT)

    safe_name = sanitize_context_snippet(ctx.display_name, 48) or "this member"
    parts.append(f"You are talking with {safe_name}.")

    if ctx.user_memory:
        parts.append(
            "What you remember about them: "
            + sanitize_context_snippet(ctx.user_memory, 500)
        )
    if ctx.trait_context:
        parts.append(ctx.trait_context)
    if ctx.facts_block:
        parts.append(ctx.facts_block)
    if ctx.channel_context:
        parts.append(ctx.channel_context)
    if ctx.emoji_context:
        parts.append(ctx.emoji_context)
    for block in ctx.extra_blocks:
        if block:
            parts.append(block)

    return "\n\n".join(p for p in parts if p)
