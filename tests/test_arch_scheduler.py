"""tests/test_arch_scheduler.py -- cron and oneshot-delay parsing."""
from __future__ import annotations

from datetime import datetime, timezone

from arch.scheduler import cron_due, parse_oneshot_delay


def _at(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, 0, tzinfo=timezone.utc)


# ── cron_due ─────────────────────────────────────────────────────────────────
def test_cron_due_exact_minute_and_hour() -> None:
    assert cron_due("0 9 * * *", _at(2026, 5, 20, 9, 0)) is True
    assert cron_due("0 9 * * *", _at(2026, 5, 20, 9, 1)) is False
    assert cron_due("0 9 * * *", _at(2026, 5, 20, 10, 0)) is False


def test_cron_due_every_n_minutes_step() -> None:
    expr = "*/15 * * * *"
    assert cron_due(expr, _at(2026, 5, 20, 9, 0)) is True
    assert cron_due(expr, _at(2026, 5, 20, 9, 15)) is True
    assert cron_due(expr, _at(2026, 5, 20, 9, 30)) is True
    assert cron_due(expr, _at(2026, 5, 20, 9, 45)) is True
    assert cron_due(expr, _at(2026, 5, 20, 9, 7)) is False


def test_cron_due_range_in_hour_field() -> None:
    expr = "0 9-17 * * *"
    assert cron_due(expr, _at(2026, 5, 20, 9, 0)) is True
    assert cron_due(expr, _at(2026, 5, 20, 13, 0)) is True
    assert cron_due(expr, _at(2026, 5, 20, 17, 0)) is True
    assert cron_due(expr, _at(2026, 5, 20, 18, 0)) is False
    assert cron_due(expr, _at(2026, 5, 20, 8, 0)) is False


def test_cron_due_list_of_minutes() -> None:
    expr = "0,15,30,45 * * * *"
    assert cron_due(expr, _at(2026, 5, 20, 9, 15)) is True
    assert cron_due(expr, _at(2026, 5, 20, 9, 16)) is False


def test_cron_due_weekday_field() -> None:
    # 2026-05-20 is a Wednesday (weekday=2 in Python's Monday=0 scheme).
    expr_weekday = "0 9 * * 0-4"   # Mon..Fri
    assert cron_due(expr_weekday, _at(2026, 5, 20, 9, 0)) is True
    expr_weekend = "0 9 * * 5,6"   # Sat..Sun
    assert cron_due(expr_weekend, _at(2026, 5, 20, 9, 0)) is False


def test_cron_due_rejects_malformed_strings() -> None:
    # A wrong field count or bad token must never fire -- a typo'd cron
    # should be silently inert, not a free-for-all.
    assert cron_due("not even cron", _at(2026, 5, 20, 9, 0)) is False
    assert cron_due("0 9 *", _at(2026, 5, 20, 9, 0)) is False
    assert cron_due("0 9 * * 7", _at(2026, 5, 20, 9, 0)) is False
    assert cron_due("", _at(2026, 5, 20, 9, 0)) is False


# ── parse_oneshot_delay ──────────────────────────────────────────────────────
def test_parse_oneshot_default_unit_is_minutes() -> None:
    now = datetime.now(timezone.utc)
    out = parse_oneshot_delay("5")
    delta = (out - now).total_seconds()
    assert 4 * 60 <= delta <= 6 * 60


def test_parse_oneshot_strip_leading_in() -> None:
    now = datetime.now(timezone.utc)
    out = parse_oneshot_delay("in 30 minutes")
    delta = (out - now).total_seconds()
    assert 29 * 60 <= delta <= 31 * 60


def test_parse_oneshot_seconds_and_hours() -> None:
    now = datetime.now(timezone.utc)
    secs = (parse_oneshot_delay("45 seconds") - now).total_seconds()
    assert 44 <= secs <= 46
    hrs = (parse_oneshot_delay("2 hours") - now).total_seconds()
    assert 2 * 3600 - 5 <= hrs <= 2 * 3600 + 5


def test_parse_oneshot_falls_back_to_an_hour_on_garbage() -> None:
    now = datetime.now(timezone.utc)
    out = parse_oneshot_delay("abracadabra")
    delta = (out - now).total_seconds()
    assert 3500 <= delta <= 3700
