"""framework/audit.py -- staff action audit log.

Every mutating ,ai command records a row so operators have a feed of who
changed what. ``,ai audit`` reads it back.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SCOPE_AI = "ai"
SEVERITY_INFO = "info"
SEVERITY_WARN = "warn"
SEVERITY_DANGER = "danger"


async def log_staff_action(
    db, *, scope: str, guild_id: int, actor_id: int, action: str,
    severity: str = SEVERITY_INFO, details: str = "",
) -> None:
    """Append one audit row. Never raises -- auditing must not break a command."""
    try:
        await db.execute(
            "INSERT INTO staff_audit "
            "(scope, guild_id, actor_id, action, severity, details) "
            "VALUES ($1,$2,$3,$4,$5,$6)",
            scope, int(guild_id), int(actor_id), action, severity, details[:500],
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("audit write failed: %s", exc)


async def recent_staff_actions(
    db, *, guild_id: int, scope: str, limit: int = 50,
) -> list[dict]:
    """Return recent audit rows for a scope, newest first."""
    return await db.fetch_all(
        "SELECT actor_id, action, severity, details, "
        "EXTRACT(EPOCH FROM created_at) AS created_at "
        "FROM staff_audit WHERE guild_id=$1 AND scope=$2 "
        "ORDER BY created_at DESC LIMIT $3",
        int(guild_id), scope, int(limit),
    )
