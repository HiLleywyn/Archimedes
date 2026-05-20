"""arch/config.py -- Archimedes application settings.

A small typed view over the Archimedes-specific environment variables. The bot's
top-level ``config.Config`` still owns the Discord, database, model-provider
and pipeline knobs; this module only covers the Archimedes personality layer:

  * Soul (the editable system prompt and its preset name)
  * Heartbeat (autonomous self-check cadence and active window)
  * Scheduler (durable task runner)
  * MCP servers (declared at boot, more added at runtime)
  * Service chain (ordered list of model providers, with fallback)
  * Dynamic UI (whether Archimedes may emit cards alongside prose)

All values are env-driven and frozen. Database overrides are merged in by
``arch.core.ArchAgent.reload_settings`` so an operator can flip a switch with
``/soul`` or ``.ai arch`` without redeploying.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from config import _env, _env_bool, _env_int, _env_list


@dataclass(frozen=True)
class HeartbeatConfig:
    """Autonomous self-check loop. Off by default so a fresh deployment
    never spends model tokens unprompted."""

    enabled: bool
    interval_minutes: int
    active_hour_start: int  # 0-23, local server clock
    active_hour_end: int    # 0-23, exclusive
    prompt: str
    model: str  # blank means "use the Archimedes default"


@dataclass(frozen=True)
class SchedulerConfig:
    enabled: bool
    poll_seconds: int
    max_concurrent: int


@dataclass(frozen=True)
class MCPServerSpec:
    """One MCP server declared in the environment. Servers added at runtime
    live in the ``archimedes_mcp_servers`` table and are not represented here."""

    name: str
    transport: str   # "stdio" or "http"
    url: str         # used for http
    command: str     # used for stdio
    args: tuple[str, ...]


@dataclass(frozen=True)
class ServiceSpec:
    """One entry in the model service fallback chain."""

    name: str        # "openrouter", "ollama", "anthropic"
    model: str       # may be blank to use the provider default


@dataclass(frozen=True)
class ArchConfig:
    """Frozen Archimedes application configuration. Built once at boot."""

    soul: str
    soul_preset: str
    dynamic_ui_enabled: bool
    heartbeat: HeartbeatConfig
    scheduler: SchedulerConfig
    mcp_servers: tuple[MCPServerSpec, ...]
    services: tuple[ServiceSpec, ...]
    dm_policy: str
    guild_policy: str

    @classmethod
    def from_env(cls) -> "ArchConfig":
        return cls(
            soul=_env("ARCHIMEDES_SOUL", ""),
            soul_preset=_env("ARCHIMEDES_SOUL_PRESET", "default"),
            dynamic_ui_enabled=_env_bool("ARCHIMEDES_DYNAMIC_UI_ENABLED", True),
            heartbeat=HeartbeatConfig(
                enabled=_env_bool("ARCHIMEDES_HEARTBEAT_ENABLED", False),
                interval_minutes=_env_int("ARCHIMEDES_HEARTBEAT_INTERVAL", 30),
                active_hour_start=_env_int("ARCHIMEDES_HEARTBEAT_HOUR_START", 8),
                active_hour_end=_env_int("ARCHIMEDES_HEARTBEAT_HOUR_END", 22),
                prompt=_env(
                    "ARCHIMEDES_HEARTBEAT_PROMPT",
                    "[HEARTBEAT] This is an automatic self-check. Review your "
                    "memories and pending tasks. If everything looks good and "
                    "nothing needs attention, respond with exactly: "
                    "HEARTBEAT_OK. If something needs attention (stale "
                    "memories, due tasks, user follow-ups), address it.",
                ),
                model=_env("ARCHIMEDES_HEARTBEAT_MODEL", ""),
            ),
            scheduler=SchedulerConfig(
                enabled=_env_bool("ARCHIMEDES_SCHEDULER_ENABLED", True),
                poll_seconds=_env_int("ARCHIMEDES_SCHEDULER_POLL_SECONDS", 15),
                max_concurrent=_env_int("ARCHIMEDES_SCHEDULER_MAX_CONCURRENT", 2),
            ),
            mcp_servers=_parse_mcp(_env("ARCHIMEDES_MCP_SERVERS", "")),
            services=_parse_services(_env_list("ARCHIMEDES_SERVICES")),
            dm_policy=_env("DISCORD_DM_POLICY", "allowlist").lower(),
            guild_policy=_env("DISCORD_GUILD_POLICY", "allowlist").lower(),
        )


def _parse_mcp(raw: str) -> tuple[MCPServerSpec, ...]:
    """Parse a compact MCP server spec list.

    Format: ``name1=http://host/mcp,name2=stdio:my-tool --flag``. A blank
    string returns no servers. The right-hand side starts with ``stdio:``
    for a local executable or with ``http(s)://`` for a streamable HTTP
    endpoint; anything else is rejected silently and logged at boot.
    """
    out: list[MCPServerSpec] = []
    if not raw:
        return tuple(out)
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, sep, value = entry.partition("=")
        name = name.strip()
        value = value.strip()
        if not name or not value:
            continue
        if value.startswith("stdio:"):
            tail = value[len("stdio:"):].strip()
            parts = tail.split()
            if not parts:
                continue
            out.append(MCPServerSpec(
                name=name, transport="stdio", url="",
                command=parts[0], args=tuple(parts[1:]),
            ))
        elif value.startswith("http://") or value.startswith("https://"):
            out.append(MCPServerSpec(
                name=name, transport="http", url=value,
                command="", args=(),
            ))
    return tuple(out)


def _parse_services(raw: list[str]) -> tuple[ServiceSpec, ...]:
    """Parse ``ARCHIMEDES_SERVICES`` as an ordered list of ``provider[:model]``.

    A blank list falls back to the bot's existing single backend, so an
    upgrade from a pre-Archimedes deployment behaves exactly as before.
    """
    out: list[ServiceSpec] = []
    for item in raw:
        name, sep, model = item.partition(":")
        name = name.strip().lower()
        model = model.strip()
        if name:
            out.append(ServiceSpec(name=name, model=model))
    if not out:
        # Single-backend fallback: pick whichever the bot was already using.
        backend = (os.environ.get("CHAT_BACKEND") or "openrouter").strip().lower()
        out.append(ServiceSpec(name=backend, model=""))
    return tuple(out)
