"""ai/agent_sidecar.py -- the OpenRouter Agent SDK sidecar bridge.

The OpenRouter Agent SDK (``@openrouter/agent``) is a TypeScript package, so
its multi-step tool-calling loop runs in a small Node service: the sidecar in
``agent-sidecar/``. This module is the Python half of the bridge.

Two responsibilities live here:

  * :class:`AgentSidecar` -- supervises the Node process. It autostarts a
    local sidecar (or points at an external one), polls it healthy, restarts
    it on crash with backoff, and tears it down on shutdown. After too many
    consecutive failures it latches unhealthy and every turn falls back to the
    in-process loop.
  * :meth:`AgentSidecar.run_stream` -- drives one chat turn over a WebSocket.

The protocol is a compact JSON exchange (see ``agent-sidecar/src/server.ts``):
the sidecar greets the connection with a ``hello`` frame, the bot sends a
``start`` frame, the sidecar streams ``delta`` text and ``tool_call``
requests, the bot runs each tool through the execution pipeline and answers
with ``tool_result``, and the turn ends with ``done`` or ``error``.

``run_stream`` yields the same event vocabulary as the in-process agent loop
in :mod:`ai.tools`, so the chat cog consumes either transparently. When the
sidecar cannot be reached it raises :class:`AgentSidecarUnavailable` before
yielding anything, so the caller can fall back to the in-process loop.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import shutil
import time

import aiohttp

from config import Config
from framework.pipeline import run_pipeline

log = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 10.0
_HEALTH_TIMEOUT_S = 15.0

# Wire-protocol version. Must match PROTOCOL_VERSION in agent-sidecar/src.
# Bumped whenever a message shape changes; the handshake fails fast when the
# two halves disagree, which catches a half-finished deploy.
_PROTOCOL_VERSION = 1

# Supervisor restart policy. After a crash the process is respawned with an
# escalating backoff; after _MAX_RESTART_ATTEMPTS consecutive failures the
# supervisor gives up and latches unhealthy. A process that stays healthy for
# longer than _HEALTHY_RESET_S clears the failure streak.
_RESTART_BACKOFFS = (2.0, 4.0, 8.0, 16.0)
_MAX_RESTART_ATTEMPTS = 4
_HEALTHY_RESET_S = 60.0

# Sustained-fallback alert. When the sidecar is enabled but turns keep landing
# on the in-process loop, the sidecar is degraded without anything failing
# loudly -- this turns that into a warning.
_FALLBACK_WINDOW_S = 600.0
_FALLBACK_MIN_SAMPLES = 5
_FALLBACK_ALERT_RATE = 0.5
_FALLBACK_ALERT_COOLDOWN_S = 300.0


class AgentSidecarUnavailable(RuntimeError):
    """Raised when the sidecar cannot be reached before a turn produces output.

    The caller catches this to fall back to the in-process agent loop. Once a
    turn has started streaming events the sidecar never raises it -- a failure
    after that point is reported as an ``error`` event instead.
    """


def log_agent_turn(
    turn_id: str,
    path: str,
    *,
    connect_ms: int,
    model_ms: int,
    tool_count: int,
    model: str | None,
) -> None:
    """Emit one structured per-turn observability event.

    A single greppable log line. ``connect_ms`` (the WebSocket setup cost, 0
    for the in-process loop) is kept separate from ``model_ms`` so connection
    jitter does not hide inside model latency. ``path`` is ``"sidecar"`` or
    ``"in_process"``; correlate with the sidecar's own ``[turn <id>]`` lines
    through ``turn_id``.
    """
    log.info(
        "agent turn %s",
        json.dumps(
            {
                "turn_id": turn_id or "-",
                "path": path,
                "connect_ms": connect_ms,
                "model_ms": model_ms,
                "tool_count": tool_count,
                "model": model or "default",
            },
            separators=(",", ":"),
        ),
    )


def _server_script() -> str:
    """Absolute path to the built sidecar entry point."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "agent-sidecar", "dist", "server.js")


