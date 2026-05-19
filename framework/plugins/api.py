"""framework/plugins/api.py -- the runtime surface handed to Lua plugins.

Every plugin handler runs in a worker thread (so a slow plugin never blocks
the gateway) and reaches Python through two values:

* ``arch``  -- a per-plugin global: the document ``store``, colour palette,
  time helpers, ``arch.dm`` and ``arch.user_name``.
* ``ctx``   -- the per-call table a command handler receives: the invoking
  user, the raw ``args`` string, mentions, and reply / deliver / confirm
  helpers.

Async work (database, Discord I/O) is bridged back onto the bot's event loop
with :func:`asyncio.run_coroutine_threadsafe`, so Lua code stays comfortably
synchronous while the loop keeps serving everyone else.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re

import discord

from ai.tools import RISK_SAFE, ToolSpec
from config import Config
from framework.embed import card
from framework.plugins import net, util
from framework.plugins.runtime import lua_to_py, py_to_lua
from framework.ui import (
    C_AMBER, C_BLURPLE, C_ERROR, C_GOLD, C_INFO, C_NAVY, C_NEUTRAL, C_PINK,
    C_PURPLE, C_SUCCESS, C_TEAL, C_WARNING, Paginator, clip,
)

log = logging.getLogger(__name__)

# Result blocked longer than this is treated as a hung plugin call.
_BRIDGE_TIMEOUT_S = 300

COLORS: dict[str, int] = {
    "success": C_SUCCESS, "error": C_ERROR, "warning": C_WARNING,
    "info": C_INFO, "gold": C_GOLD, "purple": C_PURPLE, "teal": C_TEAL,
    "navy": C_NAVY, "blurple": C_BLURPLE, "neutral": C_NEUTRAL,
    "pink": C_PINK, "amber": C_AMBER,
}

_REL_RE = re.compile(
    r"^in\s+(\d+)\s*"
    r"(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|"
    r"d|day|days|w|week|weeks)$",
    re.IGNORECASE,
)
# A leading scope token: ``#<id>`` or ``~<list>``. The token may be the whole
# argument (``note list #5``) or be followed by more text (``note add #5 hi``).
_SIGIL_RE = re.compile(r"^\s*(#\d+|~\S+)(?:\s+|$)")


# ── pure helpers (also reused by the offline test suite) ──────────────────────
def parse_time_to_epoch(text: str) -> int | None:
    """Parse a time string into a UTC epoch, or ``None``.

    Accepts relative offsets (``in 30m``, ``in 2h``, ``in 3d``, ``in 1w``) and
    absolute ``YYYY-MM-DD`` / ``YYYY-MM-DD HH:MM`` (treated as UTC).
    """
    text = (text or "").strip()
    if not text:
        return None
    rel = _REL_RE.match(text)
    if rel:
        amount = int(rel.group(1))
        unit = rel.group(2).lower()
        now = dt.datetime.now(dt.timezone.utc)
        if unit.startswith("m"):
            target = now + dt.timedelta(minutes=amount)
        elif unit.startswith("h"):
            target = now + dt.timedelta(hours=amount)
        elif unit.startswith("w"):
            target = now + dt.timedelta(weeks=amount)
        else:
            target = now + dt.timedelta(days=amount)
        return int(target.timestamp())
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int(parsed.replace(tzinfo=dt.timezone.utc).timestamp())
    return None


def split_sigils(text: str) -> tuple[str | None, str | None, str]:
    """Peel leading ``#<id>`` / ``~<list>`` scope tokens off ``text``.

    Returns ``(group_id, list_name, remaining_text)``; the group id is a
    string of digits so it survives the trip into Lua intact.
    """
    group_id: str | None = None
    list_name: str | None = None
    text = text or ""
    while True:
        match = _SIGIL_RE.match(text)
        if not match:
            break
        token = match.group(1)
        if token.startswith("#") and group_id is None:
            group_id = token[1:]
        elif token.startswith("~") and list_name is None:
            list_name = token[1:].lower()
        else:
            break
        text = text[match.end():]
    return group_id, list_name, text.strip()


def card_to_embed(spec) -> discord.Embed:
    """Build a :class:`discord.Embed` from a plugin's card table."""
    if not isinstance(spec, dict):
        spec = {"description": str(spec)}
    color = spec.get("color")
    if isinstance(color, str):
        color = COLORS.get(color.lower(), C_INFO)
    elif isinstance(color, (int, float)):
        color = int(color)
    else:
        color = C_INFO
    builder = card(
        str(spec.get("title") or "")[:256],
        description=str(spec["description"]) if spec.get("description") else None,
        color=color,
    )
    for fld in spec.get("fields") or []:
        if not isinstance(fld, dict):
            continue
        builder.field(
            str(fld.get("name") or "​")[:256],
            str(fld.get("value") or "​")[:1024],
            bool(fld.get("inline")),
        )
    if spec.get("footer"):
        builder.footer(str(spec["footer"])[:2048])
    # An http(s) URL keys the title link, the main image and the thumbnail.
    # Anything else is ignored so a plugin cannot point an embed at a
    # non-web target.
    for key, apply in (
        ("url", builder.url),
        ("image", builder.image),
        ("thumbnail", builder.thumbnail),
    ):
        value = spec.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            apply(value)
    return builder.build()


