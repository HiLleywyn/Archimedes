"""tests/test_arch_soul.py -- pure-logic tests for the soul layer.

The soul store is database-backed, so the persistence path is exercised
elsewhere (smoke-only here). These tests cover the parts that need no
PostgreSQL: preset registry, normalisation, length cap.
"""
from __future__ import annotations

from arch.soul import (
    DEFAULT_SOUL, SOUL_MAX_CHARS, SOUL_PRESETS, list_presets, normalise, preset,
)


def test_presets_have_known_names() -> None:
    names = set(list_presets())
    # The reference UI assumes "default" plus a handful of named voices;
    # losing one would silently regress the picker, so spot-check them.
    assert "default" in names
    assert "short" in names
    assert "tutor" in names
    assert "creative" in names
    assert "expert" in names


def test_preset_lookup_is_case_insensitive() -> None:
    assert preset("Tutor") == SOUL_PRESETS["tutor"]
    assert preset("EXPERT") == SOUL_PRESETS["expert"]


def test_preset_lookup_falls_back_to_default_for_unknown() -> None:
    assert preset("does-not-exist") == DEFAULT_SOUL


def test_normalise_trims_whitespace() -> None:
    assert normalise("  hello  ") == "hello"


def test_normalise_caps_at_soul_max_chars() -> None:
    long_input = "x" * (SOUL_MAX_CHARS + 500)
    out = normalise(long_input)
    assert len(out) == SOUL_MAX_CHARS


def test_normalise_blank_returns_blank() -> None:
    assert normalise("") == ""
    assert normalise("   ") == ""


def test_every_preset_fits_under_the_cap() -> None:
    # If a built-in preset ever exceeds the cap, the soul store would
    # silently truncate it -- so this guards the constant against drift.
    for name, body in SOUL_PRESETS.items():
        assert len(body) <= SOUL_MAX_CHARS, name
