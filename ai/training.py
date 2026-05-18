"""ai/training.py -- append-only capture of every chat turn.

Every completed turn is written to ``archimedes_training_turns`` along with the
full message list, so the corpus can later be curated or used for offline
fine-tuning without adding any runtime inference dependency. The thumbs
up / down reaction on a reply scores the matching row.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class TrainingTurn:
    user_id: int
    guild_id: int
    channel_id: int
    user_message: str
    assistant_reply: str
    model: str


class TrainingLogger:
    """Writes chat turns and feedback to the training corpus."""

    def __init__(self, db) -> None:
        self.db = db

    async def log_turn(
        self, *, user_id: int, guild_id: int, channel_id: int,
        user_message: str, assistant_reply: str, messages: list[dict],
        model: str = "",
    ) -> int | None:
        """Append one turn. Returns the new row id, or None on failure."""
        try:
            return await self.db.fetch_val(
                "INSERT INTO archimedes_training_turns "
                "(user_id, guild_id, channel_id, user_message, assistant_reply, "
                " messages, model) "
                "VALUES ($1,$2,$3,$4,$5,$6::jsonb,$7) RETURNING id",
                int(user_id), int(guild_id), int(channel_id),
                user_message, assistant_reply,
                json.dumps(messages, default=str), model,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("training log_turn failed: %s", exc)
            return None

    async def set_feedback(self, channel_id: int, value: int) -> None:
        """Score the most recent turn in a channel (+1 / -1)."""
        try:
            await self.db.execute(
                "UPDATE archimedes_training_turns SET feedback=$2 WHERE id = ("
                "  SELECT id FROM archimedes_training_turns WHERE channel_id=$1 "
                "  ORDER BY id DESC LIMIT 1)",
                int(channel_id), int(value),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("training set_feedback failed: %s", exc)