# ── the per-plugin API ────────────────────────────────────────────────────────
class LuaApi:
    """Wires one compiled plugin to the database, the bot and the event loop."""

    def __init__(
        self, plugin, *, db, bot, loop: asyncio.AbstractEventLoop,
        manager=None,
    ) -> None:
        self._plugin = plugin
        self._runtime = plugin.runtime
        self._db = db
        self._bot = bot
        self._loop = loop
        self._manager = manager
        self._namespace = plugin.manifest.storage
        self._lock = asyncio.Lock()
        self._store_table = None
        self._kv_table = None

    # ── activation: install the `arch` global ────────────────────────────────
    def activate(self) -> None:
        """Build the ``arch`` global and bind it in the plugin's runtime."""
        store = self._build_store()
        self._store_table = store
        kv = self._build_kv()
        self._kv_table = kv
        arch = {
            "store": store,
            "kv": kv,
            "config": Config.plugin_config(self._plugin.manifest.id),
            "colors": dict(COLORS),
            "now": lambda: int(dt.datetime.now(dt.timezone.utc).timestamp()),
            "parse_time": lambda text: parse_time_to_epoch(_s(text)),
            "fmt_time": _fmt_time,
            "clip": lambda text, limit: clip(_s(text), int(limit or 0)),
            "sigils": self._lua_sigils,
            "dm": self._dm,
            "user_name": self._user_name,
            "log": lambda msg: log.info("[plugin:%s] %s",
                                        self._plugin.manifest.id, _s(msg)),
            "json": {"encode": self._json_encode, "decode": self._json_decode},
            "base64": {"encode": util.b64_encode, "decode": util.b64_decode},
            "hash": util.hash_text,
            "uuid": util.make_uuid,
            "random": util.rand,
            "http": self._build_http(),
            "discord": self._build_discord(),
            "emit": self._emit,
        }
        self._runtime.globals()["arch"] = py_to_lua(self._runtime, arch)

    # ── arch.json ─────────────────────────────────────────────────────────────
    def _json_encode(self, value):
        return util.json_encode(lua_to_py(value))

    def _json_decode(self, text):
        return py_to_lua(self._runtime, util.json_decode(_s(text)))

    def _build_store(self):
        ns = self._namespace

        def put(collection, doc):
            rid = self._bridge(self._db.plugin_store_put(
                ns, _s(collection), lua_to_py(doc) or {}))
            return str(rid)

        def get(collection, record_id):
            rec = self._bridge(self._db.plugin_store_get(
                ns, _s(collection), _int(record_id)))
            return py_to_lua(self._runtime, _stringify_id(rec)) if rec else None

        def update(collection, record_id, doc):
            return self._bridge(self._db.plugin_store_update(
                ns, _s(collection), _int(record_id), lua_to_py(doc) or {}))

        def delete(collection, record_id):
            return self._bridge(self._db.plugin_store_delete(
                ns, _s(collection), _int(record_id)))

        def query(collection, match=None):
            rows = self._bridge(self._db.plugin_store_query(
                ns, _s(collection), lua_to_py(match) if match is not None else None))
            return py_to_lua(self._runtime, [_stringify_id(r) for r in rows])

        def all_(collection):
            rows = self._bridge(self._db.plugin_store_query(ns, _s(collection), None))
            return py_to_lua(self._runtime, [_stringify_id(r) for r in rows])

        return py_to_lua(self._runtime, {
            "put": put, "get": get, "update": update,
            "delete": delete, "query": query, "all": all_,
        })

    # ── arch.kv: the namespaced key/value store ───────────────────────────────
    def _build_kv(self):
        ns = self._namespace

        def get(key):
            value = self._bridge(self._db.plugin_kv_get(ns, _s(key)))
            return py_to_lua(self._runtime, value)

        def set_(key, value):
            self._bridge(self._db.plugin_kv_set(ns, _s(key), lua_to_py(value)))
            return True

        def delete(key):
            return self._bridge(self._db.plugin_kv_delete(ns, _s(key)))

        def keys():
            return py_to_lua(
                self._runtime, self._bridge(self._db.plugin_kv_keys(ns)))

        def clear():
            return self._bridge(self._db.plugin_kv_clear(ns))

        return py_to_lua(self._runtime, {
            "get": get, "set": set_, "delete": delete,
            "keys": keys, "clear": clear,
        })

    # ── arch.http: the SSRF-guarded outbound HTTP client ──────────────────────
    def _build_http(self):
        def get(url, opts=None):
            return self._http_call("GET", _s(url), opts)

        def post(url, opts=None):
            return self._http_call("POST", _s(url), opts)

        def request(method, url, opts=None):
            return self._http_call(_s(method), _s(url), opts)

        return py_to_lua(self._runtime, {
            "get": get, "post": post, "request": request,
        })

    def _http_call(self, method: str, url: str, opts):
        """Run one plugin HTTP request. Marshals Lua args in this worker."""
        if not Config.PLUGIN_HTTP_ENABLED:
            return py_to_lua(self._runtime, {
                "ok": False, "status": 0, "body": "", "json": None,
                "headers": {}, "error": "plugin HTTP is disabled",
            })
        parsed = lua_to_py(opts) if opts is not None else {}
        if not isinstance(parsed, dict):
            parsed = {}
        raw_headers = parsed.get("headers")
        headers = raw_headers if isinstance(raw_headers, dict) else {}
        result = self._bridge(net.fetch(
            method, url,
            headers=headers,
            body=parsed.get("body"),
            json_body=parsed.get("json"),
            timeout=_clamp(parsed.get("timeout"), Config.PLUGIN_HTTP_TIMEOUT_S),
            max_bytes=int(_clamp(parsed.get("max_bytes"),
                                 Config.PLUGIN_HTTP_MAX_BYTES)),
            max_redirects=int(_clamp(parsed.get("max_redirects"),
                                     Config.PLUGIN_HTTP_MAX_REDIRECTS)),
        ))
        return py_to_lua(self._runtime, result)

    # ── arch.discord: bridged Discord read/write helpers ──────────────────────
    def _build_discord(self):
        return py_to_lua(self._runtime, {
            "send": self._discord_send,
            "react": self._discord_react,
            "history": self._discord_history,
            "channel": self._discord_channel,
            "guild": self._discord_guild,
            "member": self._discord_member,
            "roles": self._discord_roles,
        })

    def _discord_send(self, channel_id, spec) -> bool:
        embed = card_to_embed(lua_to_py(spec))
        return self._bridge(self._do_discord_send(channel_id, embed))

    async def _do_discord_send(self, channel_id, embed: discord.Embed) -> bool:
        channel = await self._resolve_channel(channel_id)
        if channel is None:
            return False
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            return False
        return True

    def _discord_react(self, channel_id, message_id, emoji) -> bool:
        return self._bridge(
            self._do_discord_react(channel_id, message_id, _s(emoji)))

    async def _do_discord_react(self, channel_id, message_id, emoji) -> bool:
        channel = await self._resolve_channel(channel_id)
        if channel is None:
            return False
        try:
            message = await channel.fetch_message(int(message_id))
            await message.add_reaction(emoji)
        except (discord.HTTPException, TypeError, ValueError):
            return False
        return True

    def _discord_history(self, channel_id, limit=None):
        rows = self._bridge(self._do_discord_history(channel_id, limit))
        return py_to_lua(self._runtime, rows)

    async def _do_discord_history(self, channel_id, limit) -> list:
        channel = await self._resolve_channel(channel_id)
        if channel is None or not hasattr(channel, "history"):
            return []
        try:
            count = int(limit)
        except (TypeError, ValueError):
            count = 20
        count = max(1, min(count, 100))
        out: list[dict] = []
        try:
            async for message in channel.history(limit=count):
                out.append({
                    "id": str(message.id),
                    "author_id": str(message.author.id),
                    "author_name": message.author.display_name,
                    "content": message.content or "",
                    "created_at": int(message.created_at.timestamp()),
                    "bot": bool(message.author.bot),
                })
        except discord.HTTPException:
            return []
        return out

    def _discord_channel(self, channel_id):
        info = self._bridge(self._do_discord_channel(channel_id))
        return py_to_lua(self._runtime, info) if info else None

    async def _do_discord_channel(self, channel_id):
        channel = await self._resolve_channel(channel_id)
        if channel is None:
            return None
        guild = getattr(channel, "guild", None)
        return {
            "id": str(channel.id),
            "name": getattr(channel, "name", "") or "",
            "type": str(getattr(channel, "type", "")),
            "guild_id": str(guild.id) if guild else "0",
        }

    def _discord_guild(self, guild_id):
        info = self._bridge(self._do_discord_guild(guild_id))
        return py_to_lua(self._runtime, info) if info else None

    async def _do_discord_guild(self, guild_id):
        try:
            gid = int(guild_id)
        except (TypeError, ValueError):
            return None
        guild = self._bot.get_guild(gid)
        if guild is None:
            return None
        return {
            "id": str(guild.id),
            "name": guild.name,
            "member_count": guild.member_count or 0,
            "owner_id": str(guild.owner_id) if guild.owner_id else "0",
        }

    def _discord_member(self, guild_id, user_id):
        info = self._bridge(self._do_discord_member(guild_id, user_id))
        return py_to_lua(self._runtime, info) if info else None

    async def _do_discord_member(self, guild_id, user_id):
        try:
            gid, uid = int(guild_id), int(user_id)
        except (TypeError, ValueError):
            return None
        guild = self._bot.get_guild(gid)
        if guild is None:
            return None
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except discord.HTTPException:
                return None
        return {
            "id": str(member.id),
            "name": member.display_name,
            "roles": [str(role.id) for role in member.roles],
            "joined_at": (int(member.joined_at.timestamp())
                          if member.joined_at else None),
            "bot": bool(member.bot),
        }

    def _discord_roles(self, guild_id):
        rows = self._bridge(self._do_discord_roles(guild_id))
        return py_to_lua(self._runtime, rows)

    async def _do_discord_roles(self, guild_id) -> list:
        try:
            gid = int(guild_id)
        except (TypeError, ValueError):
            return []
        guild = self._bot.get_guild(gid)
        if guild is None:
            return []
        return [{
            "id": str(role.id),
            "name": role.name,
            "color": role.color.value,
            "position": role.position,
        } for role in guild.roles]

    async def _resolve_channel(self, channel_id):
        """Look up a channel by id, fetching it from the API if uncached."""
        try:
            cid = int(channel_id)
        except (TypeError, ValueError):
            return None
        channel = self._bot.get_channel(cid)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(cid)
            except discord.HTTPException:
                return None
        return channel

    # ── arch.emit: the cross-plugin event bus ─────────────────────────────────
    def _emit(self, event_name, payload=None) -> int:
        name = _s(event_name)
        data = lua_to_py(payload) if payload is not None else {}
        return self._bridge(self._do_emit(name, data))

    async def _do_emit(self, name: str, data) -> int:
        if self._manager is None:
            return 0
        return await self._manager.emit(
            name, data, source=self._plugin.manifest.id)

    # ── shared worker-thread entry point ──────────────────────────────────────
    async def run_in_worker(self, fn, *args):
        """Run a callable on a worker thread under this plugin's lock.

        Used for event handlers and the on_load / on_unload lifecycle hooks so
        every Lua entry point serialises against the one runtime uniformly.
        """
        async with self._lock:
            return await asyncio.to_thread(fn, *args)

    def invoke_event(self, handler, payload) -> None:
        """Call one Lua event handler with a payload table (worker side)."""
        handler(py_to_lua(self._runtime, payload or {}))

    # ── event-loop bridge ─────────────────────────────────────────────────────
    def _bridge(self, coro):
        """Run a coroutine on the bot's loop, blocking only this worker thread."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=_BRIDGE_TIMEOUT_S)

    # ── arch.dm / arch.user_name ──────────────────────────────────────────────
    # Every shim converts Lua values to plain Python *here*, in the worker
    # thread that owns the runtime, then bridges only plain-Python coroutines.
    # A coroutine that touched a Lua table from the loop thread would deadlock
    # against the worker that is still inside the Lua call.
    def _dm(self, user_id, spec) -> bool:
        embed = card_to_embed(lua_to_py(spec))
        return self._bridge(self._dm_send(user_id, embed))

    async def _dm_send(self, user_id, embed: discord.Embed) -> bool:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return False
        user = self._bot.get_user(uid)
        if user is None:
            try:
                user = await self._bot.fetch_user(uid)
            except discord.HTTPException:
                return False
        try:
            await user.send(embed=embed)
        except discord.HTTPException:
            return False
        return True

    def _user_name(self, user_id) -> str:
        async def _resolve() -> str:
            try:
                uid = int(user_id)
            except (TypeError, ValueError):
                return "someone"
            user = self._bot.get_user(uid)
            if user is None:
                try:
                    user = await self._bot.fetch_user(uid)
                except discord.HTTPException:
                    return f"user {uid}"
            return user.display_name

        return self._bridge(_resolve())

    def _lua_sigils(self, text):
        group_id, list_name, remaining = split_sigils(_s(text))
        return py_to_lua(self._runtime, {
            "group": group_id, "list": list_name, "text": remaining,
        })

    # ── command handlers ──────────────────────────────────────────────────────
    async def run_command(self, handler, cmd_def: dict, discord_ctx, rest: str) -> None:
        """Invoke one Lua command handler for a prefix command call."""
        if handler is None:
            await self._send_help(cmd_def, discord_ctx)
            return
        async with self._lock:
            try:
                await asyncio.to_thread(
                    self._invoke_command, handler, discord_ctx, rest,
                )
            except Exception:  # noqa: BLE001
                log.exception(
                    "plugin %s command failed", self._plugin.manifest.id,
                )
                try:
                    await discord_ctx.reply_error(
                        "That plugin command hit an error. Try again."
                    )
                except discord.HTTPException:
                    pass

    def _invoke_command(self, handler, discord_ctx, rest: str) -> None:
        handler(self._build_ctx(discord_ctx, rest))

    def _build_ctx(self, discord_ctx, rest: str):
        author = discord_ctx.author
        guild = discord_ctx.guild
        mentions = []
        for member in getattr(discord_ctx.message, "mentions", []) or []:
            if member.id == self._bot.user.id:
                continue
            mentions.append({
                "id": str(member.id),
                "name": member.display_name,
                "bot": bool(member.bot),
            })
        data = {
            "args": rest or "",
            "author_id": str(author.id),
            "author_name": author.display_name,
            "guild_id": str(guild.id) if guild else "0",
            "guild_name": guild.name if guild else "",
            "channel_id": str(discord_ctx.channel.id),
            "prefix": discord_ctx.prefix or ".",
            "is_dm": guild is None,
            "mentions": mentions,
            "store": self._store_table,
            "reply": lambda spec: self._bridge(self._send_embed(
                discord_ctx, card_to_embed(lua_to_py(spec)))),
            "ok": lambda msg: self._bridge(self._send_embed(
                discord_ctx, card("Done", description=_s(msg),
                                  color=C_SUCCESS).build())),
            "error": lambda msg: self._bridge(self._send_embed(
                discord_ctx, card("Error", description=_s(msg),
                                  color=C_ERROR).build())),
            "deliver": lambda pages, opts=None: self._bridge(self._send_pages(
                discord_ctx, self._embeds(pages), _opt_private(opts))),
            "confirm": lambda prompt: self._bridge(
                discord_ctx.confirm(_s(prompt))),
            "dm": self._dm,
            "user_name": self._user_name,
        }
        return py_to_lua(self._runtime, data)

    @staticmethod
    def _embeds(pages) -> list[discord.Embed]:
        """Convert a Lua card or array of cards into ready embeds (worker side)."""
        raw = lua_to_py(pages) or []
        if isinstance(raw, dict):  # a single card, not an array
            raw = [raw]
        return [card_to_embed(p) for p in raw]

    async def _send_embed(self, discord_ctx, embed: discord.Embed) -> None:
        await discord_ctx.reply(embed=embed, mention_author=False)

    async def _send_pages(
        self, discord_ctx, embeds: list[discord.Embed], private: bool,
    ) -> None:
        if not embeds:
            embeds = [card("Nothing here", color=C_NEUTRAL).build()]
        if not private:
            if len(embeds) == 1:
                await discord_ctx.reply(embed=embeds[0], mention_author=False)
            else:
                await discord_ctx.paginate(embeds)
            return
        try:
            channel = (discord_ctx.author.dm_channel
                       or await discord_ctx.author.create_dm())
            if len(embeds) == 1:
                await channel.send(embed=embeds[0])
            else:
                view = Paginator(embeds, author_id=discord_ctx.author.id)
                await channel.send(embed=embeds[0], view=view)
        except discord.Forbidden:
            await discord_ctx.reply_error(
                "I could not DM you. Turn on direct messages from server "
                "members and run that again."
            )
            return
        if discord_ctx.guild is not None:
            try:
                await discord_ctx.message.delete()
            except discord.HTTPException:
                pass
            try:
                await discord_ctx.send(
                    embed=card("Check your DMs",
                               description="Sent that to you privately.",
                               color=C_NEUTRAL).build(),
                    delete_after=8,
                )
            except discord.HTTPException:
                pass

    async def _send_help(self, cmd_def: dict, discord_ctx) -> None:
        await discord_ctx.reply(
            embed=command_help_embed(self._plugin, cmd_def, discord_ctx.prefix),
            mention_author=False,
        )

    # ── tool handlers ─────────────────────────────────────────────────────────
    def build_tools(self) -> list[ToolSpec]:
        """Wrap this plugin's ``tools`` into agent :class:`ToolSpec` objects."""
        specs: list[ToolSpec] = []
        for entry in self._plugin.tools:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            handler = entry.get("handler")
            if not name or handler is None:
                log.warning("plugin %s has a malformed tool, skipped",
                            self._plugin.manifest.id)
                continue
            specs.append(ToolSpec(
                name=str(name),
                description=str(entry.get("description") or ""),
                parameters=entry.get("parameters")
                or {"type": "object", "properties": {}},
                handler=self._make_tool_handler(handler),
                category="plugin",
                risk=RISK_SAFE,
            ))
        return specs

    def _make_tool_handler(self, lua_handler):
        async def handler(args: dict, tool_ctx) -> dict:
            async with self._lock:
                result = await asyncio.to_thread(
                    self._invoke_tool, lua_handler, args, tool_ctx,
                )
            if isinstance(result, dict):
                return result
            return {"result": result}

        return handler

    def _invoke_tool(self, lua_handler, args: dict, tool_ctx):
        """Call a Lua tool handler with ``(args, ctx)`` (worker side)."""
        ctx_table = self._build_tool_ctx(tool_ctx)
        return lua_to_py(lua_handler(
            py_to_lua(self._runtime, args or {}), ctx_table))

    def _build_tool_ctx(self, tool_ctx):
        """The per-call ``ctx`` table a Lua agent-tool handler receives.

        A tool has no Discord command context, so ``ctx.reply`` posts to the
        channel the conversation is happening in (``tool_ctx.channel_id``).
        """
        guild_id = getattr(tool_ctx, "guild_id", 0) or 0
        channel_id = getattr(tool_ctx, "channel_id", 0) or 0
        data = {
            "user_id": str(getattr(tool_ctx, "user_id", 0) or 0),
            "guild_id": str(guild_id),
            "channel_id": str(channel_id),
            "is_dm": not guild_id,
            "user_name": self._user_name,
            "store": self._store_table,
            "kv": self._kv_table,
            "dm": self._dm,
            "reply": lambda spec: self._bridge(self._do_discord_send(
                channel_id, card_to_embed(lua_to_py(spec)))),
        }
        return py_to_lua(self._runtime, data)

    # ── background loops ──────────────────────────────────────────────────────
    def make_loop_runner(self, loop_def: dict):
        """Return an async runner for one plugin loop, or ``None`` if invalid."""
        handler = loop_def.get("run")
        if handler is None:
            return None
        interval = loop_def.get("interval")
        try:
            interval = max(15, int(interval or 60))
        except (TypeError, ValueError):
            interval = 60
        name = str(loop_def.get("name") or "loop")
        plugin_id = self._plugin.manifest.id

        async def runner() -> None:
            await self._bot.wait_until_ready()
            while True:
                try:
                    async with self._lock:
                        await asyncio.to_thread(handler)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("plugin %s loop %s failed", plugin_id, name)
                await asyncio.sleep(interval)

        return runner


