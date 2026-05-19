"""ai/tools.py -- agent tool registry, generic tools, and the orchestrator.

The standalone bot keeps the full tool-calling infrastructure but ships
only generic, non-financial tools:

  * ``data.web_search``       -- live web search (DuckDuckGo or Brave)
  * ``vision.describe_image`` -- describe an image attachment
  * ``memory.remember_fact``  -- store a durable fact about the user/server
  * ``memory.recall_facts``   -- read back stored facts
  * ``transform.slice``       -- deterministic top-N of a list
  * ``transform.project``     -- deterministic field selection on a list
  * ``transform.aggregate``   -- deterministic sum/min/max/mean/count
  * ``image.generate``        -- generate an image (OpenRouter)
  * ``video.generate``        -- generate a video (OpenRouter, asynchronous)

Tools are registered with the :class:`ToolRegistry`; Lua plugins can register
more through :class:`framework.plugins.manager.PluginManager`.

``run_agent_stream`` is the orchestrator: the control plane that routes the
model to tools and back. Every tool result it collects is run through the
:mod:`framework.pipeline` -- wrapped in the strict contract envelope,
validated by the Pydantic gate, deterministically compressed, and reduced to
minimal JSON -- before the model is allowed to see it.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import aiohttp
import discord

from config import Config
from ai.client import (
    complete, download_media, generate_image, poll_video, stream_completion,
    submit_video,
)
from ai.models import resolve_model
from ai.safety import sanitize_context_snippet
from framework.embed import card
from framework.pipeline import run_pipeline
from framework.pipeline.transforms import aggregate, project_fields, slice_items
from framework.ui import C_ERROR, C_PURPLE

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
    """A registered tool: schema plus its async handler.

    ``result_fields``, when set, is the tool's declared result schema: the
    top-level keys its ``data`` object is expected to carry. The processing
    pipeline filters the result down to those fields, so an unexpected key
    never drifts through to the model.
    """

    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, ToolContext], Awaitable[dict]]
    category: str = "misc"
    risk: str = RISK_READ
    result_fields: tuple[str, ...] | None = None

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

    def unregister(self, name: str) -> bool:
        """Drop a tool by name. Used when a Lua plugin is disabled."""
        self._disabled.discard(name)
        return self._tools.pop(name, None) is not None

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


# ── Deterministic transform tools ─────────────────────────────────────────────
# Pure computation: no model, no I/O. These handlers ignore ``ctx`` entirely
# -- their whole answer is a function of ``args``. They let the model offload
# slicing, field selection and arithmetic instead of doing it by eye.
async def _transform_slice(args: dict, ctx: ToolContext) -> dict:
    return slice_items(
        args.get("items"), args.get("n"),
        key=args.get("key"), order=str(args.get("order") or "desc"),
    )


async def _transform_project(args: dict, ctx: ToolContext) -> dict:
    return project_fields(args.get("items"), args.get("fields"))


async def _transform_aggregate(args: dict, ctx: ToolContext) -> dict:
    return aggregate(
        args.get("items"), field=args.get("field"),
        op=str(args.get("op") or "sum"),
    )


# ── Image and video generation tools ──────────────────────────────────────────
# Both run on OpenRouter. The model is the `image` / `video` category of the
# per-guild model picker, so `.ai model set image|video <slug>` retunes them.
# Image generation is synchronous; video generation is slow and asynchronous,
# so it is submitted here and a background task delivers it when it is ready.
_video_tasks: set[asyncio.Task] = set()
_VIDEO_POLL_INTERVAL = 15
_VIDEO_POLL_DEADLINE = 15 * 60  # stop polling a job after fifteen minutes


def _decode_data_url(url: str) -> bytes | None:
    """Decode a ``data:...;base64,...`` URL into bytes, or ``None``."""
    header, _, payload = url.partition(",")
    if not payload or "base64" not in header:
        return None
    try:
        return base64.b64decode(payload)
    except (ValueError, TypeError):
        return None


async def _notify(bot, channel_id: int, title: str, message: str,
                  color: int) -> None:
    """Post a small status embed into a channel, ignoring delivery failure."""
    channel = bot.get_channel(channel_id) if bot else None
    if channel is None:
        return
    try:
        await channel.send(
            embed=card(title, description=message[:1900], color=color).build())
    except discord.HTTPException:
        pass


async def _deliver_image(ctx: ToolContext, url: str, prompt: str,
                         model: str) -> bool:
    """Post a generated image into the conversation's channel."""
    channel = (ctx.bot.get_channel(ctx.channel_id)
               if ctx.bot and ctx.channel_id else None)
    if channel is None:
        return False
    builder = card("Generated image", description=prompt[:400], color=C_PURPLE)
    builder.footer(model)
    try:
        if url.startswith("data:"):
            raw = _decode_data_url(url)
            if raw is None:
                return False
            builder.image("attachment://image.png")
            await channel.send(
                embed=builder.build(),
                file=discord.File(io.BytesIO(raw), filename="image.png"))
        else:
            builder.image(url)
            await channel.send(embed=builder.build())
        return True
    except discord.HTTPException:
        return False


