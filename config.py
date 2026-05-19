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


def _env_list(key: str) -> list[str]:
    """A comma-separated env var as a list of trimmed, non-empty items."""
    raw = os.environ.get(key) or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


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
        "OPENROUTER_IMAGE_MODEL", "x-ai/grok-imagine-image-quality")
    OPENROUTER_VIDEO_MODEL: str = _env(
        "OPENROUTER_VIDEO_MODEL", "x-ai/grok-imagine-video")
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

    # ── Agent SDK sidecar ─────────────────────────────────────────────────────
    # The multi-step tool-calling loop runs in a Node sidecar that embeds the
    # OpenRouter Agent SDK (agent-sidecar/). When the sidecar is unreachable
    # the bot falls back to its in-process loop, so this is safe to leave on.
    AGENT_SIDECAR_ENABLED: bool = _env_bool("AGENT_SIDECAR_ENABLED", True)
    # An external sidecar WebSocket endpoint. Blank autostarts a local one.
    AGENT_SIDECAR_URL: str = _env("AGENT_SIDECAR_URL")
    # Port for the autostarted local sidecar.
    AGENT_SIDECAR_PORT: int = _env_int("AGENT_SIDECAR_PORT", 8770)
    # Stop conditions for the agent loop: a hard ceiling on model steps, and
    # an optional per-turn cost cap in US dollars (0 disables the cost cap).
    AGENT_MAX_STEPS: int = _env_int("AGENT_MAX_STEPS", 4)
    AGENT_MAX_COST: float = _env_float("AGENT_MAX_COST", 0.0)
    # Sidecar provider routing and resilience, passed straight to the Agent
    # SDK. AGENT_FALLBACK_MODELS is a comma-separated list of models the
    # sidecar tries, in order, after the primary one. AGENT_PROVIDER_ORDER is
    # a comma-separated list of provider slugs to prefer; AGENT_SERVER_TOOLS a
    # comma-separated list of OpenRouter server-tool types to enable (they run
    # server-side, no bridging). All default empty -- an unset deployment
    # behaves exactly as before.
    AGENT_FALLBACK_MODELS: list[str] = _env_list("AGENT_FALLBACK_MODELS")
    AGENT_PROVIDER_ORDER: list[str] = _env_list("AGENT_PROVIDER_ORDER")
    AGENT_PROVIDER_ALLOW_FALLBACKS: bool = _env_bool(
        "AGENT_PROVIDER_ALLOW_FALLBACKS", True)
    AGENT_SERVER_TOOLS: list[str] = _env_list("AGENT_SERVER_TOOLS")

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

    # ── Agent file workspace ──────────────────────────────────────────────────
    # The files.* and shell.run agent tools operate inside a sandboxed
    # workspace: one directory per Discord server (per user in a DM), with no
    # way out -- a tool can never reach the bot's own files or another
    # server's. WORKSPACE_ROOT is the base directory those per-server
    # workspaces live under; blank puts it at .workspace/ beside the bot. The
    # caps bound a single file, the whole workspace, and the file count, so
    # the workspace can never fill the host disk. WORKSPACE_SHELL_* govern the
    # allowlist shell tool: turn it off to drop shell.run while keeping the
    # file tools.
    WORKSPACE_ENABLED: bool = _env_bool("WORKSPACE_ENABLED", True)
    WORKSPACE_ROOT: str = _env("WORKSPACE_ROOT")
    WORKSPACE_MAX_FILE_KB: int = _env_int("WORKSPACE_MAX_FILE_KB", 64)
    WORKSPACE_QUOTA_KB: int = _env_int("WORKSPACE_QUOTA_KB", 4096)
    WORKSPACE_MAX_FILES: int = _env_int("WORKSPACE_MAX_FILES", 200)
    WORKSPACE_SHELL_ENABLED: bool = _env_bool("WORKSPACE_SHELL_ENABLED", True)
    WORKSPACE_SHELL_TIMEOUT_S: int = _env_int("WORKSPACE_SHELL_TIMEOUT_S", 5)

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
