"""ai/quota.py -- per-user AI message quota.

A rolling-window counter keyed by (user, guild). Callers reserve a slot
before doing model work and release it if the request fails, so a timed-out
or empty response never costs the user a slot.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

from config import Config

_QUOTA_WINDOW = Config.AI_QUOTA_WINDOW
_QUOTA_LIMIT = Config.AI_QUOTA_LIMIT

_user_timestamps: dict[tuple[int, int], deque] = defaultdict(deque)
_user_locks: dict[tuple[int, int], asyncio.Lock] = {}


def _prune(q: deque, now: float) -> None:
    while q and now - q[0] > _QUOTA_WINDOW:
        q.popleft()


async def reserve_ai_quota(user_id: int, guild_id: int) -> tuple[bool, int, float | None]:
    """Atomically reserve a quota slot.

    Returns ``(allowed, remaining, reservation_ts)``. If the caller later
    abandons the request, it must call :func:`cancel_ai_quota_reservation`
    with ``reservation_ts``.
    """
    key = (user_id, guild_id)
    lock = _user_locks.setdefault(key, asyncio.Lock())
    async with lock:
        q = _user_timestamps[key]
        now = time.monotonic()
        _prune(q, now)
        if _QUOTA_LIMIT - len(q) <= 0:
            return False, 0, None
        q.append(now)
        return True, _QUOTA_LIMIT - len(q), now


def cancel_ai_quota_reservation(user_id: int, guild_id: int, reservation_ts: float) -> None:
    """Release a slot reserved earlier (request failed / timed out)."""
    q = _user_timestamps.get((user_id, guild_id))
    if not q:
        return
    for i, ts in enumerate(q):
        if ts == reservation_ts:
            del q[i]
            break


def quota_window_hours() -> int:
    return max(1, _QUOTA_WINDOW // 3600)


def quota_limit() -> int:
    return _QUOTA_LIMIT
