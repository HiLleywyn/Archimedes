"""arch -- Archimedes as a personal-assistant application.

This package is the application layer the Discord bot now stands on top
of. The Discord cog hands an inbound message to ``ArchAgent.handle`` and
gets back an ``ArchResponse``; future channels (web, voice, CLI) consume
the same surface, so the agent is no longer welded to discord.py.

The public exports below are the only surface external code should touch.
Internal helpers live in their own modules and stay there.
"""
from __future__ import annotations

from arch.config import (
    ArchConfig, HeartbeatConfig, MCPServerSpec, SchedulerConfig, ServiceSpec,
)
from arch.core import ArchAgent, ChannelContext
from arch.dynamic_ui import (
    ArchResponse, Button, Card, Section, StatTile, Suggestion,
    render_card_plain,
)
from arch.heartbeat import (
    Heartbeat, HeartbeatResult, HeartbeatStore, OK_TOKEN, in_active_window,
)
from arch.mcp import MCPRegistry, MCPTool
from arch.memories import ArchMemory, MemoryEntry, format_for_prompt
from arch.scheduler import (
    Scheduler, SchedulerStore, ScheduledTask, cron_due, parse_oneshot_delay,
)
from arch.services import (
    ProviderError, ServiceChain, ServiceHealth, build_chain_from_env,
)
from arch.soul import (
    DEFAULT_SOUL, SOUL_MAX_CHARS, SOUL_PRESETS, SoulRecord, SoulStore,
    list_presets, normalise, preset,
)
from arch.version import ARCH_CODENAME, ARCH_VERSION

__version__ = ARCH_VERSION

__all__ = [
    "__version__",
    "ARCH_VERSION", "ARCH_CODENAME",
    "ArchAgent", "ChannelContext",
    "ArchConfig", "HeartbeatConfig", "MCPServerSpec",
    "SchedulerConfig", "ServiceSpec",
    "ArchResponse", "Button", "Card", "Section", "StatTile", "Suggestion",
    "render_card_plain",
    "Heartbeat", "HeartbeatResult", "HeartbeatStore", "OK_TOKEN",
    "in_active_window",
    "MCPRegistry", "MCPTool",
    "ArchMemory", "MemoryEntry", "format_for_prompt",
    "Scheduler", "SchedulerStore", "ScheduledTask", "cron_due",
    "parse_oneshot_delay",
    "ProviderError", "ServiceChain", "ServiceHealth", "build_chain_from_env",
    "DEFAULT_SOUL", "SOUL_MAX_CHARS", "SOUL_PRESETS", "SoulRecord",
    "SoulStore", "list_presets", "normalise", "preset",
]
