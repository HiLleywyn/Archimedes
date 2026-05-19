"""ai/agent_sidecar.py -- the OpenRouter Agent SDK sidecar bridge.

The OpenRouter Agent SDK (``@openrouter/agent``) is a TypeScript package, so
its multi-step tool-calling loop runs in a small Node service: the sidecar in
``agent-sidecar/``. This module is the Python half of the bridge.

Two responsibilities live here:

  * :class:`AgentSidecar` -- supervises the Node process. It autostarts a
    local sidecar (or points at an external one), polls it healthy, and tears
    it down on shutdown.
  * :meth:`AgentSidecar.run_stream` -- drives one chat turn over a WebSocket.

The protocol is a compact JSON exchange (see ``agent-sidecar/src/server.ts``):
the bot sends a ``start`` frame, the sidecar streams ``delta`` text and
``tool_call`` requests, the bot runs each tool through the execution pipeline
and answers with ``tool_result``, and the turn ends with ``done`` or ``error``.

``run_stream`` yields the same event vocabulary as the in-process agent loop
in :mod:`ai.tools`, so the chat cog consumes either transparently. When the
sidecar cannot be reached it raises :class:`AgentSidecarUnavailable` before
yielding anything, so the caller can fall back to the in-process loop.
"""
from __future__ import annotations

import asyncio
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


class AgentSidecarUnavailable(RuntimeError):
    """Raised when the sidecar cannot be reached before a turn produces output.

    The caller catches this to fall back to the in-process agent loop. Once a
    turn has started streaming events the sidecar never raises it -- a failure
    after that point is reported as an ``error`` event instead.
    """


def _server_script() -> str:
    """Absolute path to the built sidecar entry point."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "agent-sidecar", "dist", "server.js")


class AgentSidecar:
    """Owns the Node agent sidecar process and the WebSocket bridge to it."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._log_task: asyncio.Task | None = None
        self._owns_process = False
        self._url = ""

    @property
    def available(self) -> bool:
        """True when a turn may be routed to the sidecar."""
        return bool(Config.AGENT_SIDECAR_ENABLED and self._url)

    @property
    def url(self) -> str:
        return self._url

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
        await self._spawn()

    async def _spawn(self) -> None:
        """Spawn and health-check the bundled Node sidecar."""
        server_js = _server_script()
        if not os.path.exists(server_js):
            log.warning("agent sidecar: %s is not built; using the in-process "
                        "loop", server_js)
            return
        node = shutil.which("node")
        if node is None:
            log.warning("agent sidecar: no node runtime on PATH; using the "
                        "in-process loop")
            return

        port = Config.AGENT_SIDECAR_PORT
        env = dict(os.environ)
        env["AGENT_SIDECAR_HOST"] = "127.0.0.1"
        env["AGENT_SIDECAR_PORT"] = str(port)
        env["OPENROUTER_API_KEY"] = Config.OPENROUTER_API_KEY

        try:
            self._proc = await asyncio.create_subprocess_exec(
                node, server_js, env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            log.warning("agent sidecar: failed to spawn (%s); using the "
                        "in-process loop", exc)
            self._proc = None
            return

        self._owns_process = True
        self._log_task = asyncio.create_task(self._pump_logs())

        if await self._wait_healthy(port):
            self._url = f"ws://127.0.0.1:{port}/agent"
            log.info("agent sidecar: started on %s", self._url)
        else:
            log.warning("agent sidecar: did not become healthy; using the "
                        "in-process loop")

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

    async def _pump_logs(self) -> None:
        """Forward the sidecar's stdout into the bot's log."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "ignore").rstrip()
                if line:
                    log.info("agent-sidecar: %s", line)
        except Exception:  # noqa: BLE001
            pass

    async def stop(self) -> None:
        """Terminate the sidecar process if this bot started it."""
        if self._log_task is not None:
            self._log_task.cancel()
            try:
                await self._log_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._log_task = None

        proc = self._proc
        if proc is not None and self._owns_process and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
            except ProcessLookupError:
                pass
        self._proc = None
        self._url = ""

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
    ):
        """Drive one tool-calling turn through the sidecar.

        Yields the agent event vocabulary (``delta``, ``tool_start``,
        ``tool_done``, ``reset``, ``sources``, ``done``, ``error``). Raises
        :class:`AgentSidecarUnavailable` only if the sidecar cannot be reached
        before the turn produces any output.
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
            try:
                ws = await asyncio.wait_for(
                    session.ws_connect(self._url, heartbeat=30.0),
                    timeout=_CONNECT_TIMEOUT_S,
                )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                raise AgentSidecarUnavailable(f"connect failed: {exc}") from exc

            await ws.send_json(start)

            produced = False
            finished = False
            tool_names: list[str] = []
            step = 0

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
                yield {"type": "error", "error": "agent sidecar closed early"}
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
