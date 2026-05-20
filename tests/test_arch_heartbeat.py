"""tests/test_arch_heartbeat.py -- pure-logic tests for the heartbeat layer."""
from __future__ import annotations

from datetime import datetime, timezone

from arch.heartbeat import OK_TOKEN, in_active_window


def _at(hour: int) -> datetime:
    return datetime(2026, 5, 20, hour, 0, 0, tzinfo=timezone.utc)


def test_in_active_window_normal_daytime() -> None:
    assert in_active_window(_at(9), 8, 22) is True
    assert in_active_window(_at(7), 8, 22) is False
    assert in_active_window(_at(22), 8, 22) is False
    assert in_active_window(_at(21), 8, 22) is True


def test_in_active_window_inclusive_start_exclusive_end() -> None:
    # Start is inclusive, end is exclusive: matches "8:00 to 22:00" intent.
    assert in_active_window(_at(8), 8, 22) is True
    assert in_active_window(_at(22), 8, 22) is False


def test_in_active_window_overnight_wraps_midnight() -> None:
    # 22:00 to 06:00 should match late-night and early-morning.
    assert in_active_window(_at(23), 22, 6) is True
    assert in_active_window(_at(2), 22, 6) is True
    assert in_active_window(_at(8), 22, 6) is False


def test_in_active_window_full_day_when_start_equals_end() -> None:
    # Operator picks "always on" by setting the same start and end.
    for h in range(24):
        assert in_active_window(_at(h), 12, 12) is True


def test_ok_token_is_the_value_the_runner_compares() -> None:
    # The Heartbeat class checks ``text == OK_TOKEN or text.startswith(...)``;
    # if this changes, the reference app's HEARTBEAT_OK contract breaks.
    assert OK_TOKEN == "HEARTBEAT_OK"
