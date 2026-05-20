"""framework/bot.py -- the Archimedes bot class.

Owns the shared services (database, Redis short-term store, memory sidecar,
tool registry, training logger), loads the cogs, and tracks the ids of its
own AI replies so the chat cog can detect when a user replies to one.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import math
import time

import discord
from discord.ext import commands

from config import Config
from framework.context import ArchimedesContext
from framework.db import Database

log = logging.getLogger(__name__)

# Watchdog: if the gateway has not produced a healthy heartbeat for this
# long, force-close the socket so discord.py's auto-reconnect kicks in.
_WATCHDOG_INTERVAL_S = 30.0
_WATCHDOG_STALE_S = 120.0

# Cogs loaded on startup, in order.
COGS: tuple[str, ...] = (
    "cogs.meta",
    "cogs.chat",
    "cogs.archimedes",
    "cogs.ai_admin",
    "cogs.sidecar",
)


class ArchimedesBot(commands.Bot):
    """The standalone AI chat bot."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        super().__init__(
            command_prefix=Config.PREFIX,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            allowed_mentions=discord.AllowedMentions(
                everyone=False, roles=False, users=True,
            ),
        )
        self.db = Database(Config.DATABASE_URL)
        self.short_term = None    # ai.redis_store.ShortTermStore
        self.memory = None        # ai.memory.MemoryService
        self.tools = None         # ai.tools.ToolRegistry
        self.training = None      # ai.training.TrainingLogger
        self.plugins = None       # framework.plugins.PluginManager
        self.agent_sidecar = None  # ai.agent_sidecar.AgentSidecar
        # Bounded ring of message ids the bot sent as AI replies. Used to
        # tell "user replied to one of my answers" from "user replied to
        # someone else" without a DB round-trip.
        self._ai_message_ids: collections.deque[int] = collections.deque(maxlen=4000)
        # Thread ids Archimedes spawned for conversations. Any message in one is
        # treated as a continuation, even without an explicit reply.
        self._ai_thread_ids: collections.deque[int] = collections.deque(maxlen=2000)
        # Operator-initiated lifecycle. The outer process loop reads these
        # after start() returns: restart re-execs the interpreter, shutdown
        # exits, otherwise the loop retries the connection.
        self.restart_requested: bool = False
        self.shutdown_requested: bool = False
        # True after the first on_ready -- the outer loop uses this to
        # tell a fresh-boot failure from a mid-session reconnect.
        self.ever_ready: bool = False
        # Last time we saw a healthy heartbeat. The watchdog forces a
        # reconnect when this gets too old.
        self._last_healthy_ts: float = time.monotonic()
        self._watchdog_task: asyncio.Task | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def setup_hook(self) -> None:
        from ai.agent_sidecar import AgentSidecar
        from ai.memory import MemoryService
        from ai.redis_store import ShortTermStore
        from ai.tools import build_default_registry
        from ai.training import TrainingLogger
        from framework.plugins import PluginManager

        await self.db.connect()
        self.short_term = ShortTermStore()
        await self.short_term.connect()
        self.memory = MemoryService(self.db, self.short_term)
        self.training = TrainingLogger(self.db)
        self.tools = build_default_registry()

        # The agent loop prefers the OpenRouter Agent SDK sidecar; starting it
        # never blocks the bot -- a failure here just leaves the in-process
        # loop in charge.
        self.agent_sidecar = AgentSidecar()
        try:
            await self.agent_sidecar.start()
        except Exception:  # noqa: BLE001
            log.exception("agent sidecar failed to start")

        for ext in COGS:
            try:
                await self.load_extension(ext)
                log.info("loaded cog: %s", ext)
            except Exception:  # noqa: BLE001
                log.exception("failed to load cog: %s", ext)

        # Plugins load after the cogs so the manager sees every built-in
        # command and can refuse a plugin command that would collide.
        self.plugins = PluginManager(self)
        try:
            await self.plugins.startup()
        except Exception:  # noqa: BLE001
            log.exception("plugin manager failed to start")

        try:
            synced = await self.tree.sync()
            log.info("synced %d application command(s)", len(synced))
        except Exception:  # noqa: BLE001
            log.exception("failed to sync application commands")

        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(
                self._connection_watchdog(), name="archimedes-watchdog",
            )

    async def on_ready(self) -> None:
        self._last_healthy_ts = time.monotonic()
        self.ever_ready = True
        log.info("Logged in as %s (id=%s) in %d guild(s)",
                 self.user, getattr(self.user, "id", "?"), len(self.guilds))

    async def on_connect(self) -> None:
        self._last_healthy_ts = time.monotonic()
        log.info("Gateway connected.")

    async def on_resumed(self) -> None:
        self._last_healthy_ts = time.monotonic()
        log.info("Gateway session resumed.")

    async def on_disconnect(self) -> None:
        log.warning("Gateway disconnected -- auto-reconnect will retry.")

    # ── connection watchdog ───────────────────────────────────────────────────
    async def _connection_watchdog(self) -> None:
        """Force a reconnect if the gateway goes quiet for too long.

        discord.py auto-reconnects most disconnects on its own, but a wedged
        websocket -- one that never raises and never delivers heartbeats --
        can leave the bot looking online while it stops responding. The
        watchdog watches the latency and the last-healthy timestamp and
        closes the socket so the inner reconnect loop runs.
        """
        try:
            while not self.is_closed():
                await asyncio.sleep(_WATCHDOG_INTERVAL_S)
                if self.is_closed() or self.shutdown_requested:
                    return
                latency = self.latency
                healthy = (
                    latency is not None
                    and not math.isnan(latency)
                    and not math.isinf(latency)
                    and latency >= 0
                )
                if healthy:
                    self._last_healthy_ts = time.monotonic()
                    continue
                stale_for = time.monotonic() - self._last_healthy_ts
                if stale_for < _WATCHDOG_STALE_S:
                    continue
                log.warning(
                    "Gateway looks stale (no heartbeat in %.0fs) -- "
                    "closing websocket to force reconnect.",
                    stale_for,
                )
                self._last_healthy_ts = time.monotonic()
                ws = getattr(self, "ws", None)
                if ws is not None:
                    try:
                        await ws.close(code=4000)
                    except Exception:  # noqa: BLE001
                        log.exception("watchdog failed to close websocket")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("connection watchdog crashed")

    # ── operator lifecycle ────────────────────────────────────────────────────
    def request_restart(self) -> None:
        """Flag that the outer loop should re-exec after a clean shutdown."""
        self.restart_requested = True

    def request_shutdown(self) -> None:
        """Flag that the outer loop should exit instead of reconnecting."""
        self.shutdown_requested = True

    async def get_context(self, message, *, cls=ArchimedesContext):
        return await super().get_context(message, cls=cls)

    async def close(self) -> None:
        from ai.client import close_client

        log.info("Shutting down...")
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            if self.plugins is not None:
                await self.plugins.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.agent_sidecar is not None:
                await self.agent_sidecar.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            await close_client()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self.short_term is not None:
                await self.short_term.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self.db.close()
        except Exception:  # noqa: BLE001
            pass
        await super().close()

    # ── AI-reply bookkeeping ──────────────────────────────────────────────────
    def remember_ai_message(self, message_id: int) -> None:
        self._ai_message_ids.append(int(message_id))

    def is_ai_message(self, message_id: int | None) -> bool:
        return message_id is not None and int(message_id) in self._ai_message_ids

    def remember_ai_thread(self, thread_id: int) -> None:
        self._ai_thread_ids.append(int(thread_id))

    def is_ai_thread(self, channel) -> bool:
        return getattr(channel, "id", None) in self._ai_thread_ids

    # ── error handling ────────────────────────────────────────────────────────
    async def on_command_error(self, ctx, error) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.reply_error("This command only works in a server.")
            return
        if isinstance(error, (commands.CheckFailure, commands.MissingPermissions)):
            await ctx.reply_error(str(error) or "You can't use that command.")
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply_error(f"Missing argument: `{error.param.name}`.")
            return
        if isinstance(error, commands.BadArgument):
            await ctx.reply_error(f"Bad argument: {error}")
            return
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.reply_cooldown(error.retry_after)
            return
        log.exception("command error in %s", getattr(ctx.command, "name", "?"),
                      exc_info=error)
        try:
            await ctx.reply_error("Something broke running that. Try again.")
        except discord.HTTPException:
            pass