async def _deliver_video(bot, channel_id: int, url: str, prompt: str,
                         model: str) -> None:
    """Download a finished video and post it into the channel."""
    channel = bot.get_channel(channel_id) if bot else None
    if channel is None:
        return
    download = await download_media(url)
    if download.get("error"):
        await _notify(bot, channel_id, "Video generation finished",
                      "The video was generated but could not be downloaded: "
                      + str(download["error"]), C_ERROR)
        return
    builder = card("Generated video", description=prompt[:400], color=C_PURPLE)
    builder.footer(model)
    try:
        await channel.send(
            embed=builder.build(),
            file=discord.File(io.BytesIO(download["data"]), filename="video.mp4"))
    except discord.HTTPException as exc:
        await _notify(bot, channel_id, "Video generation finished",
                      f"The video was generated but could not be posted: {exc}",
                      C_ERROR)


async def _poll_video_job(bot, channel_id: int, polling_url: str,
                          prompt: str, model: str) -> None:
    """Poll a video job to completion, then deliver it to the channel."""
    deadline = time.monotonic() + _VIDEO_POLL_DEADLINE
    while time.monotonic() < deadline:
        await asyncio.sleep(_VIDEO_POLL_INTERVAL)
        status = await poll_video(polling_url)
        state = str(status.get("status") or "").lower()
        if state in ("completed", "succeeded"):
            urls = status.get("unsigned_urls") or status.get("urls") or []
            if isinstance(urls, str):
                urls = [urls]
            if urls:
                await _deliver_video(bot, channel_id, str(urls[0]), prompt, model)
            else:
                await _notify(bot, channel_id, "Video generation finished",
                              "The video completed but returned no file.",
                              C_ERROR)
            return
        if state in ("failed", "canceled", "cancelled"):
            await _notify(bot, channel_id, "Video generation failed",
                          str(status.get("error") or "the job did not succeed"),
                          C_ERROR)
            return
    await _notify(bot, channel_id, "Video generation timed out",
                  "The video did not finish within fifteen minutes.", C_ERROR)


async def _generate_image(args: dict, ctx: ToolContext) -> dict:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt is required"}
    pick = await resolve_model(ctx.db, ctx.guild_id, "image")
    result = await generate_image(prompt, model=pick.model)
    if result.get("error"):
        return {"error": result["error"]}
    delivered = await _deliver_image(ctx, result["image"], prompt, pick.model)
    return {
        "ok": True,
        "model": pick.model,
        "delivered": delivered,
        "note": ("The image has been posted into the channel as an embed."
                 if delivered else
                 "The image was generated but could not be posted."),
    }