# ── discord.py command construction ───────────────────────────────────────────
def _resolve_handler(cmd_def: dict):
    """The Lua ``run`` function for a command, or ``None`` for a bare group."""
    handler = cmd_def.get("run")
    return handler if handler is not None else None


async def _no_bots_check(ctx) -> bool:
    from discord.ext import commands
    if ctx.author.bot:
        raise commands.CheckFailure("Bots cannot use this command.")
    return True


async def _guild_only_check(ctx) -> bool:
    from discord.ext import commands
    if ctx.guild is None:
        raise commands.NoPrivateMessage("This command only works in a server.")
    return True


def build_commands(api: LuaApi, plugin) -> list:
    """Build top-level discord.py commands for a plugin's command tree."""
    from discord.ext import commands

    built = []
    for cmd_def in plugin.commands:
        if not isinstance(cmd_def, dict):
            continue
        built.append(_build_one(api, plugin, cmd_def, commands))
    return built


def _build_one(api: LuaApi, plugin, cmd_def: dict, commands):
    name = str(cmd_def["name"])
    aliases = [str(a) for a in (cmd_def.get("aliases") or [])]
    summary = str(cmd_def.get("summary") or cmd_def.get("description") or "")
    guild_only = bool(cmd_def.get("guild_only"))
    subdefs = cmd_def.get("subcommands") or []
    handler = _resolve_handler(cmd_def)

    def _callback_for(target_def, target_handler):
        async def callback(ctx, *, rest: str = "") -> None:
            await api.run_command(target_handler, target_def, ctx, rest)
        return callback

    if subdefs:
        group = commands.Group(
            _callback_for(cmd_def, handler),
            name=name, aliases=aliases, help=summary,
            invoke_without_command=True, case_insensitive=True,
        )
        group.checks.append(_no_bots_check)
        if guild_only:
            group.checks.append(_guild_only_check)
        for sub in subdefs:
            if not isinstance(sub, dict):
                continue
            group.add_command(_build_one(api, plugin, sub, commands))
        return group

    cmd = commands.Command(
        _callback_for(cmd_def, handler),
        name=name, aliases=aliases, help=summary,
    )
    cmd.checks.append(_no_bots_check)
    if guild_only:
        cmd.checks.append(_guild_only_check)
    return cmd


