"""arch/scheduler.py -- durable scheduled task runner.

The Archimedes assistant lets a user say "remind me in an hour" or "every morning
at 9, check the calendar." That intent lands in ``archimedes_scheduled_tasks`` and
the scheduler polls it on a tight interval, firing due tasks back through
``ArchAgent.handle`` as if the user had typed the original prompt themselves.

The scheduler is intentionally simple: one polling loop, one due-task
query per tick, at-most-N tasks fired concurrently per tick. Cron is
parsed by a tiny built-in evaluator that understands the standard five
fields and ``*/N`` step expressions -- enough for "every weekday at 09:00"
without pulling in another dependency.

Tasks survive restarts because state lives in the database. A task that
crashed mid-fire reverts to ``pending`` on the next boot via
``mark_orphans_pending``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)


@dataclass
class ScheduledTask:
    id: int
    owner_id: int
    kind: str               # "oneshot" | "cron"
    cron_expr: str          # blank for oneshot
    run_at: datetime | None # populated for oneshot
    payload: dict[str, Any]
    status: str             # "pending" | "running" | "done" | "failed"
    last_run_at: datetime | None
    next_run_at: datetime | None


# ── Cron evaluator ────────────────────────────────────────────────────────────
def _match_field(value: int, expr: str, lo: int, hi: int) -> bool:
    """Match one ``minute hour dom month dow`` field against an integer.

    Supported: ``*``, ``N``, ``N,M``, ``A-B``, ``*/N``. That covers every
    cron Archimedes's UI will ever write. Anything else returns False -- a typo'd
    cron is silently inert, which is far safer than crashing the scheduler.
    """
    expr = expr.strip()
    if not expr or expr == "*":
        return True
    for part in expr.split(","):
        part = part.strip()
        if part == "*":
            return True
        if part.startswith("*/"):
            try:
                step = int(part[2:])
                if step > 0 and (value - lo) % step == 0:
                    return True
            except ValueError:
                continue
        elif "-" in part:
            a, _, b = part.partition("-")
            try:
                if int(a) <= value <= int(b):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(part) == value:
                    return True
            except ValueError:
                continue
    return False


def cron_due(expr: str, now: datetime) -> bool:
    """True when ``now`` matches every field of a five-field cron string."""
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    return (
        _match_field(now.minute, minute, 0, 59)
        and _match_field(now.hour, hour, 0, 23)
        and _match_field(now.day, dom, 1, 31)
        and _match_field(now.month, month, 1, 12)
        and _match_field(now.weekday(), dow, 0, 6)
    )


# ── Persistence ───────────────────────────────────────────────────────────────
class SchedulerStore:
    """Database surface for scheduled tasks. Pure SQL, no policy."""

    def __init__(self, db) -> None:
        self.db = db

    async def add_oneshot(self, *, owner_id: int, run_at: datetime,
                          payload: dict[str, Any]) -> int:
        row = await self.db.fetch_one(
            "INSERT INTO archimedes_scheduled_tasks "
            "(owner_id, kind, run_at, payload, status, next_run_at) "
            "VALUES ($1, 'oneshot', $2, $3::jsonb, 'pending', $2) "
            "RETURNING id",
            owner_id, run_at, json.dumps(payload),
        )
        return int(row["id"])

    async def add_cron(self, *, owner_id: int, cron_expr: str,
                       payload: dict[str, Any]) -> int:
        row = await self.db.fetch_one(
            "INSERT INTO archimedes_scheduled_tasks "
            "(owner_id, kind, cron_expr, payload, status) "
            "VALUES ($1, 'cron', $2, $3::jsonb, 'pending') "
            "RETURNING id",
            owner_id, cron_expr, json.dumps(payload),
        )
        return int(row["id"])

    async def cancel(self, task_id: int) -> bool:
        out = await self.db.execute(
            "UPDATE archimedes_scheduled_tasks SET status='done' WHERE id=$1 "
            "AND status IN ('pending','running')",
            task_id,
        )
        return "UPDATE 1" in (out or "")

    async def list_for_owner(self, owner_id: int) -> list[ScheduledTask]:
        rows = await self.db.fetch_all(
            "SELECT id, owner_id, kind, cron_expr, run_at, payload, status, "
            "last_run_at, next_run_at FROM archimedes_scheduled_tasks "
            "WHERE owner_id=$1 ORDER BY id",
            owner_id,
        )
        return [_row_to_task(r) for r in rows]

    async def due(self, *, now: datetime, limit: int = 20) -> list[ScheduledTask]:
        rows = await self.db.fetch_all(
            "SELECT id, owner_id, kind, cron_expr, run_at, payload, status, "
            "last_run_at, next_run_at FROM archimedes_scheduled_tasks "
            "WHERE status='pending' AND "
            "(kind='cron' OR (kind='oneshot' AND run_at <= $1)) "
            "ORDER BY id LIMIT $2",
            now, limit,
        )
        return [_row_to_task(r) for r in rows]

    async def mark_running(self, task_id: int) -> None:
        await self.db.execute(
            "UPDATE archimedes_scheduled_tasks SET status='running' WHERE id=$1",
            task_id,
        )

    async def mark_complete(self, task_id: int, *, success: bool,
                            next_run_at: datetime | None = None,
                            kind: str = "oneshot") -> None:
        if kind == "cron":
            # Cron tasks revert to pending so they fire again on the next tick.
            await self.db.execute(
                "UPDATE archimedes_scheduled_tasks SET status='pending', "
                "last_run_at=NOW(), next_run_at=$2 WHERE id=$1",
                task_id, next_run_at,
            )
        else:
            await self.db.execute(
                "UPDATE archimedes_scheduled_tasks SET status=$2, last_run_at=NOW() "
                "WHERE id=$1",
                task_id, "done" if success else "failed",
            )

    async def mark_orphans_pending(self) -> int:
        """A task left ``running`` after a crash flips back to ``pending``."""
        out = await self.db.execute(
            "UPDATE archimedes_scheduled_tasks SET status='pending' WHERE status='running'",
        )
        try:
            return int((out or "").split()[-1])
        except (ValueError, IndexError):
            return 0


def _row_to_task(r) -> ScheduledTask:
    raw = r["payload"]
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = dict(raw or {})
    return ScheduledTask(
        id=int(r["id"]), owner_id=int(r["owner_id"] or 0),
        kind=r["kind"], cron_expr=r["cron_expr"] or "",
        run_at=r["run_at"], payload=payload, status=r["status"],
        last_run_at=r["last_run_at"], next_run_at=r["next_run_at"],
    )


# ── Runner ────────────────────────────────────────────────────────────────────
TaskHandler = Callable[[ScheduledTask], Awaitable[None]]


class Scheduler:
    """The polling loop. ``start()`` returns once the loop is running; the
    caller cancels the task via ``stop()`` on shutdown."""

    def __init__(self, store: SchedulerStore, handler: TaskHandler, *,
                 poll_seconds: int = 15, max_concurrent: int = 2) -> None:
        self.store = store
        self.handler = handler
        self.poll_seconds = max(1, int(poll_seconds))
        self.max_concurrent = max(1, int(max_concurrent))
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        recovered = await self.store.mark_orphans_pending()
        if recovered:
            log.info("Scheduler: recovered %d orphaned task(s).", recovered)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="arch-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        sem = asyncio.Semaphore(self.max_concurrent)
        while not self._stop_event.is_set():
            try:
                await self._tick(sem)
            except Exception as exc:  # noqa: BLE001
                log.exception("Scheduler tick crashed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.poll_seconds,
                )
            except asyncio.TimeoutError:
                continue

    async def _tick(self, sem: asyncio.Semaphore) -> None:
        now = datetime.now(timezone.utc)
        due_tasks = await self.store.due(now=now)
        for task in due_tasks:
            if task.kind == "cron" and not cron_due(task.cron_expr, now):
                continue
            await sem.acquire()
            asyncio.create_task(self._fire(task, sem))

    async def _fire(self, task: ScheduledTask, sem: asyncio.Semaphore) -> None:
        success = False
        try:
            await self.store.mark_running(task.id)
            await self.handler(task)
            success = True
        except Exception as exc:  # noqa: BLE001
            log.exception("Scheduled task %d failed: %s", task.id, exc)
        finally:
            next_run = None
            if task.kind == "cron":
                next_run = datetime.now(timezone.utc) + timedelta(minutes=1)
            try:
                await self.store.mark_complete(
                    task.id, success=success, next_run_at=next_run,
                    kind=task.kind,
                )
            finally:
                sem.release()


# ── Convenience constructors ──────────────────────────────────────────────────
def parse_oneshot_delay(spec: str) -> datetime:
    """Parse "in 5 minutes" / "in 2h" into a UTC datetime.

    A liberal parser kept on purpose: this is called from chat where the
    user types in their own words. Supported units: s, sec, m, min, h, hr,
    d, day. Falls back to one hour from now on an unrecognised string.
    """
    text = (spec or "").strip().lower()
    if text.startswith("in "):
        text = text[3:].strip()
    parts = text.split()
    if not parts:
        return datetime.now(timezone.utc) + timedelta(hours=1)
    try:
        n = int(parts[0])
    except ValueError:
        return datetime.now(timezone.utc) + timedelta(hours=1)
    unit = (parts[1] if len(parts) > 1 else "m").lower().rstrip("s")
    if unit in ("s", "sec", "second"):
        delta = timedelta(seconds=n)
    elif unit in ("h", "hr", "hour"):
        delta = timedelta(hours=n)
    elif unit in ("d", "day"):
        delta = timedelta(days=n)
    else:
        delta = timedelta(minutes=n)
    return datetime.now(timezone.utc) + delta
