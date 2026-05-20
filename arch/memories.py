"""arch/memories.py -- the Archimedes memory facade.

A thin, intent-named view over ``ai.memory.MemoryService``. The underlying
tables (``archimedes_facts``, ``ai_user_memory``, ``archimedes_episodes``)
and the refresh loop are unchanged; this module exposes the three verbs
the Archimedes assistant actually uses:

  * ``remember(key, value)``   -- store a fact about the active user/scope
  * ``recall(query)``          -- look facts up by substring match
  * ``top_for_prompt(n)``      -- the N most recent facts for the system prompt

A ArchAgent calls ``top_for_prompt`` every turn and prepends the result to
its prompt, so frequently-accessed facts integrate naturally without any
explicit recall step. This mirrors Archimedes's "memories that integrate
into the system prompt" behaviour.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class MemoryEntry:
    """One stored fact, in the shape Archimedes uses externally."""

    key: str
    value: str
    confidence: float
    updated_at: float
    scope: str


class ArchMemory:
    """Intent-named adapter over the existing ``ai.memory.MemoryService``.

    ``service`` is the already-built service that the bot wires up in
    ``framework.bot.ArchimedesBot.setup_hook``. Reusing it means every
    existing trait, episode and refresh path keeps working untouched; Archimedes
    only renames the verbs.
    """

    def __init__(self, service) -> None:
        self.service = service

    # ── Scoping ──────────────────────────────────────────────────────────────
    @staticmethod
    def _scope(user_id: int, guild_id: int) -> str:
        from ai.memory import guild_scope, user_scope  # noqa: WPS433
        return user_scope(user_id, guild_id) if user_id else guild_scope(guild_id)

    # ── Public verbs ─────────────────────────────────────────────────────────
    async def remember(self, key: str, value: str, *,
                       user_id: int = 0, guild_id: int = 0,
                       confidence: float = 0.8, source: str = "arch") -> None:
        """Store one fact. ``user_id``/``guild_id`` pick the scope: a
        non-zero user_id stores a per-user fact, otherwise it lands at the
        guild scope. Calling code in the agent already knows the right ids."""
        scope = self._scope(user_id, guild_id)
        await self.service.upsert_fact(
            scope=scope, key=key, value=value,
            confidence=confidence, source=source,
        )

    async def recall(self, query: str, *, user_id: int = 0, guild_id: int = 0,
                     limit: int = 5) -> list[MemoryEntry]:
        """Substring search over a scope's facts. Postgres full-text would
        be nicer, but the table is small and a Python filter is enough --
        and it keeps this layer free of new SQL."""
        scope = self._scope(user_id, guild_id)
        facts = await self.service.get_facts(scope, limit=200)
        q = (query or "").strip().lower()
        out: list[MemoryEntry] = []
        for f in facts:
            if not q or q in f.key.lower() or q in f.value.lower():
                out.append(MemoryEntry(
                    key=f.key, value=f.value, confidence=f.confidence,
                    updated_at=f.updated_at or 0.0, scope=f.scope,
                ))
            if len(out) >= limit:
                break
        return out

    async def top_for_prompt(self, *, user_id: int = 0, guild_id: int = 0,
                             limit: int = 5) -> list[MemoryEntry]:
        """The N most recent facts the agent should see this turn.

        A turn with no relevant facts returns an empty list; the agent omits
        the memory section from its prompt rather than emitting an empty
        block.
        """
        scope = self._scope(user_id, guild_id)
        facts = await self.service.get_facts(scope, limit=limit)
        return [
            MemoryEntry(
                key=f.key, value=f.value, confidence=f.confidence,
                updated_at=f.updated_at or 0.0, scope=f.scope,
            )
            for f in facts
        ]


def format_for_prompt(entries: list[MemoryEntry]) -> str:
    """Render a small memory block to drop into a system prompt.

    Empty input returns an empty string so the agent can ``if block:`` cheaply
    and skip the block entirely on a cold scope.
    """
    if not entries:
        return ""
    lines = ["Memories about this user (use naturally, do not list back):"]
    for e in entries:
        lines.append(f"- {e.key}: {e.value}")
    return "\n".join(lines)
