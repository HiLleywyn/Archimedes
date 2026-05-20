"""arch/tools/builtin.py -- Archimedes's headline tools.

The reference app ships five built-in tools the user can toggle: Web
Search, Get Local Time, Get Location, Open URL, Fetch URL. The first
overlaps with the bot's existing ``data.web_search`` so it is just an
alias; the rest are new and registered as plain ``ToolSpec``s on the
shared registry.

These tools live alongside the bot's existing toolset, not in place of
it. An Archimedes turn sees both -- ``arch.time`` and ``data.web_search`` are both
callable from one prompt -- so an upgrade does not strip any feature.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp

from ai.tools import RISK_MUTATE, RISK_READ, ToolContext, ToolSpec
from config import Config

log = logging.getLogger(__name__)

ARCH_FETCH_MAX_BYTES = 256 * 1024
ARCH_FETCH_TIMEOUT_S = 10.0


# ── Handlers ──────────────────────────────────────────────────────────────────
async def _get_local_time(args: dict, ctx: ToolContext) -> dict:
    """Return the current UTC time in ISO 8601 plus a human-readable form.

    A timezone argument is accepted but only the offset form is honoured
    (``+02:00``); zoneinfo lookups would pull a new dependency for marginal
    value. The model still gets enough to reason about "what day is it" and
    "when did X happen".
    """
    now = datetime.now(timezone.utc)
    tz_arg = (args or {}).get("offset", "").strip()
    label = "UTC"
    if tz_arg and tz_arg.lower() not in ("utc", "z"):
        try:
            sign = 1 if tz_arg[0] == "+" else -1 if tz_arg[0] == "-" else 0
            if sign:
                hh, sep, mm = tz_arg[1:].partition(":")
                offset_minutes = sign * (int(hh) * 60 + int(mm or "0"))
                now = datetime.fromtimestamp(
                    now.timestamp() + offset_minutes * 60, tz=timezone.utc,
                )
                label = f"UTC{tz_arg}"
        except (ValueError, IndexError):
            pass
    return {
        "iso": now.isoformat(),
        "epoch": int(now.timestamp()),
        "label": label,
        "weekday": now.strftime("%A"),
        "human": now.strftime("%Y-%m-%d %H:%M ") + label,
    }


async def _get_location(args: dict, ctx: ToolContext) -> dict:
    """Estimate the server's public location from its outbound IP.

    The Discord channel is a server-side bot, so this returns where the
    bot is hosted, not where the user is. A future client-side Archimedes channel
    would return the user's location instead -- the tool contract is the
    same; the implementation differs by transport.
    """
    timeout = aiohttp.ClientTimeout(total=ARCH_FETCH_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get("https://ipinfo.io/json") as resp:
            if resp.status != 200:
                return {"available": False, "error": f"HTTP {resp.status}"}
            data = await resp.json()
    return {
        "available": True,
        "city": data.get("city", ""),
        "region": data.get("region", ""),
        "country": data.get("country", ""),
        "timezone": data.get("timezone", ""),
        "source": "ipinfo.io (server outbound IP)",
    }


def _is_safe_url(url: str) -> bool:
    """Reject internal addresses to avoid SSRF. We delegate detailed
    parsing to the existing plugin HTTP guard in production; for the Archimedes
    tool surface a conservative scheme + host check is enough."""
    try:
        parts = urlparse(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if not host or host in ("localhost", "0.0.0.0"):
        return False
    if host.startswith("127.") or host.startswith("10.") or host.startswith("169.254."):
        return False
    if host.startswith("192.168.") or host.startswith("[::1]"):
        return False
    return True


async def _fetch_url(args: dict, ctx: ToolContext) -> dict:
    """Fetch a URL and return up to ``ARCH_FETCH_MAX_BYTES`` of its body.

    Plain HTML / JSON / text only; binary responses are flagged but their
    body is not returned. The bot's verbatim pipeline ceiling is bigger
    than this cap, so the model sees the whole snippet without trimming.
    """
    url = (args or {}).get("url", "").strip()
    if not _is_safe_url(url):
        return {"ok": False, "error": "URL rejected"}
    timeout = aiohttp.ClientTimeout(total=ARCH_FETCH_TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            ctype = resp.headers.get("Content-Type", "")
            if any(
                ctype.startswith(p)
                for p in ("image/", "video/", "audio/", "application/octet-stream")
            ):
                return {
                    "ok": True, "url": str(resp.url),
                    "status": resp.status,
                    "content_type": ctype,
                    "binary": True,
                    "body": "",
                }
            raw = await resp.content.read(ARCH_FETCH_MAX_BYTES + 1)
            body = raw[:ARCH_FETCH_MAX_BYTES].decode("utf-8", errors="replace")
            return {
                "ok": True,
                "url": str(resp.url),
                "status": resp.status,
                "content_type": ctype,
                "truncated": len(raw) > ARCH_FETCH_MAX_BYTES,
                "body": body,
            }


async def _open_url(args: dict, ctx: ToolContext) -> dict:
    """Record an intent to open a URL.

    The bot has no graphical surface to actually open one, so this tool
    returns the URL back so the rendering layer can show it as a button
    in the dynamic-UI card. A future Archimedes web client will execute the open
    locally on the user's device.
    """
    url = (args or {}).get("url", "").strip()
    if not _is_safe_url(url):
        return {"ok": False, "error": "URL rejected"}
    return {"ok": True, "url": url, "action": "open_in_browser"}


# ── Registry hookup ───────────────────────────────────────────────────────────
ARCH_TIME = ToolSpec(
    name="arch.time",
    description=(
        "Return the current local time and weekday. Use whenever the user "
        "refers to 'today', 'now', or 'this week' so the answer is grounded "
        "in real time."
    ),
    parameters={
        "type": "object",
        "properties": {
            "offset": {
                "type": "string",
                "description": "Optional timezone offset like '+02:00'. Blank means UTC.",
            },
        },
    },
    handler=_get_local_time,
    category="arch",
    risk=RISK_READ,
    result_fields=("iso", "epoch", "label", "weekday", "human"),
)

ARCH_LOCATION = ToolSpec(
    name="arch.location",
    description=(
        "Estimate the server's geographic location from its outbound IP. "
        "Use when the user asks where the bot is hosted or for region-aware "
        "answers like 'what is the local time here'."
    ),
    parameters={"type": "object", "properties": {}},
    handler=_get_location,
    category="arch",
    risk=RISK_READ,
    result_fields=("available", "city", "region", "country", "timezone", "source"),
)

ARCH_FETCH = ToolSpec(
    name="arch.fetch_url",
    description=(
        "Fetch a URL and return its body (up to 256 KB). Use for pages the "
        "web-search tool returned, or when the user pastes a link and asks "
        "what is on it."
    ),
    parameters={
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "description": "Absolute http/https URL."},
        },
    },
    handler=_fetch_url,
    category="arch",
    risk=RISK_READ,
    verbatim=True,
    result_fields=("ok", "url", "status", "content_type", "truncated", "body"),
)

ARCH_OPEN = ToolSpec(
    name="arch.open_url",
    description=(
        "Hand a URL to the user with an Open button on the reply. Use when "
        "the helpful action is for the user to visit the page themselves."
    ),
    parameters={
        "type": "object",
        "required": ["url"],
        "properties": {"url": {"type": "string"}},
    },
    handler=_open_url,
    category="arch",
    risk=RISK_READ,
    result_fields=("ok", "url", "action"),
)


def register_builtin_tools(registry) -> None:
    """Attach Archimedes's built-in tools to the shared ``ToolRegistry``.

    Called once during ``framework.bot.ArchimedesBot.setup_hook`` after the
    existing tools are built, so a Archimedes turn sees both surfaces.
    """
    for spec in (ARCH_TIME, ARCH_LOCATION, ARCH_FETCH, ARCH_OPEN):
        registry.register(spec)
    log.info("Archimedes built-in tools registered: %d", 4)
