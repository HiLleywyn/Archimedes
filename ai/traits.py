"""ai/traits.py -- layered user trait engine with time-decay.

Every interaction emits a weak signal toward one or more traits. Weights
decay exponentially so a member's profile tracks who they are *now*, not
who they were months ago. All decay math runs DB-side via
``EXTRACT(EPOCH FROM (NOW() - last_observed_at))`` so container / DB clock
skew can never corrupt a weight.

  weight     = old_weight * exp(-LAMBDA * age_seconds) + signal_weight
  confidence = 1 - exp(-sample_size / K)

Conflicting traits (blunt vs chatty, upbeat vs down) share signal space:
when one gets a signal its opposite is dampened, so the profile cannot
claim a member is both terse and verbose at once.
"""
from __future__ import annotations

import logging
import math
import re

log = logging.getLogger(__name__)

# Decay constant: ~3-day half-life. lambda = ln(2) / half_life_seconds.
_LAMBDA = math.log(2) / (3 * 24 * 3600)
# Confidence shaping constant.
_K = 5.0

# Traits the engine tracks, with a human label for the prompt block.
TRAITS: dict[str, str] = {
    "curious": "asks a lot of questions",
    "humorous": "jokes around, light-hearted",
    "technical": "talks shop, technical / detail-oriented",
    "supportive": "warm, thankful, encouraging",
    "blunt": "terse, gets to the point",
    "chatty": "writes long, talkative messages",
    "upbeat": "high energy, enthusiastic",
    "down": "venting, frustrated, low energy",
}

# Pairs that should not both be high; a signal to one dampens the other.
_CONFLICTS: dict[str, str] = {
    "blunt": "chatty", "chatty": "blunt",
    "upbeat": "down", "down": "upbeat",
}

# ── Tone detection keyword sets ───────────────────────────────────────────────
_HUMOR_RE = re.compile(r"\b(lol|lmao|lmfao|haha+|rofl|jk|kidding|funny)\b", re.I)
_TECH_RE = re.compile(
    r"\b(code|bug|error|function|api|server|deploy|python|json|database|"
    r"compile|stack ?trace|regex|git)\b", re.I
)
_SUPPORT_RE = re.compile(r"\b(thanks|thank you|ty|appreciate|helpful|great|nice work)\b", re.I)
_DOWN_RE = re.compile(
    r"\b(ugh|tired|exhausted|annoyed|frustrat|hate this|sucks|terrible|"
    r"awful|stressed|sad|depressed)\b", re.I
)


def _detect_tone_signals(content: str) -> list[tuple[str, float]]:
    """Return ``(trait, weight)`` signals inferred from a chat message."""
    text = content or ""
    signals: list[tuple[str, float]] = []
    stripped = text.strip()

    if "?" in text:
        signals.append(("curious", 1.0))
    if _HUMOR_RE.search(text):
        signals.append(("humorous", 1.0))
    if _TECH_RE.search(text):
        signals.append(("technical", 1.0))
    if _SUPPORT_RE.search(text):
        signals.append(("supportive", 1.0))
    if _DOWN_RE.search(text):
        signals.append(("down", 1.2))

    exclaims = text.count("!")
    letters = sum(c.isalpha() for c in text)
    caps = sum(c.isupper() for c in text)
    if exclaims >= 2 or (letters > 8 and caps / max(1, letters) > 0.6):
        signals.append(("upbeat", 1.0))

    length = len(stripped)
    if length <= 24 and length > 0:
        signals.append(("blunt", 0.8))
    elif length >= 180:
        signals.append(("chatty", 0.8))
    return signals


# ── Signal ingestion ──────────────────────────────────────────────────────────
async def _ingest_signal(
    db, user_id: int, guild_id: int, trait: str, weight: float,
    event_type: str, subtype: str = "",
) -> None:
    """Log an event and upsert one trait with DB-side time-decay."""
    if trait not in TRAITS:
        return
    await db.execute(
        "INSERT INTO ai_user_events (user_id, guild_id, event_type, subtype) "
        "VALUES ($1,$2,$3,$4)",
        int(user_id), int(guild_id), event_type, subtype or trait,
    )
    await db.execute(
        "INSERT INTO ai_user_traits "
        "(user_id, guild_id, trait, weight, sample_size, last_observed_at) "
        "VALUES ($1,$2,$3,$4,1,NOW()) "
        "ON CONFLICT (user_id, guild_id, trait) DO UPDATE SET "
        "weight = ai_user_traits.weight "
        "  * exp(-$5::double precision "
        "        * EXTRACT(EPOCH FROM (NOW() - ai_user_traits.last_observed_at))) "
        "  + $4, "
        "sample_size = ai_user_traits.sample_size + 1, "
        "last_observed_at = NOW()",
        int(user_id), int(guild_id), trait, float(weight), _LAMBDA,
    )
    rival = _CONFLICTS.get(trait)
    if rival:
        await db.execute(
            "UPDATE ai_user_traits SET weight = weight * 0.8 "
            "WHERE user_id=$1 AND guild_id=$2 AND trait=$3",
            int(user_id), int(guild_id), rival,
        )


