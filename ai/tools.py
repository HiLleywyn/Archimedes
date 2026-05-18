"""ai/tools.py -- agent tool registry, generic tools, and the tool loop.

The standalone bot keeps the full tool-calling infrastructure but ships
only generic, non-financial tools:

  * ``data.web_search``       -- live web search (DuckDuckGo or Brave)
  * ``vision.describe_image`` -- describe an image attachment
  * ``memory.remember_fact``  -- store a durable fact about the user/server
  * ``memory.recall_facts``   -- read back stored facts

Tools are registered with the :class:`ToolRegistry`; Lua plugins in
``plugins/*.lua`` can register more (see ``plugins/README``).
``run_agent_stream`` is the loop that lets the model call them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp

from config import Config
from ai.client import stream_completion, complete
from ai.models import resolve_model
from ai.safety import sanitize_context_snippet

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 4

# Risk tiers. The agent loop never exposes ``danger`` tools.
RISK_READ = "read"
RISK_SAFE = "safe"
RISK_MUTATE = "mutate"
RISK_DANGER = "danger"


@dataclass
class ToolContext:
    """Everything a tool handler needs to run one call."""

    bot: object
    db: object
    user_id: int
    guild_id: int
    channel_id: int = 0
    memory: object | None = None
    registry: "ToolRegistry | None" = None


@dataclass
class ToolSpec:
    """A registered tool: schema plus its async handler."""

    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, ToolContext], Awaitable[dict]]
    category: str = "misc"
    risk: str = RISK_READ

    def as_openai_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    """Holds every tool and tracks which are enabled."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._disabled: set[str] = set()

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def tool(self, name: str, description: str, parameters: dict, *,
             category: str = "misc", risk: str = RISK_READ):
        """Decorator form of :meth:`register`."""

        def deco(handler):
            self.register(ToolSpec(name, description, parameters, handler,
                                   category=category, risk=risk))
            return handler

        return deco

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def by_category(self, category: str) -> list[ToolSpec]:
        return [t for t in self._tools.values() if t.category == category]

    def is_enabled(self, name: str) -> bool:
        return name not in self._disabled

    def set_enabled(self, name: str, enabled: bool) -> None:
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)

    def as_openai_tools(self, *, include_danger: bool = False) -> list[dict]:
        """OpenAI ``tools`` array for every enabled, non-danger tool."""
        out = []
        for spec in self._tools.values():
            if not self.is_enabled(spec.name):
                continue
            if spec.risk == RISK_DANGER and not include_danger:
                continue
            out.append(spec.as_openai_tool())
        return out

    async def run(self, name: str, args: dict, ctx: ToolContext) -> dict:
        """Execute a tool call, returning a JSON-serialisable result dict."""
        spec = self._tools.get(name)
        if spec is None:
            return {"error": f"unknown tool: {name}"}
        if not self.is_enabled(name):
            return {"error": f"tool {name} is disabled"}
        try:
            return await spec.handler(args or {}, ctx)
        except Exception as exc:  # noqa: BLE001
            log.warning("tool %s failed: %s", name, exc)
            return {"error": f"tool {name} raised {type(exc).__name__}"}


# ── Generic tool implementations ──────────────────────────────────────────────
async def _web_search(args: dict, ctx: ToolContext) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    backend = Config.SEARCH_BACKEND
    if backend == "brave" and Config.BRAVE_SEARCH_API_KEY:
        return await _brave_search(query)
    return await _ddg_search(query)


