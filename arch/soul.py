"""arch/soul.py -- the editable system-prompt layer.

Archimedes ships with named soul presets. An operator picks one with
``/soul preset tutor`` or writes a free-form prompt with
``/soul set "you are a deadpan Rust tutor"``. The active soul is stored in
``archimedes_soul`` (database) so it survives restarts and can be edited at
runtime without touching the environment.

The soul layer composes with the framework's non-negotiable safety rules
(``ai.prompts.BASE_SYSTEM_INSTRUCTIONS``) and any per-guild persona
overrides already stored in ``guild_settings.ai_promptchat``; Archimedes's soul
is the outer layer that wraps both.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Hard cap on a soul prompt. The Archimedes reference app uses 4000; we match
# it so an exported soul drops in cleanly.
SOUL_MAX_CHARS = 4000


# ── Built-in presets ──────────────────────────────────────────────────────────
DEFAULT_SOUL = (
    "You are not a chatbot. You are a personal assistant who grows with your "
    "user. Be genuinely helpful: skip 'Great question!' and 'I'd be happy "
    "to help!' filler, and just help. Actions speak louder than filler words. "
    "Remember the people you talk to and bring up what you know about them "
    "naturally when it fits. Match the energy of the room: relaxed for casual "
    "chat, precise for technical work."
)

SOUL_PRESETS: dict[str, str] = {
    "default": DEFAULT_SOUL,
    "short": (
        "You are a personal assistant. Reply in one or two short sentences. "
        "No preamble, no apologies, no follow-up offers. Just the answer."
    ),
    "tutor": (
        "You are a patient one-on-one tutor. Break complex topics into small "
        "steps, check for understanding before moving on, and prefer worked "
        "examples to definitions. When a student is wrong, point at the "
        "specific step that went off and ask a question that helps them "
        "self-correct."
    ),
    "creative": (
        "You are a creative writing partner. Match the user's voice, offer "
        "concrete suggestions rather than vague encouragement, and never "
        "rewrite their work without permission. When asked for ideas, hand "
        "back several distinct directions, not one polished pitch."
    ),
    "expert": (
        "You are a senior subject-matter expert. Answer with precision and "
        "without hedging. When you are uncertain say so explicitly. Cite the "
        "specific tool, source, or measurement that supports a non-obvious "
        "claim. No filler, no apologies, no AI-assistant disclaimers."
    ),
}


@dataclass
class SoulRecord:
    prompt: str
    preset_name: str
    updated_at: float


# ── Pure helpers (no DB) ──────────────────────────────────────────────────────
def preset(name: str) -> str:
    """Return the prompt text for a named preset, or the default."""
    return SOUL_PRESETS.get(name.lower().strip(), DEFAULT_SOUL)


def list_presets() -> list[str]:
    """Stable, alphabetical list of preset names for picker UIs."""
    return sorted(SOUL_PRESETS.keys())


def normalise(prompt: str) -> str:
    """Trim a candidate prompt to the hard cap and strip stray whitespace."""
    text = (prompt or "").strip()
    if len(text) > SOUL_MAX_CHARS:
        text = text[:SOUL_MAX_CHARS]
    return text


# ── Persistence ───────────────────────────────────────────────────────────────
class SoulStore:
    """Database-backed soul holder. The active soul is identified by the
    string ``id`` column (a single row keyed ``'default'`` for a single-tenant
    deployment; future per-guild souls would live under a guild id).

    Reads are not cached -- a soul change must be visible to the next turn,
    and the table is touched at most once per turn.
    """

    def __init__(self, db) -> None:  # framework.db.Database (kept loose to dodge a cycle)
        self.db = db

    async def get(self, *, soul_id: str = "default") -> SoulRecord:
        row = await self.db.fetch_one(
            "SELECT prompt, preset_name, EXTRACT(EPOCH FROM updated_at) AS ts "
            "FROM archimedes_soul WHERE id = $1",
            soul_id,
        )
        if row is None:
            return SoulRecord(
                prompt=DEFAULT_SOUL, preset_name="default", updated_at=0.0,
            )
        return SoulRecord(
            prompt=row["prompt"] or DEFAULT_SOUL,
            preset_name=row["preset_name"] or "default",
            updated_at=float(row["ts"] or 0.0),
        )

    async def set(self, prompt: str, *, preset_name: str = "custom",
                  soul_id: str = "default") -> SoulRecord:
        text = normalise(prompt) or DEFAULT_SOUL
        await self.db.execute(
            "INSERT INTO archimedes_soul (id, prompt, preset_name, updated_at) "
            "VALUES ($1, $2, $3, NOW()) "
            "ON CONFLICT (id) DO UPDATE SET prompt = EXCLUDED.prompt, "
            "preset_name = EXCLUDED.preset_name, updated_at = NOW()",
            soul_id, text, preset_name,
        )
        return SoulRecord(prompt=text, preset_name=preset_name,
                          updated_at=time.time())

    async def use_preset(self, name: str, *, soul_id: str = "default") -> SoulRecord:
        name = name.lower().strip()
        if name not in SOUL_PRESETS:
            raise ValueError(f"unknown soul preset: {name!r}")
        return await self.set(SOUL_PRESETS[name], preset_name=name,
                              soul_id=soul_id)

    async def reset(self, *, soul_id: str = "default") -> SoulRecord:
        return await self.use_preset("default", soul_id=soul_id)
