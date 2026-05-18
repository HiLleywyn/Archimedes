"""ai/emoji_index.py -- build the custom-emoji meaning index.

A vision pass on each custom emoji image produces a one-line description
of what it is and the vibe it carries. ``ai.context`` surfaces these to
the model so it understands a server's emoji palette. Entries refresh on a
staleness window so meanings stay current.
"""
from __future__ import annotations

import asyncio
import logging

import discord

from ai.client import complete
from ai.models import resolve_model
from ai.safety import sanitize_context_snippet

log = logging.getLogger(__name__)

DEFAULT_MAX_AGE_DAYS = 14
_MAX_FAILURES_BEFORE_GIVEUP = 4
_CONCURRENCY = 3

_VISION_PROMPT = (
    "This is a custom Discord emoji. In one short sentence (max 20 words), "
    "say what it depicts and the mood or reaction it conveys. No preamble."
)


async def _describe_emoji(db, guild_id: int, emoji: discord.Emoji) -> str | None:
    pick = await resolve_model(db, guild_id, "vision")
    messages = [
        {"role": "user", "content": [
            {"type": "text", "text": _VISION_PROMPT},
            {"type": "image_url", "image_url": {"url": str(emoji.url)}},
        ]},
    ]
    text = await complete(messages, model=pick.model, max_tokens=60, timeout=30)
    if not text:
        return None
    return sanitize_context_snippet(text, 200)


async def index_guild(
    db, guild: discord.Guild, *, force: bool = False,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
) -> dict:
    """Index (or refresh) every custom emoji in a guild. Returns stats."""
    emojis = list(getattr(guild, "emojis", []) or [])
    stats = {"total": len(emojis), "indexed": 0, "skipped": 0,
             "failed": 0, "pruned": 0, "vision_down": False}

    stale_ids = set(await db.get_stale_emoji_meaning_ids(guild.id, max_age_days))
    known = {int(r["emoji_id"]) for r in await db.get_all_emoji_meanings(guild.id)}
    live_ids = {int(e.id) for e in emojis}

    # Prune meanings for emojis that no longer exist.
    for gone in known - live_ids:
        await db.execute(
            "DELETE FROM guild_emoji_meanings WHERE guild_id=$1 AND emoji_id=$2",
            guild.id, gone,
        )
        stats["pruned"] += 1

    semaphore = asyncio.Semaphore(_CONCURRENCY)
    failures = 0

    async def _one(emoji: discord.Emoji) -> None:
        nonlocal failures
        eid = int(emoji.id)
        if not force and eid in known and eid not in stale_ids:
            stats["skipped"] += 1
            return
        if failures >= _MAX_FAILURES_BEFORE_GIVEUP:
            stats["vision_down"] = True
            stats["failed"] += 1
            return
        async with semaphore:
            desc = await _describe_emoji(db, guild.id, emoji)
        if not desc:
            failures += 1
            stats["failed"] += 1
            return
        await db.upsert_emoji_meaning(
            guild.id, eid, emoji.name, desc,
            animated=bool(emoji.animated), source="vision",
        )
        stats["indexed"] += 1

    await asyncio.gather(*(_one(e) for e in emojis))
    return stats