async def ingest_message_tone(db, user_id: int, guild_id: int, content: str) -> None:
    """Detect tone from a chat message and feed every matched trait."""
    for trait, weight in _detect_tone_signals(content):
        await _ingest_signal(db, user_id, guild_id, trait, weight, "message")


# Emoji reaction category -> trait signal.
_REACTION_TRAITS: dict[str, str] = {
    "positive": "supportive",
    "laugh": "humorous",
    "negative": "down",
    "hype": "upbeat",
}


async def ingest_reaction(db, user_id: int, guild_id: int, category: str) -> None:
    """An emoji reaction in a known category nudges the matching trait."""
    trait = _REACTION_TRAITS.get(category)
    if trait:
        await _ingest_signal(db, user_id, guild_id, trait, 0.6, "reaction", category)
    await db.execute(
        "INSERT INTO ai_reaction_memory (user_id, guild_id, category, count) "
        "VALUES ($1,$2,$3,1) "
        "ON CONFLICT (user_id, guild_id, category) DO UPDATE "
        "SET count = ai_reaction_memory.count + 1",
        int(user_id), int(guild_id), category,
    )


async def ingest_tool_use(db, user_id: int, guild_id: int, tool_key: str) -> None:
    """Record that a member's message activated a tool."""
    await db.execute(
        "INSERT INTO ai_tool_memory (user_id, guild_id, tool_key, count, last_used_at) "
        "VALUES ($1,$2,$3,1,NOW()) "
        "ON CONFLICT (user_id, guild_id, tool_key) DO UPDATE "
        "SET count = ai_tool_memory.count + 1, last_used_at = NOW()",
        int(user_id), int(guild_id), tool_key,
    )
    # Tool use is a weak signal toward curiosity / technical interest.
    await _ingest_signal(db, user_id, guild_id, "technical", 0.4, "tool", tool_key)


# ── Read side ─────────────────────────────────────────────────────────────────
async def get_traits(db, user_id: int, guild_id: int) -> list[dict]:
    """Return decayed traits with confidence, strongest first."""
    rows = await db.fetch_all(
        "SELECT trait, "
        "weight * exp(-$3::double precision "
        "           * EXTRACT(EPOCH FROM (NOW() - last_observed_at))) AS weight, "
        "sample_size, "
        "EXTRACT(EPOCH FROM (NOW() - last_observed_at)) AS age "
        "FROM ai_user_traits WHERE user_id=$1 AND guild_id=$2",
        int(user_id), int(guild_id), _LAMBDA,
    )
    out: list[dict] = []
    for r in rows:
        weight = float(r["weight"] or 0)
        if weight < 0.15:
            continue
        confidence = 1 - math.exp(-(r["sample_size"] or 0) / _K)
        out.append({
            "trait": r["trait"],
            "weight": weight,
            "confidence": confidence,
            "label": TRAITS.get(r["trait"], r["trait"]),
        })
    out.sort(key=lambda t: t["weight"], reverse=True)
    return out


async def get_trait_context(db, user_id: int, guild_id: int, *, limit: int = 4) -> str:
    """Render a member's strongest traits as a system-prompt snippet."""
    traits = await get_traits(db, user_id, guild_id)
    traits = [t for t in traits if t["confidence"] >= 0.3][:limit]
    if not traits:
        return ""
    parts = [f"{t['label']}" for t in traits]
    return "Read on this member's style: " + "; ".join(parts) + "."


async def prune_traits(db, user_id: int, guild_id: int) -> None:
    """Drop traits that have decayed below the noise floor."""
    await db.execute(
        "DELETE FROM ai_user_traits WHERE user_id=$1 AND guild_id=$2 AND "
        "weight * exp(-$3::double precision "
        "           * EXTRACT(EPOCH FROM (NOW() - last_observed_at))) < 0.05",
        int(user_id), int(guild_id), _LAMBDA,
    )
