"""framework/db.py -- PostgreSQL access layer.

A thin async wrapper over an asyncpg pool. Generic ``fetch_*`` / ``execute``
are used directly by services; the AI-specific convenience methods below
keep the cogs free of inline SQL for the common reads and writes.

Single source of truth: any value used in more than one place lives in a
method here, never copy-pasted SQL across cogs.
"""
from __future__ import annotations

import json
import logging
import os

import asyncpg

log = logging.getLogger(__name__)

# Columns ``update_guild_setting`` is allowed to write. The column name is
# always supplied by bot code (never user input), but an explicit allowlist
# stops a typo from silently writing nothing and documents the schema.
_GUILD_SETTING_COLUMNS: frozenset[str] = frozenset({
    "ai_chat_enabled", "ai_commentary_enabled", "ai_flavor_enabled",
    "ai_events_enabled", "ai_ambient_enabled", "ai_threaded",
    "ai_persona_name", "ai_promptchat", "ai_promptcommentary",
    "ai_promptevents", "ai_promptflavor",
    "ai_reply_delete_after", "ai_cmd_delete_after",
    "search_backend", "tools_backend",
})

# Short feature name -> guild_settings boolean column.
_AI_FLAG_COLUMNS: dict[str, str] = {
    "chat": "ai_chat_enabled",
    "mm": "ai_commentary_enabled",
    "commentary": "ai_commentary_enabled",
    "flavor": "ai_flavor_enabled",
    "events": "ai_events_enabled",
    "ambient": "ai_ambient_enabled",
    "threaded": "ai_threaded",
}

# Columns ``update_installed_plugin`` is allowed to write. Column names are
# always supplied by bot code, never user input; the allowlist documents the
# writable surface and stops a typo from silently writing nothing.
_PLUGIN_UPDATE_COLUMNS: frozenset[str] = frozenset({
    "name", "version", "description", "author", "category",
    "source", "source_repo", "enabled",
})


