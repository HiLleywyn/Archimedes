"""tests/test_arch_mcp.py -- MCP client surface.

Only the no-network parts: spec parsing, registry bookkeeping, transport
factory. Real RPC roundtrips need a live MCP server and live elsewhere.
"""
from __future__ import annotations

import pytest

from arch.config import MCPServerSpec, _parse_mcp
from arch.mcp import (
    MCPRegistry, MCPTool, _HTTPTransport, _StdioTransport, make_transport,
)


# ── spec parsing ─────────────────────────────────────────────────────────────
def test_parse_mcp_http_entry() -> None:
    out = _parse_mcp("context7=https://mcp.context7.com/mcp")
    assert len(out) == 1
    assert out[0].name == "context7"
    assert out[0].transport == "http"
    assert out[0].url == "https://mcp.context7.com/mcp"
    assert out[0].command == ""
    assert out[0].args == ()


def test_parse_mcp_stdio_entry() -> None:
    out = _parse_mcp("local=stdio:my-tool --flag value")
    assert out[0].transport == "stdio"
    assert out[0].command == "my-tool"
    assert out[0].args == ("--flag", "value")


def test_parse_mcp_multiple_comma_separated() -> None:
    out = _parse_mcp(
        "ctx=https://a.example/mcp,local=stdio:tool"
    )
    assert [s.name for s in out] == ["ctx", "local"]


def test_parse_mcp_rejects_unknown_scheme_silently() -> None:
    out = _parse_mcp("weird=ftp://example.com")
    assert out == ()


def test_parse_mcp_empty_string_returns_empty_tuple() -> None:
    assert _parse_mcp("") == ()


# ── transport factory ────────────────────────────────────────────────────────
def test_make_transport_returns_http_for_http_spec() -> None:
    spec = MCPServerSpec(name="x", transport="http",
                         url="https://a.example/mcp", command="", args=())
    t = make_transport(spec)
    assert isinstance(t, _HTTPTransport)


def test_make_transport_returns_stdio_for_stdio_spec() -> None:
    spec = MCPServerSpec(name="x", transport="stdio", url="",
                         command="echo", args=("hi",))
    t = make_transport(spec)
    assert isinstance(t, _StdioTransport)


def test_make_transport_rejects_unknown_transport() -> None:
    spec = MCPServerSpec(name="x", transport="websocket", url="",
                         command="", args=())
    with pytest.raises(ValueError):
        make_transport(spec)


# ── registry ─────────────────────────────────────────────────────────────────
def test_registry_starts_empty() -> None:
    reg = MCPRegistry(db=None)
    assert reg.servers() == []
    assert reg.tools() == []
    assert reg.find_tool("anything") is None


def test_registry_find_tool_matches_qualified_name() -> None:
    reg = MCPRegistry(db=None)
    # Hand-place a transport with one fake tool so we exercise lookup
    # without a real connect.
    spec = MCPServerSpec(name="ctx", transport="http",
                         url="https://example.com/mcp", command="", args=())
    fake = _HTTPTransport(spec)
    fake.connected = True
    fake.tools = [MCPTool(name="ctx.search", description="",
                          parameters={}, server="ctx")]
    reg._transports["ctx"] = fake
    assert reg.find_tool("ctx.search") is not None
    assert reg.find_tool("ctx.missing") is None
    assert [t.name for t in reg.tools()] == ["ctx.search"]
