"""ai/redis_store.py -- short-term conversation buffer backed by Redis.

Holds the last few raw turns for a (guild, channel, user) triple so the
model has immediate recent context even before anything is summarised into
long-term memory. Redis is optional: when ``REDIS_URL`` is unset every
method degrades to a no-op and the bot relies on Postgres history alone.
"""
from __future__ import annotations

import json
import logging
import time

from config import Config

log = logging.getLogger(__name__)


class ShortTermStore:
    """Thin async wrapper over a Redis list of recent turns."""

    def __init__(self) -> None:
        self._redis = None
        self._enabled = bool(Config.REDIS_URL)

    async def connect(self) -> None:
        if not self._enabled:
            log.info("Redis disabled -- short-term memory uses Postgres only")
            return
        try:
            import redis.asyncio as redis

            self._redis = redis.from_url(Config.REDIS_URL, decode_responses=True)
            await self._redis.ping()
            log.info("Redis connected -- short-term memory enabled")
        except Exception as exc:  # noqa: BLE001
            log.warning("Redis connection failed (%s) -- disabling short-term store", exc)
            self._redis = None
            self._enabled = False

    async def close(self) -> None:
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _key(guild_id: int, channel_id: int, user_id: int) -> str:
        return f"archimedes:st:{guild_id}:{channel_id}:{user_id}"

    async def add_turn(
        self, guild_id: int, channel_id: int, user_id: int, role: str, content: str,
    ) -> None:
        if self._redis is None:
            return
        key = self._key(guild_id, channel_id, user_id)
        item = json.dumps({"role": role, "content": content, "ts": time.time()})
        try:
            await self._redis.rpush(key, item)
            await self._redis.ltrim(key, -Config.SHORT_TERM_TURNS, -1)
            await self._redis.expire(key, Config.SHORT_TERM_TTL_S)
        except Exception as exc:  # noqa: BLE001
            log.debug("short-term add_turn failed: %s", exc)

    async def get_turns(
        self, guild_id: int, channel_id: int, user_id: int,
    ) -> list[dict]:
        if self._redis is None:
            return []
        try:
            raw = await self._redis.lrange(
                self._key(guild_id, channel_id, user_id), 0, -1,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("short-term get_turns failed: %s", exc)
            return []
        out: list[dict] = []
        for item in raw:
            try:
                out.append(json.loads(item))
            except json.JSONDecodeError:
                continue
        return out

    async def clear_user(self, guild_id: int, user_id: int) -> int:
        return await self._clear_pattern(f"archimedes:st:{guild_id}:*:{user_id}")

    async def clear_channel(self, guild_id: int, channel_id: int) -> int:
        return await self._clear_pattern(f"archimedes:st:{guild_id}:{channel_id}:*")

    async def clear_guild(self, guild_id: int) -> int:
        return await self._clear_pattern(f"archimedes:st:{guild_id}:*")

    async def _clear_pattern(self, pattern: str) -> int:
        if self._redis is None:
            return 0
        cleared = 0
        try:
            async for key in self._redis.scan_iter(match=pattern, count=200):
                await self._redis.delete(key)
                cleared += 1
        except Exception as exc:  # noqa: BLE001
            log.debug("short-term clear failed: %s", exc)
        return cleared