class Database:
    """Async PostgreSQL handle shared across the whole bot."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self.pool: asyncpg.Pool | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=10, command_timeout=30,
        )
        await self._apply_schema()
        log.info("Database pool ready")

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def _apply_schema(self) -> None:
        """Run the idempotent schema file on startup."""
        path = os.path.join(os.path.dirname(__file__), "..", "database", "schema.sql")
        path = os.path.abspath(path)
        if not os.path.exists(path):
            log.warning("schema.sql missing at %s -- skipping", path)
            return
        with open(path, "r", encoding="utf-8") as fh:
            sql = fh.read()
        async with self.pool.acquire() as conn:
            await conn.execute(sql)
        log.info("Schema applied")

    # ── generic accessors ─────────────────────────────────────────────────────
    async def fetch_one(self, query: str, *args) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, *args)
        return dict(row) if row is not None else None

    async def fetch_all(self, query: str, *args) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *args)
        return [dict(r) for r in rows]

    async def fetch_val(self, query: str, *args):
        async with self.pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    async def execute(self, query: str, *args) -> str:
        async with self.pool.acquire() as conn:
            return await conn.execute(query, *args)

    # ── guild settings ────────────────────────────────────────────────────────
    async def get_guild_settings(self, guild_id: int) -> dict:
        """Return the full guild_settings row, creating it if absent."""
        await self.execute(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) "
            "ON CONFLICT (guild_id) DO NOTHING",
            int(guild_id),
        )
        row = await self.fetch_one(
            "SELECT * FROM guild_settings WHERE guild_id=$1", int(guild_id),
        )
        return row or {}

    async def update_guild_setting(self, guild_id: int, column: str, value) -> None:
        if column not in _GUILD_SETTING_COLUMNS:
            raise ValueError(f"unknown guild setting column: {column!r}")
        await self.execute(
            "INSERT INTO guild_settings (guild_id) VALUES ($1) "
            "ON CONFLICT (guild_id) DO NOTHING",
            int(guild_id),
        )
        await self.execute(
            f"UPDATE guild_settings SET {column}=$2 WHERE guild_id=$1",
            int(guild_id), value,
        )

    async def get_ai_flags(self, guild_id: int) -> dict:
        """Return AI feature flags keyed by short name (chat, mm, ...)."""
        s = await self.get_guild_settings(guild_id)
        return {
            "chat": bool(s.get("ai_chat_enabled", True)),
            "mm": bool(s.get("ai_commentary_enabled", False)),
            "commentary": bool(s.get("ai_commentary_enabled", False)),
            "flavor": bool(s.get("ai_flavor_enabled", False)),
            "events": bool(s.get("ai_events_enabled", False)),
            "ambient": bool(s.get("ai_ambient_enabled", False)),
            "threaded": bool(s.get("ai_threaded", True)),
        }

    # ── reply mode (.arch chat | threads) ────────────────────────────────────
    async def get_archimedes_reply_mode(self, user_id: int, guild_id: int) -> str:
        val = await self.fetch_val(
            "SELECT mode FROM archimedes_reply_modes WHERE user_id=$1 AND guild_id=$2",
            int(user_id), int(guild_id),
        )
        return val or "thread"

    async def set_archimedes_reply_mode(self, user_id: int, guild_id: int, mode: str) -> None:
        await self.execute(
            "INSERT INTO archimedes_reply_modes (user_id, guild_id, mode) VALUES ($1,$2,$3) "
            "ON CONFLICT (user_id, guild_id) DO UPDATE SET mode=EXCLUDED.mode",
            int(user_id), int(guild_id), mode,
        )

    # ── conversation history ──────────────────────────────────────────────────
    async def save_ai_message(
        self, user_id: int, guild_id: int, role: str, content: str,
        history_key: str = "default",
    ) -> None:
        await self.execute(
            "INSERT INTO ai_conversations (user_id, guild_id, history_key, role, content) "
            "VALUES ($1,$2,$3,$4,$5)",
            int(user_id), int(guild_id), history_key, role, content,
        )

    async def get_ai_history(
        self, user_id: int, guild_id: int, history_key: str = "default",
        limit: int = 12,
    ) -> list[dict]:
        """Return the last ``limit`` turns as ``[{role, content}, ...]`` oldest-first."""
        rows = await self.fetch_all(
            "SELECT role, content FROM ai_conversations "
            "WHERE user_id=$1 AND guild_id=$2 AND history_key=$3 "
            "ORDER BY id DESC LIMIT $4",
            int(user_id), int(guild_id), history_key, int(limit),
        )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def clear_ai_conversation(self, user_id: int, guild_id: int) -> int:
        status = await self.execute(
            "DELETE FROM ai_conversations WHERE user_id=$1 AND guild_id=$2",
            int(user_id), int(guild_id),
        )
        return _rowcount(status)

    async def clear_all_ai_conversations(self, guild_id: int) -> int:
        status = await self.execute(
            "DELETE FROM ai_conversations WHERE guild_id=$1", int(guild_id),
        )
        return _rowcount(status)

    # ── opt-out ───────────────────────────────────────────────────────────────
    async def is_ai_opted_out(self, user_id: int, guild_id: int) -> bool:
        val = await self.fetch_val(
            "SELECT 1 FROM ai_opt_outs WHERE user_id=$1 AND guild_id=$2",
            int(user_id), int(guild_id),
        )
        return bool(val)

    async def set_ai_opt_out(self, user_id: int, guild_id: int) -> None:
        await self.execute(
            "INSERT INTO ai_opt_outs (user_id, guild_id) VALUES ($1,$2) "
            "ON CONFLICT DO NOTHING",
            int(user_id), int(guild_id),
        )
        # Opting out wipes everything Archimedes learned about the member.
        await self.wipe_ai_user_state(user_id, guild_id)

    async def clear_ai_opt_out(self, user_id: int, guild_id: int) -> None:
        await self.execute(
            "DELETE FROM ai_opt_outs WHERE user_id=$1 AND guild_id=$2",
            int(user_id), int(guild_id),
        )

    # ── per-user memory ───────────────────────────────────────────────────────
    async def get_ai_user_memory(self, user_id: int, guild_id: int) -> str:
        val = await self.fetch_val(
            "SELECT memory FROM ai_user_memory WHERE user_id=$1 AND guild_id=$2",
            int(user_id), int(guild_id),
        )
        return val or ""

    async def set_ai_user_memory(self, user_id: int, guild_id: int, memory: str) -> None:
        await self.execute(
            "INSERT INTO ai_user_memory (user_id, guild_id, memory, last_refreshed_at) "
            "VALUES ($1,$2,$3,NOW()) "
            "ON CONFLICT (user_id, guild_id) DO UPDATE "
            "SET memory=EXCLUDED.memory, last_refreshed_at=NOW()",
            int(user_id), int(guild_id), memory,
        )

    async def clear_ai_user_memory(self, user_id: int, guild_id: int) -> bool:
        status = await self.execute(
            "DELETE FROM ai_user_memory WHERE user_id=$1 AND guild_id=$2",
            int(user_id), int(guild_id),
        )
        return _rowcount(status) > 0

    async def get_ai_memories_for_users(
        self, guild_id: int, user_ids: list[int],
    ) -> dict[int, str]:
        if not user_ids:
            return {}
        rows = await self.fetch_all(
            "SELECT user_id, memory FROM ai_user_memory "
            "WHERE guild_id=$1 AND user_id = ANY($2::bigint[])",
            int(guild_id), [int(u) for u in user_ids],
        )
        return {int(r["user_id"]): r["memory"] for r in rows if r["memory"]}

    # ── bulk wipes (used by .arch ctx clear / .ai recontext) ─────────────────
    async def wipe_ai_user_state(self, user_id: int, guild_id: int) -> dict[str, int]:
        """Drop every per-user AI row in a guild. Returns per-table counts."""
        out: dict[str, int] = {}
        for table in (
            "ai_conversations", "ai_user_memory", "ai_user_traits",
            "ai_user_events", "ai_reaction_memory", "ai_tool_memory",
        ):
            status = await self.execute(
                f"DELETE FROM {table} WHERE user_id=$1 AND guild_id=$2",
                int(user_id), int(guild_id),
            )
            if _rowcount(status):
                out[table] = _rowcount(status)
        # Scopes are computed in Python so the literal never confuses the
        # query planner's parameter type inference.
        scope = f"user:{int(user_id)}:{int(guild_id)}"
        status = await self.execute(
            "DELETE FROM archimedes_facts WHERE scope=$1", scope,
        )
        if _rowcount(status):
            out["archimedes_facts"] = _rowcount(status)
        return out

    async def wipe_ai_guild_state(self, guild_id: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for table in (
            "ai_conversations", "ai_user_memory", "ai_user_traits",
            "ai_user_events", "ai_reaction_memory", "ai_tool_memory",
            "channel_context",
        ):
            status = await self.execute(
                f"DELETE FROM {table} WHERE guild_id=$1", int(guild_id),
            )
            if _rowcount(status):
                out[table] = _rowcount(status)
        guild_pat = f"guild:{int(guild_id)}"
        user_pat = f"user:%:{int(guild_id)}"
        for table in ("archimedes_facts", "archimedes_episodes"):
            status = await self.execute(
                f"DELETE FROM {table} WHERE scope=$1 OR scope LIKE $2",
                guild_pat, user_pat,
            )
            if _rowcount(status):
                out[table] = _rowcount(status)
        return out

    async def wipe_ai_channel_context(self, guild_id: int, channel_id: int) -> int:
        status = await self.execute(
            "DELETE FROM channel_context WHERE guild_id=$1 AND channel_id=$2",
            int(guild_id), int(channel_id),
        )
        return _rowcount(status)

    # ── saved Archimedes answers (.arch save) ─────────────────────────────────────
    async def add_archimedes_saved_message(
        self, user_id: int, guild_id: int, channel_id: int, archimedes_message_id: int,
        trigger_message_id: int | None, prompt_text: str, response_text: str,
        jump_url: str,
    ) -> bool:
        status = await self.execute(
            "INSERT INTO archimedes_saved_messages "
            "(user_id, guild_id, channel_id, archimedes_message_id, trigger_message_id, "
            " prompt_text, response_text, jump_url) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8) "
            "ON CONFLICT (user_id, guild_id, archimedes_message_id) DO NOTHING",
            int(user_id), int(guild_id), int(channel_id), int(archimedes_message_id),
            trigger_message_id, prompt_text, response_text, jump_url,
        )
        return _rowcount(status) > 0

    async def list_archimedes_saved_messages(
        self, user_id: int, guild_id: int,
    ) -> list[dict]:
        return await self.fetch_all(
            "SELECT *, EXTRACT(EPOCH FROM saved_at) AS saved_at "
            "FROM archimedes_saved_messages WHERE user_id=$1 AND guild_id=$2 "
            "ORDER BY archimedes_saved_messages.saved_at ASC",
            int(user_id), int(guild_id),
        )

    async def delete_archimedes_saved_message(
        self, user_id: int, guild_id: int, row_id: int,
    ) -> bool:
        status = await self.execute(
            "DELETE FROM archimedes_saved_messages WHERE id=$1 AND user_id=$2 AND guild_id=$3",
            int(row_id), int(user_id), int(guild_id),
        )
        return _rowcount(status) > 0

    # ── custom emoji meaning index ────────────────────────────────────────────
    async def get_all_emoji_meanings(self, guild_id: int) -> list[dict]:
        return await self.fetch_all(
            "SELECT *, EXTRACT(EPOCH FROM updated_at) AS updated_at "
            "FROM guild_emoji_meanings WHERE guild_id=$1 ORDER BY name ASC",
            int(guild_id),
        )

    async def upsert_emoji_meaning(
        self, guild_id: int, emoji_id: int, name: str, description: str, *,
        animated: bool = False, category: str | None = None, source: str = "vision",
    ) -> None:
        await self.execute(
            "INSERT INTO guild_emoji_meanings "
            "(guild_id, emoji_id, name, description, animated, category, source, updated_at) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,NOW()) "
            "ON CONFLICT (guild_id, emoji_id) DO UPDATE SET "
            "name=EXCLUDED.name, description=EXCLUDED.description, "
            "animated=EXCLUDED.animated, category=EXCLUDED.category, "
            "source=EXCLUDED.source, updated_at=NOW()",
            int(guild_id), int(emoji_id), name, description,
            bool(animated), category, source,
        )

    async def get_stale_emoji_meaning_ids(
        self, guild_id: int, max_age_days: int = 14,
    ) -> list[int]:
        rows = await self.fetch_all(
            "SELECT emoji_id FROM guild_emoji_meanings "
            "WHERE guild_id=$1 AND updated_at < NOW() - ($2 * INTERVAL '1 day')",
            int(guild_id), int(max_age_days),
        )
        return [int(r["emoji_id"]) for r in rows]

    # ── Lua plugins: the installed-plugin registry ────────────────────────────
    async def list_installed_plugins(self) -> list[dict]:
        """Every plugin row, bundled and marketplace alike, oldest first."""
        return await self.fetch_all(
            "SELECT * FROM installed_plugins ORDER BY installed_at ASC, plugin_id ASC",
        )

    async def get_installed_plugin(self, plugin_id: str) -> dict | None:
        return await self.fetch_one(
            "SELECT * FROM installed_plugins WHERE plugin_id=$1", str(plugin_id),
        )

    async def upsert_installed_plugin(
        self, *, plugin_id: str, name: str, version: str, origin: str,
        description: str = "", author: str = "", category: str = "General",
        source: str = "", source_repo: str = "", enabled: bool = True,
        installed_by: int | None = None,
    ) -> None:
        """Insert a plugin row, or refresh its metadata if it already exists.

        ``enabled`` is only written on first insert -- a refresh of a bundled
        plugin must never silently flip an operator's enable/disable choice.
        """
        await self.execute(
            "INSERT INTO installed_plugins "
            "(plugin_id, name, version, origin, description, author, category, "
            " source, source_repo, enabled, installed_by) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) "
            "ON CONFLICT (plugin_id) DO UPDATE SET "
            "name=EXCLUDED.name, version=EXCLUDED.version, origin=EXCLUDED.origin, "
            "description=EXCLUDED.description, author=EXCLUDED.author, "
            "category=EXCLUDED.category, source=EXCLUDED.source, "
            "source_repo=EXCLUDED.source_repo, updated_at=NOW()",
            str(plugin_id), name, version, origin, description, author, category,
            source, source_repo, bool(enabled),
            int(installed_by) if installed_by else None,
        )

    async def update_installed_plugin(self, plugin_id: str, **fields) -> bool:
        cols = {k: v for k, v in fields.items() if k in _PLUGIN_UPDATE_COLUMNS}
        if not cols:
            return False
        names = list(cols.keys())
        assignments = ", ".join(f"{n}=${i + 2}" for i, n in enumerate(names))
        values = [cols[n] for n in names]
        status = await self.execute(
            f"UPDATE installed_plugins SET {assignments}, updated_at=NOW() "
            "WHERE plugin_id=$1",
            str(plugin_id), *values,
        )
        return _rowcount(status) > 0

    async def delete_installed_plugin(self, plugin_id: str) -> bool:
        status = await self.execute(
            "DELETE FROM installed_plugins WHERE plugin_id=$1", str(plugin_id),
        )
        return _rowcount(status) > 0

    # ── Lua plugins: the generic document store ───────────────────────────────
    async def plugin_store_put(
        self, namespace: str, collection: str, doc: dict,
    ) -> int:
        """Insert one document, returning its auto-assigned id."""
        new_id = await self.fetch_val(
            "INSERT INTO plugin_storage (namespace, collection, doc) "
            "VALUES ($1,$2,$3::jsonb) RETURNING id",
            str(namespace), str(collection), _json_dumps(doc),
        )
        return int(new_id)

    async def plugin_store_get(
        self, namespace: str, collection: str, record_id: int,
    ) -> dict | None:
        row = await self.fetch_one(
            "SELECT id, doc FROM plugin_storage "
            "WHERE namespace=$1 AND collection=$2 AND id=$3",
            str(namespace), str(collection), int(record_id),
        )
        return _store_row(row)

    async def plugin_store_update(
        self, namespace: str, collection: str, record_id: int, doc: dict,
    ) -> bool:
        status = await self.execute(
            "UPDATE plugin_storage SET doc=$4::jsonb, updated_at=NOW() "
            "WHERE namespace=$1 AND collection=$2 AND id=$3",
            str(namespace), str(collection), int(record_id), _json_dumps(doc),
        )
        return _rowcount(status) > 0

    async def plugin_store_delete(
        self, namespace: str, collection: str, record_id: int,
    ) -> bool:
        status = await self.execute(
            "DELETE FROM plugin_storage "
            "WHERE namespace=$1 AND collection=$2 AND id=$3",
            str(namespace), str(collection), int(record_id),
        )
        return _rowcount(status) > 0

    async def plugin_store_query(
        self, namespace: str, collection: str, match: dict | None = None,
    ) -> list[dict]:
        """Return documents in a collection, filtered by JSON containment.

        ``match`` is an equality filter -- every key/value pair must be
        present in the stored document. ``None`` or ``{}`` returns the lot.
        """
        if match:
            rows = await self.fetch_all(
                "SELECT id, doc FROM plugin_storage "
                "WHERE namespace=$1 AND collection=$2 AND doc @> $3::jsonb "
                "ORDER BY id ASC",
                str(namespace), str(collection), _json_dumps(match),
            )
        else:
            rows = await self.fetch_all(
                "SELECT id, doc FROM plugin_storage "
                "WHERE namespace=$1 AND collection=$2 ORDER BY id ASC",
                str(namespace), str(collection),
            )
        return [_store_row(r) for r in rows]

    async def plugin_store_clear(
        self, namespace: str, collection: str | None = None,
    ) -> int:
        """Drop every document in a namespace (or one collection of it)."""
        if collection is None:
            status = await self.execute(
                "DELETE FROM plugin_storage WHERE namespace=$1", str(namespace),
            )
        else:
            status = await self.execute(
                "DELETE FROM plugin_storage WHERE namespace=$1 AND collection=$2",
                str(namespace), str(collection),
            )
        return _rowcount(status)


def _rowcount(status: str) -> int:
    """Parse the affected-row count out of an asyncpg status string."""
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except (ValueError, IndexError):
        return 0


def _json_dumps(value) -> str:
    """Serialise a plugin document for a JSONB column."""
    return json.dumps(value if value is not None else {}, default=str)


def _store_row(row: dict | None) -> dict | None:
    """Flatten a plugin_storage row into ``{id, ...doc}`` for callers.

    The document is stored as JSONB, so asyncpg may hand it back as a JSON
    string; decode it either way. ``id`` always wins over a stray ``id`` key
    inside the document.
    """
    if row is None:
        return None
    doc = row.get("doc")
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except (ValueError, TypeError):
            doc = {}
    if not isinstance(doc, dict):
        doc = {}
    out = dict(doc)
    out["id"] = int(row["id"])
    return out