class AgentSidecar:
    """Owns the Node agent sidecar process and the WebSocket bridge to it."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._log_task: asyncio.Task | None = None
        self._supervisor_task: asyncio.Task | None = None
        self._owns_process = False
        self._url = ""
        self._port = 0
        self._shutting_down = False
        # Latched once the supervisor gives up restarting a crashed process.
        self._unhealthy = False
        self._consecutive_failures = 0
        self._last_healthy_at = 0.0
        # Sliding window of (monotonic_time, path) for the fallback alert.
        self._turn_log: collections.deque[tuple[float, str]] = (
            collections.deque()
        )
        self._last_fallback_alert = 0.0

    @property
    def available(self) -> bool:
        """True when a turn may be routed to the sidecar."""
        return bool(
            Config.AGENT_SIDECAR_ENABLED
            and self._url
            and not self._unhealthy
        )

    @property
    def url(self) -> str:
        return self._url

    @property
    def unhealthy(self) -> bool:
        """True once the supervisor has given up restarting the sidecar."""
        return self._unhealthy

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def start(self) -> None:
        """Resolve the sidecar endpoint, autostarting a local one if needed."""
        if not Config.AGENT_SIDECAR_ENABLED:
            log.info("agent sidecar disabled; using the in-process loop")
            return
        if Config.AGENT_SIDECAR_URL:
            self._url = Config.AGENT_SIDECAR_URL
            log.info("agent sidecar: using external endpoint %s", self._url)
            return

        server_js = _server_script()
        if not os.path.exists(server_js):
            log.warning("agent sidecar: %s is not built; using the in-process "
                        "loop", server_js)
            return
        if shutil.which("node") is None:
            log.warning("agent sidecar: no node runtime on PATH; using the "
                        "in-process loop")
            return

        self._port = Config.AGENT_SIDECAR_PORT
        if await self._attempt_spawn():
            log.info("agent sidecar: started on %s", self._url)
        else:
            log.warning("agent sidecar: did not become healthy on start; the "
                        "supervisor will keep retrying")
        self._supervisor_task = asyncio.create_task(self._supervise())

    async def _attempt_spawn(self) -> bool:
        """Spawn the sidecar process and wait for it to become healthy.

        Returns True when the process is up and serving. A process that spawns
        but never answers ``/health`` is terminated, so the supervisor records
        a clean failure instead of waiting forever on a hung process.
        """
        node = shutil.which("node")
        if node is None:
            return False

        # Retire any log pump still attached to a previous process.
        if self._log_task is not None:
            self._log_task.cancel()
            self._log_task = None

        env = dict(os.environ)
        env["AGENT_SIDECAR_HOST"] = "127.0.0.1"
        env["AGENT_SIDECAR_PORT"] = str(self._port)
        env["OPENROUTER_API_KEY"] = Config.OPENROUTER_API_KEY

        try:
            self._proc = await asyncio.create_subprocess_exec(
                node, _server_script(), env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            log.warning("agent sidecar: failed to spawn (%s)", exc)
            self._proc = None
            return False

        self._owns_process = True
        self._log_task = asyncio.create_task(self._pump_logs(self._proc))

        if await self._wait_healthy(self._port):
            self._url = f"ws://127.0.0.1:{self._port}/agent"
            self._last_healthy_at = time.monotonic()
            return True

        log.warning("agent sidecar: process did not become healthy")
        await self._terminate_proc()
        return False

    async def _wait_healthy(self, port: int) -> bool:
        """Poll the sidecar's /health endpoint until it answers or times out."""
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        url = f"http://127.0.0.1:{port}/health"
        async with aiohttp.ClientSession() as session:
            while time.monotonic() < deadline:
                if self._proc is not None and self._proc.returncode is not None:
                    return False  # the process exited already
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=2),
                    ) as resp:
                        if resp.status == 200:
                            return True
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    pass
                await asyncio.sleep(0.4)
        return False

    async def _supervise(self) -> None:
        """Keep the sidecar process alive: restart on crash with backoff.

        After _MAX_RESTART_ATTEMPTS consecutive failures the supervisor latches
        unhealthy and stops trying -- every turn then uses the in-process loop
        until the bot itself restarts. A process that survives _HEALTHY_RESET_S
        is treated as stable and clears the failure streak.
        """
        while not self._shutting_down:
            proc = self._proc
            if proc is not None and proc.returncode is None:
                await proc.wait()
            if self._shutting_down:
                break

            rc = self._proc.returncode if self._proc is not None else None
            now = time.monotonic()
            if (self._last_healthy_at
                    and now - self._last_healthy_at > _HEALTHY_RESET_S):
                self._consecutive_failures = 1
            else:
                self._consecutive_failures += 1
            self._url = ""  # no endpoint while down -- turns fall back

            if self._consecutive_failures > _MAX_RESTART_ATTEMPTS:
                self._unhealthy = True
                log.error(
                    "agent sidecar: %d consecutive failures (last exit code "
                    "%s); giving up. Every turn now uses the in-process loop "
                    "until the bot restarts.",
                    self._consecutive_failures, rc,
                )
                return

            backoff = _RESTART_BACKOFFS[
                min(self._consecutive_failures - 1, len(_RESTART_BACKOFFS) - 1)
            ]
            log.warning(
                "agent sidecar: process down (exit code %s); restart attempt "
                "%d/%d in %.0fs",
                rc, self._consecutive_failures, _MAX_RESTART_ATTEMPTS, backoff,
            )
            try:
                await asyncio.sleep(backoff)
            except asyncio.CancelledError:
                break
            if self._shutting_down:
                break

            if await self._attempt_spawn():
                log.info("agent sidecar: restarted on %s", self._url)
            # If the respawn failed, _proc points at a terminated process, so
            # the next loop skips the wait and counts another failure.

    async def _pump_logs(self, proc: asyncio.subprocess.Process) -> None:
        """Forward one sidecar process's stdout into the bot's log."""
        if proc.stdout is None:
            return
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "ignore").rstrip()
                if line:
                    log.info("agent-sidecar: %s", line)
        except Exception:  # noqa: BLE001
            pass

    async def _terminate_proc(self) -> None:
        """Terminate the current sidecar process if this bot started it."""
        proc = self._proc
        if proc is not None and self._owns_process and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
            except ProcessLookupError:
                pass

    async def stop(self) -> None:
        """Stop the supervisor and terminate the sidecar process."""
        self._shutting_down = True

        if self._supervisor_task is not None:
            self._supervisor_task.cancel()
            try:
                await self._supervisor_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._supervisor_task = None

        if self._log_task is not None:
            self._log_task.cancel()
            try:
                await self._log_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._log_task = None

        await self._terminate_proc()
        self._proc = None
        self._url = ""

    # ── observability ─────────────────────────────────────────────────────────
    def note_turn_path(self, path: str) -> None:
        """Record which loop served a turn and warn on sustained fallback.

        When the sidecar is enabled but turns keep landing on the in-process
        loop, the sidecar is silently degraded. This makes that loud: once a
        majority of recent turns have fallen back, it logs an error (debounced
        so it does not spam every turn).
        """
        if not Config.AGENT_SIDECAR_ENABLED:
            return
        now = time.monotonic()
        self._turn_log.append((now, path))
        cutoff = now - _FALLBACK_WINDOW_S
        while self._turn_log and self._turn_log[0][0] < cutoff:
            self._turn_log.popleft()

        total = len(self._turn_log)
        if total < _FALLBACK_MIN_SAMPLES:
            return
        fallbacks = sum(1 for _, p in self._turn_log if p != "sidecar")
        rate = fallbacks / total
        if (rate >= _FALLBACK_ALERT_RATE
                and now - self._last_fallback_alert
                >= _FALLBACK_ALERT_COOLDOWN_S):
            self._last_fallback_alert = now
            log.error(
                "agent sidecar: %d of the last %d turns (%.0f%%) used the "
                "in-process loop -- the sidecar is degraded%s",
                fallbacks, total, rate * 100.0,
                " (latched unhealthy)" if self._unhealthy else "",
            )

    # ── one chat turn ─────────────────────────────────────────────────────────
    async def run_stream(
        self,
        messages: list[dict],
        ctx,
        *,
        model: str | None = None,
        max_tokens: int = 600,
        temperature: float = 0.85,
        tools_override: list[dict] | None = None,
        turn_id: str = "",
    ):
        """Drive one tool-calling turn through the sidecar.

        Yields the agent event vocabulary (``delta``, ``tool_start``,
        ``tool_done``, ``reset``, ``sources``, ``done``, ``error``). Raises
        :class:`AgentSidecarUnavailable` only if the sidecar cannot be reached
        -- or fails the protocol handshake -- before the turn produces any
        output, so the caller can fall back to the in-process loop.
        """
        if not self.available:
            raise AgentSidecarUnavailable("sidecar endpoint is not configured")

        registry = getattr(ctx, "registry", None)
        if tools_override is not None:
            tool_schemas = tools_override
        elif registry is not None:
            tool_schemas = registry.as_openai_tools()
        else:
            tool_schemas = []

        start = {
            "type": "start",
            "protocol_version": _PROTOCOL_VERSION,
            "turn_id": turn_id,
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_output_tokens": max_tokens,
            "tools": tool_schemas,
            "max_steps": max(1, Config.AGENT_MAX_STEPS),
            "max_cost": max(0.0, Config.AGENT_MAX_COST),
        }

        session = aiohttp.ClientSession()
        try:
            connect_started = time.monotonic()
            try:
                ws = await asyncio.wait_for(
                    session.ws_connect(self._url, heartbeat=30.0),
                    timeout=_CONNECT_TIMEOUT_S,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                raise AgentSidecarUnavailable(f"connect failed: {exc}") from exc
            connect_ms = int((time.monotonic() - connect_started) * 1000)

            model_started = time.monotonic()
            await ws.send_json(start)

            produced = False
            finished = False
            handshake_done = False
            tool_names: list[str] = []
            step = 0
            model_ms = 0

            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    if msg.type in (
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
                    continue
                try:
                    event = json.loads(msg.data)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("type")

                if not handshake_done:
                    # The sidecar greets every connection with a hello frame.
                    if kind != "hello":
                        raise AgentSidecarUnavailable(
                            f"expected a hello frame, got {kind!r}")
                    peer = event.get("protocol_version")
                    if peer != _PROTOCOL_VERSION:
                        log.error(
                            "agent sidecar: protocol version mismatch -- bot "
                            "speaks v%s, sidecar speaks v%s; the two halves "
                            "are out of sync. Falling back to the in-process "
                            "loop.", _PROTOCOL_VERSION, peer,
                        )
                        raise AgentSidecarUnavailable(
                            f"protocol mismatch (bot v{_PROTOCOL_VERSION}, "
                            f"sidecar v{peer})")
                    handshake_done = True
                    log.info(
                        "agent sidecar: handshake ok for turn %s "
                        "(protocol v%s, sdk %s)",
                        turn_id or "-", _PROTOCOL_VERSION,
                        event.get("sdk_version") or "unknown",
                    )
                    continue

                if kind == "delta":
                    text = event.get("text")
                    if text:
                        produced = True
                        yield {"type": "delta", "text": text}

                elif kind == "tool_call":
                    produced = True
                    step += 1
                    tool_names.append(str(event.get("name") or ""))
                    async for out in self._run_tool(
                        ws, ctx, registry, event, step,
                    ):
                        yield out

                elif kind == "done":
                    finished = True
                    model_ms = int((time.monotonic() - model_started) * 1000)
                    meter = getattr(ctx, "meter", None)
                    if meter is not None:
                        meter.record_usage(
                            event.get("model") or model,
                            event.get("usage"), label="chat",
                        )
                    yield {
                        "type": "done",
                        "text": event.get("text") or "",
                        "finish_reason": event.get("finish_reason") or "",
                        "usage": event.get("usage") or {},
                        "tool_names": event.get("tool_names") or tool_names,
                    }
                    break

                elif kind == "error":
                    finished = True
                    produced = True
                    model_ms = int((time.monotonic() - model_started) * 1000)
                    yield {
                        "type": "error",
                        "error": str(event.get("error")
                                     or "agent sidecar error"),
                    }
                    break

            if not finished:
                if not produced:
                    raise AgentSidecarUnavailable(
                        "sidecar closed before responding")
                model_ms = int((time.monotonic() - model_started) * 1000)
                yield {"type": "error", "error": "agent sidecar closed early"}

            log_agent_turn(
                turn_id, "sidecar", connect_ms=connect_ms, model_ms=model_ms,
                tool_count=len(tool_names), model=model,
            )
        finally:
            await session.close()

    async def _run_tool(self, ws, ctx, registry, event: dict, step: int):
        """Execute one bridged tool call and stream its agent events."""
        name = str(event.get("name") or "")
        call_id = event.get("call_id")
        raw_args = event.get("arguments")
        args = raw_args if isinstance(raw_args, dict) else {}

        yield {"type": "reset"}
        yield {"type": "tool_start", "tool": name}

        started = time.monotonic()
        if registry is not None:
            result = await registry.run(name, args, ctx)
        else:
            result = {"error": "no tool registry available"}
        elapsed_ms = int((time.monotonic() - started) * 1000)

        spec = registry.get(name) if registry is not None else None
        piped = run_pipeline(
            name, result,
            meta={"round": step, "elapsed_ms": elapsed_ms},
            result_fields=spec.result_fields if spec else None,
        )
        data = piped.envelope.get("data")
        if (name == "data.web_search" and isinstance(data, dict)
                and data.get("results")):
            yield {"type": "sources", "results": data["results"]}
        yield {"type": "tool_done", "tool": name}

        try:
            payload = json.loads(piped.injected)
        except (json.JSONDecodeError, TypeError):
            payload = piped.injected
        await ws.send_json({
            "type": "tool_result",
            "call_id": call_id,
            "result": payload,
        })
