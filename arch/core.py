"""arch/core.py -- the ArchAgent: Archimedes as a coherent application.

This is the front door for every channel. A Discord message, a future web
request, a CLI prompt -- all of them route into ``ArchAgent.handle`` with
a small ``ChannelContext`` and walk out with an ``ArchResponse``.

The agent assembles every turn from the same parts:

  * **Soul**         (system prompt, picked from ``arch.soul.SoulStore``)
  * **Memories**     (top facts merged into the prompt before the user msg)
  * **Service chain** (the first healthy provider in ``arch.services``)
  * **Tools**         (the bot's existing registry plus MCP-bridged tools)

The heavy lifting -- streaming, multi-step tool loops, the result
pipeline -- still lives in the existing ``ai/`` modules. ArchAgent is the
conductor: it builds the prompt, picks the model, calls into the
existing client, then wraps the answer as an ``ArchResponse``.

A separate ``run_heartbeat`` entry runs one self-check turn with the
heartbeat prompt and no tools. ``ChannelContext`` keeps channel-specific
metadata (session key, transport name) so the agent can route a reminder
back to the channel that scheduled it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from arch.config import ArchConfig
from arch.dynamic_ui import ArchResponse, Card
from arch.heartbeat import Heartbeat, HeartbeatStore
from arch.mcp import MCPRegistry
from arch.memories import ArchMemory, format_for_prompt
from arch.scheduler import Scheduler, SchedulerStore
from arch.services import ServiceChain
from arch.soul import SoulStore

log = logging.getLogger(__name__)


# ── Channel-agnostic call context ─────────────────────────────────────────────
@dataclass
class ChannelContext:
    """Everything the agent needs to handle one inbound message.

    A channel populates the fields it knows; the agent treats missing
    fields as zero / blank. ``session_key`` is the routing identifier
    (``arch:discord:channel:123``); ``user_id`` and ``guild_id`` are the
    transport's native ids so existing memory scopes still match.
    """

    session_key: str = ""
    transport: str = ""
    user_id: int = 0
    guild_id: int = 0
    channel_id: int = 0
    display_name: str = ""
    is_dm: bool = False
    is_thread: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ── The agent ────────────────────────────────────────────────────────────────
class ArchAgent:
    """The application-level assistant. Owns Soul, Memories, Scheduler,
    Heartbeat, MCP and the service chain; calls into the existing
    ``ai.client``/``ai.tools`` plumbing for the actual model work.

    Construction is cheap; the real connections happen in ``start()``,
    which the bot calls from ``setup_hook`` after the database is up.
    """

    def __init__(
        self,
        *,
        config: ArchConfig,
        db,
        memory_service,
        tool_registry,
    ) -> None:
        self.config = config
        self.db = db
        self.tools = tool_registry
        self.memory = ArchMemory(memory_service)
        self.soul = SoulStore(db)
        self.scheduler_store = SchedulerStore(db)
        self.heartbeat_store = HeartbeatStore(db)
        self.services = ServiceChain.from_specs(config.services)
        self.mcp = MCPRegistry(db)
        self.scheduler: Scheduler | None = None
        self.heartbeat: Heartbeat | None = None
        self._announcer = None  # channel-supplied broadcast hook
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Open background loops and connect to declared MCP servers."""
        if self._started:
            return
        self._started = True

        env_specs = list(self.config.mcp_servers)
        db_specs = await self.mcp.load_from_db()
        if env_specs or db_specs:
            await self.mcp.connect_all(env_specs + db_specs)
            tool_count = len(self.mcp.tools())
            log.info(
                "Archimedes MCP: %d server(s), %d tool(s) bridged.",
                len(self.mcp.servers()), tool_count,
            )

        if self.config.scheduler.enabled:
            self.scheduler = Scheduler(
                self.scheduler_store,
                handler=self._fire_scheduled,
                poll_seconds=self.config.scheduler.poll_seconds,
                max_concurrent=self.config.scheduler.max_concurrent,
            )
            await self.scheduler.start()

        if self.config.heartbeat.enabled:
            self.heartbeat = Heartbeat(
                self.heartbeat_store,
                runner=self.run_heartbeat,
                enabled=True,
                interval_minutes=self.config.heartbeat.interval_minutes,
                active_hour_start=self.config.heartbeat.active_hour_start,
                active_hour_end=self.config.heartbeat.active_hour_end,
                prompt=self.config.heartbeat.prompt,
                announcer=self._announce_heartbeat,
            )
            await self.heartbeat.start()

    async def stop(self) -> None:
        if self.scheduler is not None:
            await self.scheduler.stop()
        if self.heartbeat is not None:
            await self.heartbeat.stop()
        await self.mcp.close_all()
        self._started = False

    def register_announcer(self, announcer) -> None:
        """A channel hands the agent a coroutine to post heartbeat or
        scheduled-task output back to the user. The agent calls it with an
        ``ArchResponse`` (or a ``HeartbeatResult``); the channel decides
        how to render it."""
        self._announcer = announcer

    # ── prompt assembly ──────────────────────────────────────────────────────
    async def build_system_prompt(self, ctx: ChannelContext) -> str:
        """Compose Soul + the safety base + a memory block.

        The safety base is ``ai.prompts.BASE_SYSTEM_INSTRUCTIONS`` -- the
        non-negotiable rules every turn carries. The soul layer wraps it.
        Memories follow as a small section the agent must read before
        replying.
        """
        from ai.prompts import BASE_SYSTEM_INSTRUCTIONS  # noqa: WPS433
        soul = await self.soul.get()
        mem = await self.memory.top_for_prompt(
            user_id=ctx.user_id, guild_id=ctx.guild_id, limit=5,
        )
        parts = [soul.prompt.strip(), BASE_SYSTEM_INSTRUCTIONS]
        block = format_for_prompt(mem)
        if block:
            parts.append(block)
        return "\n\n".join(parts)

    # ── one turn ─────────────────────────────────────────────────────────────
    async def handle(
        self,
        message_text: str,
        ctx: ChannelContext,
        *,
        max_tokens: int = 400,
        temperature: float = 0.7,
    ) -> ArchResponse:
        """Run one turn through the service chain.

        This is the simple, non-streaming, non-tool-calling path: enough
        for slash commands, heartbeats, and one-shot prompts. The full
        tool loop still goes through ``ai.tools.run_agent_stream`` in the
        Discord cog; that path will call back into this object for soul
        and memory prep in a follow-up commit, but it does not need to be
        re-implemented here to ship Archimedes 3.0.
        """
        system = await self.build_system_prompt(ctx)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": message_text},
        ]
        text = await self.services.complete(
            messages, max_tokens=max_tokens, temperature=temperature,
        )
        return ArchResponse(text=text)

    async def run_heartbeat(self, prompt: str) -> str:
        """Run one heartbeat turn with no tools and a tight budget."""
        ctx = ChannelContext(
            session_key="arch:heartbeat", transport="heartbeat",
        )
        # The heartbeat soul block stays the same as a normal turn; only
        # the user-side prompt differs. A future revision may swap to a
        # heartbeat-specific soul preset.
        system = await self.build_system_prompt(ctx)
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        return await self.services.complete(
            messages, max_tokens=200, temperature=0.4,
        )

    # ── scheduler glue ───────────────────────────────────────────────────────
    async def _fire_scheduled(self, task) -> None:
        """When a scheduled task is due, treat its payload as if the user
        had typed the prompt themselves, then push the result through the
        registered announcer (if any). A scheduled task with no announcer
        leaves only a log line behind, which is fine for cron health-checks."""
        payload = task.payload or {}
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            log.warning("Scheduled task %d has no prompt -- skipping.", task.id)
            return
        ctx = ChannelContext(
            session_key=payload.get("session_key", "arch:scheduler"),
            transport=payload.get("transport", "scheduler"),
            user_id=int(payload.get("user_id") or task.owner_id or 0),
            guild_id=int(payload.get("guild_id") or 0),
            channel_id=int(payload.get("channel_id") or 0),
        )
        response = await self.handle(prompt, ctx)
        if self._announcer:
            await self._announcer(ctx, response)

    async def _announce_heartbeat(self, result) -> None:
        if self._announcer is None:
            return
        ctx = ChannelContext(
            session_key="arch:heartbeat", transport="heartbeat",
        )
        await self._announcer(
            ctx,
            ArchResponse(
                text=f"[heartbeat] {result.detail}",
                card=Card(
                    title="Heartbeat acted",
                    body=result.detail,
                    accent="info",
                    footer=f"{result.duration_ms} ms",
                ),
            ),
        )
