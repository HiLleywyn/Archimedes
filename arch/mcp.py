"""arch/mcp.py -- Model Context Protocol client.

A small, dependency-light MCP client. Archimedes ships with the same
servers the reference app lists -- Context7, CoinGecko, Fetch, DeepWiki --
and lets the operator add more at runtime via ``/mcp add``.

Two transports:

  * **HTTP**: streamable HTTP (the modern default). One ``tools/list``
    handshake on connect, one ``tools/call`` per invocation. Aiohttp.
  * **stdio**: the launched server speaks JSON-RPC over its stdin/stdout
    pipes (Anthropic's reference servers, ``npx`` packages, etc.). Each
    server runs as one subprocess for the lifetime of the bot.

Tool schemas reported by an MCP server are translated to the bot's
existing ``ToolSpec`` (``ai.tools``) so the agent loop, sidecar, and
result pipeline pick them up like any other tool. Calls land back through
the transport at invocation time.

Servers declared in ``ARCHIMEDES_MCP_SERVERS`` are loaded at boot. Servers added
through ``MCPRegistry.add`` are persisted in ``archimedes_mcp_servers`` and
re-connected on next boot.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from arch.config import MCPServerSpec

log = logging.getLogger(__name__)

# Bound the time we will wait for any single MCP RPC. A misbehaving server
# must never hang the agent loop.
RPC_TIMEOUT_S = 20.0


@dataclass
class MCPTool:
    name: str               # prefixed with the server name: "context7.search"
    description: str
    parameters: dict
    server: str             # plain server name, no prefix


@dataclass
class _Transport:
    """Per-server transport state. Kept separate so the registry can pull
    one server's tools without touching another's connection."""

    spec: MCPServerSpec
    tools: list[MCPTool] = field(default_factory=list)
    connected: bool = False
    last_error: str = ""

    async def connect(self) -> None:
        raise NotImplementedError

    async def list_tools(self) -> list[MCPTool]:
        raise NotImplementedError

    async def call_tool(self, name: str, args: dict) -> dict:
        raise NotImplementedError

    async def close(self) -> None:  # noqa: B027
        pass


# ── HTTP transport ────────────────────────────────────────────────────────────
class _HTTPTransport(_Transport):

    def __init__(self, spec: MCPServerSpec) -> None:
        super().__init__(spec=spec)
        self._session = None
        self._next_id = 1

    async def connect(self) -> None:
        import aiohttp  # noqa: WPS433
        self._session = aiohttp.ClientSession()
        # The server's tools list doubles as a liveness probe -- if it fails
        # the registry drops this transport and the operator sees a red dot.
        try:
            self.tools = await self.list_tools()
            self.connected = True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            raise

    async def _rpc(self, method: str, params: dict) -> Any:
        if self._session is None:
            raise RuntimeError("transport not connected")
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        self._next_id += 1
        async with self._session.post(
            self.spec.url, json=body,
            timeout=RPC_TIMEOUT_S,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if "error" in data and data["error"]:
            raise RuntimeError(str(data["error"]))
        return data.get("result", {})

    async def list_tools(self) -> list[MCPTool]:
        result = await self._rpc("tools/list", {})
        tools: list[MCPTool] = []
        for entry in result.get("tools", []) or []:
            name = entry.get("name", "")
            if not name:
                continue
            tools.append(MCPTool(
                name=f"{self.spec.name}.{name}",
                description=entry.get("description", ""),
                parameters=entry.get("inputSchema", {"type": "object"}),
                server=self.spec.name,
            ))
        return tools

    async def call_tool(self, name: str, args: dict) -> dict:
        bare = name.split(".", 1)[1] if "." in name else name
        return await self._rpc("tools/call", {
            "name": bare, "arguments": args or {},
        })

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


# ── stdio transport ───────────────────────────────────────────────────────────
class _StdioTransport(_Transport):

    def __init__(self, spec: MCPServerSpec) -> None:
        super().__init__(spec=spec)
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 1
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.spec.command, *self.spec.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Initialise handshake (MCP requires it before tools/list).
            await self._rpc("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "archimedes", "version": "3.0.0"},
            })
            self.tools = await self.list_tools()
            self.connected = True
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            raise

    async def _rpc(self, method: str, params: dict) -> Any:
        if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
            raise RuntimeError("stdio transport not connected")
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        self._next_id += 1
        line = (json.dumps(body) + "\n").encode("utf-8")
        async with self._lock:
            self._proc.stdin.write(line)
            await self._proc.stdin.drain()
            try:
                raw = await asyncio.wait_for(
                    self._proc.stdout.readline(), timeout=RPC_TIMEOUT_S,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(f"MCP {method} timed out") from exc
        if not raw:
            raise RuntimeError("MCP server closed the pipe")
        data = json.loads(raw.decode("utf-8"))
        if data.get("error"):
            raise RuntimeError(str(data["error"]))
        return data.get("result", {})

    async def list_tools(self) -> list[MCPTool]:
        result = await self._rpc("tools/list", {})
        tools: list[MCPTool] = []
        for entry in result.get("tools", []) or []:
            name = entry.get("name", "")
            if not name:
                continue
            tools.append(MCPTool(
                name=f"{self.spec.name}.{name}",
                description=entry.get("description", ""),
                parameters=entry.get("inputSchema", {"type": "object"}),
                server=self.spec.name,
            ))
        return tools

    async def call_tool(self, name: str, args: dict) -> dict:
        bare = name.split(".", 1)[1] if "." in name else name
        return await self._rpc("tools/call", {
            "name": bare, "arguments": args or {},
        })

    async def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=2.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass


# ── Registry ──────────────────────────────────────────────────────────────────
def make_transport(spec: MCPServerSpec) -> _Transport:
    if spec.transport == "http":
        return _HTTPTransport(spec)
    if spec.transport == "stdio":
        return _StdioTransport(spec)
    raise ValueError(f"unknown MCP transport: {spec.transport!r}")


class MCPRegistry:
    """Owns every connected MCP server and exposes their tools to the agent."""

    def __init__(self, db=None) -> None:
        self.db = db
        self._transports: dict[str, _Transport] = {}

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def connect_all(self, specs: list[MCPServerSpec]) -> None:
        for spec in specs:
            await self.connect_one(spec)

    async def connect_one(self, spec: MCPServerSpec) -> _Transport:
        if spec.name in self._transports:
            await self._transports[spec.name].close()
        transport = make_transport(spec)
        try:
            await transport.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("MCP: %s failed to connect: %s", spec.name, exc)
        self._transports[spec.name] = transport
        return transport

    async def disconnect(self, name: str) -> bool:
        t = self._transports.pop(name, None)
        if t is None:
            return False
        await t.close()
        return True

    async def close_all(self) -> None:
        for t in list(self._transports.values()):
            await t.close()
        self._transports.clear()

    # ── inspection ───────────────────────────────────────────────────────────
    def servers(self) -> list[_Transport]:
        return list(self._transports.values())

    def tools(self) -> list[MCPTool]:
        out: list[MCPTool] = []
        for t in self._transports.values():
            if t.connected:
                out.extend(t.tools)
        return out

    def find_tool(self, name: str) -> tuple[_Transport, MCPTool] | None:
        for t in self._transports.values():
            for tool in t.tools:
                if tool.name == name:
                    return t, tool
        return None

    async def call(self, name: str, args: dict) -> dict:
        found = self.find_tool(name)
        if found is None:
            raise ValueError(f"MCP tool not found: {name!r}")
        transport, _tool = found
        return await transport.call_tool(name, args)

    # ── persistence ──────────────────────────────────────────────────────────
    async def load_from_db(self) -> list[MCPServerSpec]:
        if self.db is None:
            return []
        rows = await self.db.fetch_all(
            "SELECT name, transport, url, command, args FROM archimedes_mcp_servers "
            "WHERE enabled = TRUE",
        )
        out: list[MCPServerSpec] = []
        for r in rows:
            args = r["args"]
            if isinstance(args, str):
                try:
                    args_list = json.loads(args) or []
                except json.JSONDecodeError:
                    args_list = []
            else:
                args_list = list(args or [])
            out.append(MCPServerSpec(
                name=r["name"], transport=r["transport"],
                url=r["url"] or "", command=r["command"] or "",
                args=tuple(args_list),
            ))
        return out

    async def save(self, spec: MCPServerSpec) -> None:
        if self.db is None:
            return
        await self.db.execute(
            "INSERT INTO archimedes_mcp_servers (name, transport, url, command, args) "
            "VALUES ($1, $2, $3, $4, $5::jsonb) "
            "ON CONFLICT (name) DO UPDATE SET "
            "transport=EXCLUDED.transport, url=EXCLUDED.url, "
            "command=EXCLUDED.command, args=EXCLUDED.args, enabled=TRUE",
            spec.name, spec.transport, spec.url, spec.command,
            json.dumps(list(spec.args)),
        )

    async def forget(self, name: str) -> None:
        if self.db is None:
            return
        await self.db.execute(
            "DELETE FROM archimedes_mcp_servers WHERE name = $1", name,
        )
