"""config.py -- runtime configuration for the Archimedes chat bot.

Every value is sourced from an environment variable so the bot stays
twelve-factor and deploys cleanly on Railway / Docker. ``.env`` is loaded
automatically when python-dotenv is installed.
"""
from __future__ import annotations

import os

try:  # optional: load a local .env in development
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # noqa: BLE001 -- dotenv is optional
    pass


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key) or default)
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key) or default)
    except (TypeError, ValueError):
        return default


class Config:
    """Frozen, env-driven settings. Read-only at runtime."""

    # ── Discord ───────────────────────────────────────────────────────────────
    TOKEN: str = _env("DISCORD_TOKEN")
    PREFIX: str = _env("PREFIX", ".")
    OWNER_ID: int = _env_int("OWNER_ID", 0)

    # ── Storage ───────────────────────────────────────────────────────────────
    DATABASE_URL: str = _env("DATABASE_URL")
    REDIS_URL: str = _env("REDIS_URL")

    # ── Model provider ────────────────────────────────────────────────────────
    OPENROUTER_API_KEY: str = _env("OPENROUTER_API_KEY")
    OPENROUTER_BASE_URL: str = _env("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    OPENROUTER_MODEL: str = _env("OPENROUTER_MODEL", "openai/gpt-4o-mini")
    OPENROUTER_VISION_MODEL: str = _env("OPENROUTER_VISION_MODEL", "openai/gpt-4o-mini")
    OPENROUTER_TOOLS_MODEL: str = _env("OPENROUTER_TOOLS_MODEL")
    OPENROUTER_REASON_MODEL: str = _env("OPENROUTER_REASON_MODEL")
    # Image and video generation models. Both run on OpenRouter regardless of
    # CHAT_BACKEND -- the local Ollama backend has no equivalent.
    OPENROUTER_IMAGE_MODEL: str = _env(
        "OPENROUTER_IMAGE_MODEL", "google/gemini-2.5-flash-image-preview")
    OPENROUTER_VIDEO_MODEL: str = _env("OPENROUTER_VIDEO_MODEL", "google/veo-3.1")
    OPENROUTER_REFERER: str = _env("OPENROUTER_REFERER", "https://github.com")
    OPENROUTER_TITLE: str = _env("OPENROUTER_TITLE", "Archimedes")

    # ── Local backend (Ollama) ────────────────────────────────────────────────
    OLLAMA_BASE_URL: str = _env("OLLAMA_BASE_URL")
    OLLAMA_MODEL: str = _env("OLLAMA_MODEL", "llama3.1")
    CHAT_BACKEND: str = _env("CHAT_BACKEND", "openrouter").lower()

    # ── Web search ────────────────────────────────────────────────────────────
    SEARCH_BACKEND: str = _env("SEARCH_BACKEND", "ddg").lower()
    BRAVE_SEARCH_API_KEY: str = _env("BRAVE_SEARCH_API_KEY")

    # ── AI behaviour ──────────────────────────────────────────────────────────
    AI_REPLY_TIMEOUT_S: int = _env_int("AI_REPLY_TIMEOUT_S", 90)
    AI_QUEUE_CAP: int = _env_int("AI_QUEUE_CAP", 6)
    AI_QUOTA_LIMIT: int = _env_int("AI_QUOTA_LIMIT", 25)
    AI_QUOTA_WINDOW: int = _env_int("AI_QUOTA_WINDOW", 3600)
    AI_COOLDOWN_S: float = _env_float("AI_COOLDOWN_S", 4.0)
    PASSIVE_LEARNING: bool = _env_bool("ARCHIMEDES_PASSIVE_LEARNING", False)
    AMBIENT_REPLIES: bool = _env_bool("AMBIENT_REPLIES", False)

    # ── Memory ────────────────────────────────────────────────────────────────
    MEMORY_REFRESH_HOURS: int = _env_int("MEMORY_REFRESH_HOURS", 4)
    SHORT_TERM_TURNS: int = _env_int("SHORT_TERM_TURNS", 12)
    SHORT_TERM_TTL_S: int = _env_int("SHORT_TERM_TTL_S", 86400)

    # ── Misc ──────────────────────────────────────────────────────────────────
    DEBUG: bool = _env_bool("DEBUG", False)
    LOG_LEVEL: str = _env("LOG_LEVEL", "INFO").upper()

    # ── Lua plugins ───────────────────────────────────────────────────────────
    PLUGINS_ENABLED: bool = _env_bool("PLUGINS_ENABLED", True)
    PLUGIN_REGISTRY_REPO: str = _env(
        "PLUGIN_REGISTRY_REPO", "hilleywyn/archimedes-plugins",
    )
    PLUGIN_REGISTRY_REF: str = _env("PLUGIN_REGISTRY_REF", "main")
    GITHUB_TOKEN: str = _env("GITHUB_TOKEN")

    # ── Plugin HTTP client ────────────────────────────────────────────────────
    # The outbound HTTP surface handed to Lua plugins as `arch.http`. Every
    # request is SSRF-guarded; these caps are hard ceilings a per-call `opts`
    # table may lower but never raise.
    PLUGIN_HTTP_ENABLED: bool = _env_bool("PLUGIN_HTTP_ENABLED", True)
    PLUGIN_HTTP_TIMEOUT_S: int = _env_int("PLUGIN_HTTP_TIMEOUT_S", 10)
    PLUGIN_HTTP_MAX_BYTES: int = _env_int("PLUGIN_HTTP_MAX_BYTES", 1048576)
    PLUGIN_HTTP_MAX_REDIRECTS: int = _env_int("PLUGIN_HTTP_MAX_REDIRECTS", 3)
    # An escape hatch for self-hosted operators who deliberately want plugins
    # to reach a private network. Off by default -- leave it off.
    PLUGIN_HTTP_ALLOW_PRIVATE: bool = _env_bool("PLUGIN_HTTP_ALLOW_PRIVATE", False)

    # ── Tool execution pipeline ───────────────────────────────────────────────
    # Compression caps for the deterministic tool-output pipeline. A string
    # field longer than PIPELINE_MAX_STRING is truncated; a list longer than
    # PIPELINE_MAX_LIST is capped. PIPELINE_INJECT_MAX_CHARS is the hard
    # ceiling on the JSON tool result handed back to the model. Tune these if
    # tool results are arriving over- or under-compressed.
    PIPELINE_MAX_STRING: int = _env_int("PIPELINE_MAX_STRING", 1200)
    PIPELINE_MAX_LIST: int = _env_int("PIPELINE_MAX_LIST", 25)
    PIPELINE_INJECT_MAX_CHARS: int = _env_int("PIPELINE_INJECT_MAX_CHARS", 4000)

    @classmethod
    def validate(cls) -> list[str]:
        """Return a list of fatal configuration problems (empty when OK)."""
        problems: list[str] = []
        if not cls.TOKEN:
            problems.append("DISCORD_TOKEN is required.")
        if not cls.DATABASE_URL:
            problems.append("DATABASE_URL is required.")
        if cls.CHAT_BACKEND == "openrouter" and not cls.OPENROUTER_API_KEY:
            problems.append(
                "OPENROUTER_API_KEY is required when CHAT_BACKEND=openrouter."
            )
        if cls.CHAT_BACKEND == "ollama" and not cls.OLLAMA_BASE_URL:
            problems.append(
                "OLLAMA_BASE_URL is required when CHAT_BACKEND=ollama."
            )
        return problems

    @classmethod
    def plugin_config(cls, plugin_id: str) -> dict[str, str]:
        """Operator configuration for one plugin, read from the environment.

        A ``PLUGIN_<ID>_<KEY>`` environment variable is exposed to plugin
        ``<id>`` as ``<key>`` (lower-cased): ``PLUGIN_IMAGEGEN_API_KEY``
        reaches the ``imagegen`` plugin as ``config.api_key``. A plugin sees
        only variables under its own prefix -- never another plugin's
        configuration, and never the bot's own secrets.
        """
        prefix = "PLUGIN_" + plugin_id.upper().replace("-", "_") + "_"
        out: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.startswith(prefix) and value and value.strip():
                out[key[len(prefix):].lower()] = value.strip()
        return out