def command_help_embed(plugin, cmd_def: dict, prefix: str) -> discord.Embed:
    """A help card for one command, listing its subcommands."""
    name = str(cmd_def.get("name"))
    builder = card(
        f"{prefix}{name}",
        description=str(cmd_def.get("summary")
                        or cmd_def.get("description") or ""),
        color=C_INFO,
    )
    for fld in cmd_def.get("help") or []:
        if isinstance(fld, dict) and fld.get("name"):
            builder.field(str(fld["name"]), str(fld.get("value") or "​"))
    for sub in cmd_def.get("subcommands") or []:
        if not isinstance(sub, dict):
            continue
        usage = sub.get("usage") or sub["name"]
        builder.field(
            f"{prefix}{name} {usage}",
            str(sub.get("summary") or sub.get("description") or "​"),
        )
    builder.footer(f"{plugin.manifest.name}  v{plugin.manifest.version}")
    return builder.build()


# ── small internal helpers ────────────────────────────────────────────────────
def _s(value) -> str:
    return "" if value is None else str(value)


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clamp(value, cap: float) -> float:
    """A plugin-supplied numeric option, bounded to ``[0, cap]``.

    A missing or unparseable value falls back to the cap, so a plugin gets the
    operator default unless it deliberately asks for something smaller.
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return cap
    if num < 0:
        return cap
    return min(num, cap)


def _opt_private(opts) -> bool:
    """Read ``{private=true}`` off a Lua options table (worker side)."""
    parsed = lua_to_py(opts) if opts is not None else None
    return bool(parsed.get("private")) if isinstance(parsed, dict) else False


def _fmt_time(epoch) -> str:
    if epoch is None or epoch == "":
        return "no date"
    try:
        return f"<t:{int(float(epoch))}:f>"
    except (TypeError, ValueError):
        return str(epoch)


def _stringify_id(record: dict | None) -> dict | None:
    """Hand record ids to Lua as strings -- Discord-scale ints lose precision."""
    if record is None:
        return None
    out = dict(record)
    if "id" in out:
        out["id"] = str(out["id"])
    return out