async def _brave_search(query: str) -> dict:
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {"X-Subscription-Token": Config.BRAVE_SEARCH_API_KEY,
               "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, params={"q": query, "count": 5},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return {"error": f"brave http {resp.status}"}
                data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return {"error": f"brave search failed: {exc}"}
    results = []
    for item in (data.get("web", {}).get("results") or [])[:5]:
        results.append({
            "title": item.get("title", ""),
            "snippet": sanitize_context_snippet(item.get("description", ""), 240),
            "url": item.get("url", ""),
        })
    return {"query": query, "results": results}


_DDG_RESULT_RE = re.compile(
    r'result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'result__snippet"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


async def _ddg_search(query: str) -> dict:
    url = "https://html.duckduckgo.com/html/"
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Archimedes/1.0)"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=headers, data={"q": query},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return {"error": f"ddg http {resp.status}"}
                html = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        return {"error": f"ddg search failed: {exc}"}
    results = []
    for m in _DDG_RESULT_RE.finditer(html):
        link, title, snippet = m.group(1), m.group(2), m.group(3)
        results.append({
            "title": _TAG_RE.sub("", title).strip(),
            "snippet": sanitize_context_snippet(_TAG_RE.sub("", snippet), 240),
            "url": link,
        })
        if len(results) >= 5:
            break
    return {"query": query, "results": results}


async def _describe_image(args: dict, ctx: ToolContext) -> dict:
    image_url = str(args.get("url") or "").strip()
    if not image_url:
        return {"error": "url is required"}
    pick = await resolve_model(ctx.db, ctx.guild_id, "vision")
    messages = [
        {"role": "system", "content": "Describe the image plainly and concisely."},
        {"role": "user", "content": [
            {"type": "text", "text": "What is in this image?"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]},
    ]
    text = await complete(messages, model=pick.model, max_tokens=300, timeout=45)
    if not text:
        return {"error": "vision model returned nothing"}
    return {"description": text}


async def _remember_fact(args: dict, ctx: ToolContext) -> dict:
    key = str(args.get("key") or "").strip()
    value = str(args.get("value") or "").strip()
    scope_kind = str(args.get("scope") or "user").strip().lower()
    if not key or not value:
        return {"error": "key and value are required"}
    if ctx.memory is None:
        return {"error": "memory service unavailable"}
    from ai.memory import guild_scope, user_scope

    scope = (guild_scope(ctx.guild_id) if scope_kind == "guild"
             else user_scope(ctx.user_id, ctx.guild_id))
    await ctx.memory.upsert_fact(scope, key, value, confidence=0.9, source="tool")
    return {"stored": True, "scope": scope_kind, "key": key}


async def _recall_facts(args: dict, ctx: ToolContext) -> dict:
    if ctx.memory is None:
        return {"error": "memory service unavailable"}
    from ai.memory import guild_scope, user_scope

    user_facts = await ctx.memory.get_facts(user_scope(ctx.user_id, ctx.guild_id), 8)
    guild_facts = await ctx.memory.get_facts(guild_scope(ctx.guild_id), 8)
    return {
        "about_user": [{"key": f.key, "value": f.value} for f in user_facts],
        "about_server": [{"key": f.key, "value": f.value} for f in guild_facts],
    }


def build_default_registry() -> ToolRegistry:
    """Create a registry pre-loaded with the generic tool set."""
    reg = ToolRegistry()
    reg.register(ToolSpec(
        "data.web_search",
        "Search the web for a current real-world fact you do not already know. "
        "Do NOT use for casual chat or anything answerable from training data.",
        {"type": "object", "properties": {
            "query": {"type": "string", "description": "The search query."},
        }, "required": ["query"]},
        _web_search, category="data", risk=RISK_READ,
    ))
    reg.register(ToolSpec(
        "vision.describe_image",
        "Describe what is in an image. Call this whenever a user message "
        "contains an [ATTACHMENT: <url>] marker.",
        {"type": "object", "properties": {
            "url": {"type": "string", "description": "The image URL."},
        }, "required": ["url"]},
        _describe_image, category="vision", risk=RISK_READ,
    ))
    reg.register(ToolSpec(
        "memory.remember_fact",
        "Store a durable fact about the current user or server so you can "
        "recall it in future conversations.",
        {"type": "object", "properties": {
            "key": {"type": "string", "description": "Short fact label."},
            "value": {"type": "string", "description": "The fact itself."},
            "scope": {"type": "string", "enum": ["user", "guild"],
                      "description": "Whether the fact is about the user or the server."},
        }, "required": ["key", "value"]},
        _remember_fact, category="memory", risk=RISK_MUTATE,
    ))
    reg.register(ToolSpec(
        "memory.recall_facts",
        "Read back every durable fact stored about the current user and server.",
        {"type": "object", "properties": {}},
        _recall_facts, category="memory", risk=RISK_READ,
    ))
    return reg


# ── Agent loop ────────────────────────────────────────────────────────────────
async def run_agent_stream(
    messages: list[dict],
    ctx: ToolContext,
    *,
    model: str | None = None,
    max_tokens: int = 600,
    temperature: float = 0.85,
    tools_override: list[dict] | None = None,
):
    """Stream a chat turn that may call tools.

    Yields the same event vocabulary as :func:`ai.client.stream_completion`
    plus ``{"type": "tool_start"|"tool_done", "tool": name}`` while a tool
    runs and ``{"type": "reset"}`` when a tool round clears the visible
    buffer. The terminal ``done`` event carries ``tool_names`` used.
    """
    registry = ctx.registry
    if registry is None:
        tool_schemas = tools_override or []
    elif tools_override is not None:
        tool_schemas = tools_override
    else:
        tool_schemas = registry.as_openai_tools()

    convo = list(messages)
    tool_names: list[str] = []

    for _round in range(MAX_TOOL_ROUNDS):
        done_event: dict | None = None
        async for ev in stream_completion(
            convo, model=model, max_tokens=max_tokens,
            temperature=temperature, tools=tool_schemas or None,
        ):
            kind = ev.get("type")
            if kind == "delta":
                yield ev
            elif kind == "error":
                yield ev
                return
            elif kind == "done":
                done_event = ev

        if done_event is None:
            yield {"type": "error", "error": "empty_response"}
            return

        calls = done_event.get("tool_calls") or []
        if not calls or registry is None:
            yield {
                "type": "done",
                "text": done_event.get("text", ""),
                "finish_reason": done_event.get("finish_reason", ""),
                "usage": done_event.get("usage", {}),
                "tool_names": tool_names,
            }
            return

        # The model wants tools. Drop whatever streamed so far -- the real
        # answer comes after the tool results land.
        yield {"type": "reset"}
        convo.append({
            "role": "assistant",
            "content": done_event.get("text") or None,
            "tool_calls": calls,
        })
        for tc in calls:
            name = tc.get("function", {}).get("name", "")
            raw_args = tc.get("function", {}).get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool_start", "tool": name}
            result = await registry.run(name, args, ctx)
            tool_names.append(name)
            if name == "data.web_search" and isinstance(result, dict) and result.get("results"):
                yield {"type": "sources", "results": result["results"]}
            yield {"type": "tool_done", "tool": name}
            convo.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": json.dumps(result, default=str)[:4000],
            })

    # Tool-round budget exhausted -- ask for a final plain answer.
    final = await complete(
        convo, model=model, max_tokens=max_tokens, temperature=temperature,
    )
    yield {
        "type": "done",
        "text": final or "",
        "finish_reason": "tool_limit",
        "usage": {},
        "tool_names": tool_names,
    }
