"""ai/models.py -- model categories and the per-guild model picker.

Each category (chat, tools, vision, search, reason) has an env-var default.
A guild admin can override any category via ``.ai model set``; the override
lives in ``ai_model_defaults`` and wins over the env default at runtime.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import Config


@dataclass(frozen=True)
class ModelOption:
    """A resolved (provider, model) pick."""

    provider: str
    model: str
    label: str = ""


@dataclass(frozen=True)
class Category:
    """One tunable model category."""

    key: str
    label: str
    description: str

    def env_default(self) -> ModelOption:
        provider = Config.CHAT_BACKEND
        if provider == "ollama":
            return ModelOption("ollama", Config.OLLAMA_MODEL)
        mapping = {
            "chat": Config.OPENROUTER_MODEL,
            "tools": Config.OPENROUTER_TOOLS_MODEL or Config.OPENROUTER_MODEL,
            "vision": Config.OPENROUTER_VISION_MODEL or Config.OPENROUTER_MODEL,
            "search": Config.OPENROUTER_MODEL,
            "reason": Config.OPENROUTER_REASON_MODEL or Config.OPENROUTER_MODEL,
        }
        return ModelOption("openrouter", mapping.get(self.key, Config.OPENROUTER_MODEL))


TOOL_CATEGORIES: tuple[Category, ...] = (
    Category("chat", "Chat", "Conversational replies to mentions / .ask."),
    Category("tools", "Tools", "The agent loop that calls tools."),
    Category("vision", "Vision", "Image understanding (describe attachments)."),
    Category("search", "Search", "Summarising web-search results."),
    Category("reason", "Reason", "Heavier multi-step reasoning."),
)

# A small curated catalog surfaced by .ai model show. Purely advisory.
_CATALOG: dict[str, list[ModelOption]] = {
    "chat": [
        ModelOption("openrouter", "openai/gpt-4o-mini", "GPT-4o mini (fast, cheap)"),
        ModelOption("openrouter", "anthropic/claude-3.5-haiku", "Claude 3.5 Haiku"),
        ModelOption("openrouter", "google/gemini-2.0-flash-001", "Gemini 2.0 Flash"),
    ],
    "tools": [
        ModelOption("openrouter", "openai/gpt-4o-mini", "GPT-4o mini"),
        ModelOption("openrouter", "anthropic/claude-3.5-sonnet", "Claude 3.5 Sonnet"),
    ],
    "vision": [
        ModelOption("openrouter", "openai/gpt-4o-mini", "GPT-4o mini (vision)"),
        ModelOption("openrouter", "google/gemini-2.0-flash-001", "Gemini 2.0 Flash"),
    ],
    "search": [
        ModelOption("openrouter", "openai/gpt-4o-mini", "GPT-4o mini"),
        ModelOption("openrouter", "perplexity/sonar", "Perplexity Sonar"),
    ],
    "reason": [
        ModelOption("openrouter", "anthropic/claude-3.5-sonnet", "Claude 3.5 Sonnet"),
        ModelOption("openrouter", "openai/o4-mini", "o4-mini"),
    ],
}

# Substrings that mark a model slug as multimodal / vision-capable.
_VISION_SLUGS = (
    "gpt-4o", "gpt-4.1", "claude-3", "claude-4", "gemini", "llava", "pixtral",
    "qwen-vl", "qwen2-vl", "gemma3:", "o4-mini", "vision",
)


def is_vision_capable_slug(model: str) -> bool:
    """Best-effort: does this model slug look multimodal?"""
    low = (model or "").lower()
    return any(s in low for s in _VISION_SLUGS)


def catalog_for(category: str) -> list[ModelOption]:
    return list(_CATALOG.get(category, []))


def category(key: str) -> Category | None:
    return next((c for c in TOOL_CATEGORIES if c.key == key), None)


async def resolve_model(db, guild_id: int, category_key: str) -> ModelOption:
    """Return the effective model for a category in a guild.

    Guild override (``ai_model_defaults``) wins; otherwise the env default.
    """
    cat = category(category_key) or TOOL_CATEGORIES[0]
    row = await db.fetch_one(
        "SELECT provider, model FROM ai_model_defaults "
        "WHERE guild_id=$1 AND category=$2",
        int(guild_id), cat.key,
    )
    if row and row.get("model"):
        return ModelOption(row["provider"], row["model"])
    return cat.env_default()


async def get_guild_default(db, guild_id: int, category_key: str) -> ModelOption | None:
    row = await db.fetch_one(
        "SELECT provider, model FROM ai_model_defaults "
        "WHERE guild_id=$1 AND category=$2",
        int(guild_id), category_key,
    )
    if row and row.get("model"):
        return ModelOption(row["provider"], row["model"])
    return None


async def list_guild_defaults(db, guild_id: int) -> dict[str, ModelOption | None]:
    rows = await db.fetch_all(
        "SELECT category, provider, model FROM ai_model_defaults WHERE guild_id=$1",
        int(guild_id),
    )
    by_cat = {r["category"]: ModelOption(r["provider"], r["model"]) for r in rows}
    return {c.key: by_cat.get(c.key) for c in TOOL_CATEGORIES}


async def set_guild_default(
    db, guild_id: int, category_key: str, provider: str, model: str,
    *, updated_by: int | None = None,
) -> None:
    await db.execute(
        "INSERT INTO ai_model_defaults "
        "(guild_id, category, provider, model, updated_by, updated_at) "
        "VALUES ($1,$2,$3,$4,$5,NOW()) "
        "ON CONFLICT (guild_id, category) DO UPDATE SET "
        "provider=EXCLUDED.provider, model=EXCLUDED.model, "
        "updated_by=EXCLUDED.updated_by, updated_at=NOW()",
        int(guild_id), category_key, provider, model, updated_by,
    )


async def clear_guild_default(db, guild_id: int, category_key: str) -> None:
    await db.execute(
        "DELETE FROM ai_model_defaults WHERE guild_id=$1 AND category=$2",
        int(guild_id), category_key,
    )
