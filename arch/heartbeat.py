"""arch/heartbeat.py -- the autonomous self-check loop.

Archimedes's flagship background behaviour: every N minutes (during a
configured active-hours window) the agent runs the heartbeat prompt
against itself, reviews its memories and pending tasks, and either logs a
``HEARTBEAT_OK`` or takes action. The run record lands in
``archimedes_heartbeat_log`` so the ``/heartbeat`` UI can show recent activity
just like the reference app.

The loop is intentionally light: one prompt, one model call, no tools.
Real work happens because the heartbeat prompt asks the agent to "address
it" -- so when the model produces non-OK output, the loop ships the
result back through the channel layer's announcer (when one is
registered) so the operator sees it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

OK_TOKEN = "HEARTBEAT_OK"


@dataclass
class HeartbeatResult:
    status: str         # "OK" | "ACTED" | "FAIL"
    detail: str
    duration_ms: int


HeartbeatRunner = Callable[[str], Awaitable[str]]
HeartbeatAnnouncer = Callable[[HeartbeatResult], Awaitable[None]]


class HeartbeatStore:
    """One-table persistence for the recent-activity strip."""

    def __init__(self, db) -> None:
        self.db = db

    async def record(self, result: HeartbeatResult) -> None:
        await self.db.execute(
            "INSERT INTO archimedes_heartbeat_log (status, detail, duration_ms) "
            "VALUES ($1, $2, $3)",
            result.status, result.detail[:1000], int(result.duration_ms),
        )

    async def recent(self, limit: int = 10) -> list[tuple[datetime, str, str]]:
        rows = await self.db.fetch_all(
            "SELECT ran_at, status, detail FROM archimedes_heartbeat_log "
            "ORDER BY ran_at DESC LIMIT $1",
            int(limit),
        )
        return [(r["ran_at"], r["status"], r["detail"] or "") for r in rows]


def in_active_window(now: datetime, start_hour: int, end_hour: int) -> bool:
    """True when ``now`` falls inside the active-hours window.

    ``start_hour < end_hour`` is a normal daytime window (08-22). When the
    operator wants an overnight window (22-06) we wrap, returning True from
    22 up to midnight and from midnight up to 06.
    """
    h = now.hour
    if start_hour == end_hour:
        return True            # 24/7
    if start_hour < end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


class Heartbeat:
    """The periodic self-check loop.

    The ``runner`` is the function that actually calls the model with the
    heartbeat prompt -- in practice ``ArchAgent.run_heartbeat`` -- and
    returns the model's text reply. The ``announcer`` is optional: when an
    active channel knows where to broadcast a non-OK result, it registers
    one; otherwise the result lands only in the database log.
    """

    def __init__(
        self,
        store: HeartbeatStore,
        runner: HeartbeatRunner,
        *,
        enabled: bool,
        interval_minutes: int,
        active_hour_start: int,
        active_hour_end: int,
        prompt: str,
        announcer: HeartbeatAnnouncer | None = None,
    ) -> None:
        self.store = store
        self.runner = runner
        self.enabled = enabled
        self.interval_minutes = max(1, int(interval_minutes))
        self.active_hour_start = int(active_hour_start) % 24
        self.active_hour_end = int(active_hour_end) % 24
        self.prompt = prompt
        self.announcer = announcer
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None or not self.enabled:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="arch-heartbeat")
        log.info(
            "Heartbeat: enabled, every %dm during %02d:00-%02d:00.",
            self.interval_minutes, self.active_hour_start, self.active_hour_end,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

    async def trigger_once(self) -> HeartbeatResult:
        """Run the heartbeat now, ignoring the active-hours window. Useful
        for an operator who types ``.ai arch heartbeat run`` to test."""
        return await self._fire()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                now = datetime.now(timezone.utc)
                if in_active_window(
                    now, self.active_hour_start, self.active_hour_end,
                ):
                    await self._fire()
                else:
                    log.debug("Heartbeat: outside active window, skipping.")
            except Exception as exc:  # noqa: BLE001
                log.exception("Heartbeat tick crashed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.interval_minutes * 60,
                )
            except asyncio.TimeoutError:
                continue

    async def _fire(self) -> HeartbeatResult:
        started = time.monotonic()
        try:
            text = (await self.runner(self.prompt)).strip()
        except Exception as exc:  # noqa: BLE001
            result = HeartbeatResult(
                status="FAIL", detail=str(exc),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        else:
            if text == OK_TOKEN or text.startswith(OK_TOKEN):
                result = HeartbeatResult(
                    status="OK", detail="",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
            else:
                result = HeartbeatResult(
                    status="ACTED", detail=text[:1000],
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
        try:
            await self.store.record(result)
        except Exception as exc:  # noqa: BLE001
            log.warning("Heartbeat: failed to record result: %s", exc)
        if self.announcer and result.status == "ACTED":
            try:
                await self.announcer(result)
            except Exception as exc:  # noqa: BLE001
                log.warning("Heartbeat announce failed: %s", exc)
        return result