async def _generate_video(args: dict, ctx: ToolContext) -> dict:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return {"error": "prompt is required"}
    if ctx.bot is None or not ctx.channel_id:
        return {"error": "video generation needs a channel to deliver to"}
    pick = await resolve_model(ctx.db, ctx.guild_id, "video")
    submission = await submit_video(prompt, model=pick.model)
    if submission.get("error"):
        return {"error": submission["error"]}
    task = asyncio.create_task(_poll_video_job(
        ctx.bot, int(ctx.channel_id), submission["polling_url"],
        prompt, pick.model))
    _video_tasks.add(task)
    task.add_done_callback(_video_tasks.discard)
    return {
        "ok": True,
        "model": pick.model,
        "status": "submitted",
        "note": "Video generation has started. It usually takes a few "
                "minutes; the finished video is posted into this channel "
                "automatically when ready. Tell the user it is on the way.",
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
        result_fields=("query", "results"),
    ))
    reg.register(ToolSpec(
        "vision.describe_image",
        "Describe what is in an image. Call this whenever a user message "
        "contains an [ATTACHMENT: <url>] marker.",
        {"type": "object", "properties": {
            "url": {"type": "string", "description": "The image URL."},
        }, "required": ["url"]},
        _describe_image, category="vision", risk=RISK_READ,
        result_fields=("description",),
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
        result_fields=("stored", "scope", "key"),
    ))
    reg.register(ToolSpec(
        "memory.recall_facts",
        "Read back every durable fact stored about the current user and server.",
        {"type": "object", "properties": {}},
        _recall_facts, category="memory", risk=RISK_READ,
        result_fields=("about_user", "about_server"),
    ))
    reg.register(ToolSpec(
        "transform.slice",
        "Return the top N items of a list you already have. Deterministic: "
        "use this instead of picking items by eye. Optionally sorts by an "
        "object field first.",
        {"type": "object", "properties": {
            "items": {"type": "array", "items": {},
                      "description": "The list to slice."},
            "n": {"type": "integer", "description": "How many items to keep."},
            "key": {"type": "string",
                    "description": "Optional object field to sort by first."},
            "order": {"type": "string", "enum": ["asc", "desc"],
                      "description": "Sort direction when key is set."},
        }, "required": ["items", "n"]},
        _transform_slice, category="transform", risk=RISK_READ,
        result_fields=("items", "returned", "total"),
    ))
    reg.register(ToolSpec(
        "transform.project",
        "Keep only the named fields on each object of a list, dropping every "
        "other field. Deterministic: use this to trim wide rows.",
        {"type": "object", "properties": {
            "items": {"type": "array", "items": {"type": "object"},
                      "description": "The list of objects."},
            "fields": {"type": "array", "items": {"type": "string"},
                       "description": "The field names to keep."},
        }, "required": ["items", "fields"]},
        _transform_project, category="transform", risk=RISK_READ,
        result_fields=("items", "returned", "fields"),
    ))
    reg.register(ToolSpec(
        "transform.aggregate",
        "Reduce a list of numbers to one metric. Deterministic: use this "
        "instead of doing arithmetic in your head.",
        {"type": "object", "properties": {
            "items": {"type": "array", "items": {},
                      "description": "The list to reduce."},
            "field": {"type": "string",
                      "description": "Optional object field to read the number from."},
            "op": {"type": "string",
                   "enum": ["sum", "min", "max", "mean", "count"],
                   "description": "The metric to compute."},
        }, "required": ["items", "op"]},
        _transform_aggregate, category="transform", risk=RISK_READ,
        result_fields=("op", "value", "count", "skipped"),
    ))
    reg.register(ToolSpec(
        "image.generate",
        "Generate an image from a text description and post it into the "
        "channel. Use when a user asks you to draw, paint, create, make or "
        "generate a picture or image.",
        {"type": "object", "properties": {
            "prompt": {"type": "string",
                       "description": "A detailed description of the image."},
        }, "required": ["prompt"]},
        _generate_image, category="media", risk=RISK_SAFE,
        result_fields=("ok", "model", "delivered", "note"),
    ))
    reg.register(ToolSpec(
        "video.generate",
        "Start generating a video from a text description. Video generation "
        "is slow: this returns immediately and the finished video is posted "
        "into the channel automatically when it is ready. Use when a user "
        "asks you to make or generate a video.",
        {"type": "object", "properties": {
            "prompt": {"type": "string",
                       "description": "A detailed description of the video."},
        }, "required": ["prompt"]},
        _generate_video, category="media", risk=RISK_SAFE,
        result_fields=("ok", "model", "status", "note"),
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
            started = time.monotonic()
            result = await registry.run(name, args, ctx)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            tool_names.append(name)

            # The result does not go to the model raw. The pipeline wraps it
            # in the contract envelope, runs it through the validation gate,
            # compresses it deterministically and reduces it to minimal JSON.
            spec = registry.get(name)
            piped = run_pipeline(
                name, result,
                meta={"round": _round + 1, "elapsed_ms": elapsed_ms},
                result_fields=spec.result_fields if spec else None,
            )
            data = piped.envelope.get("data")
            if (name == "data.web_search" and isinstance(data, dict)
                    and data.get("results")):
                yield {"type": "sources", "results": data["results"]}
            yield {"type": "tool_done", "tool": name}
            convo.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": piped.injected,
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
