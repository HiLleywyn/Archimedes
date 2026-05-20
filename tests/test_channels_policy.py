"""tests/test_channels_policy.py -- DM / guild access policy."""
from __future__ import annotations

from channels.policy import (
    PolicyDecision, evaluate_dm, evaluate_guild,
)


# ── DM policy ────────────────────────────────────────────────────────────────
def test_dm_open_lets_everyone_through() -> None:
    assert evaluate_dm(
        "open", user_id=999, allow_users=[],
    ) is PolicyDecision.ALLOW


def test_dm_disabled_denies_everyone() -> None:
    assert evaluate_dm(
        "disabled", user_id=999, allow_users=[999], owner_id=999,
    ) is PolicyDecision.DENY


def test_dm_allowlist_only_admits_listed_users() -> None:
    assert evaluate_dm(
        "allowlist", user_id=100, allow_users=[100, 200],
    ) is PolicyDecision.ALLOW
    assert evaluate_dm(
        "allowlist", user_id=300, allow_users=[100, 200],
    ) is PolicyDecision.DENY


def test_dm_allowlist_always_admits_the_owner() -> None:
    # Even if the owner is missing from the allowlist, they are not locked
    # out of their own bot.
    assert evaluate_dm(
        "allowlist", user_id=42, allow_users=[], owner_id=42,
    ) is PolicyDecision.ALLOW


def test_dm_unknown_policy_falls_back_to_allowlist_behaviour() -> None:
    # A typo in env config must not silently flip to open mode -- the safer
    # default is allowlist, which denies unlisted users.
    assert evaluate_dm(
        "completely-unknown", user_id=1, allow_users=[],
    ) is PolicyDecision.DENY


# ── Guild policy ─────────────────────────────────────────────────────────────
def test_guild_open_lets_every_guild_through() -> None:
    assert evaluate_guild(
        "open", guild_id=42, allow_guilds=[],
    ) is PolicyDecision.ALLOW


def test_guild_disabled_denies_every_guild() -> None:
    assert evaluate_guild(
        "disabled", guild_id=42, allow_guilds=[42],
    ) is PolicyDecision.DENY


def test_guild_allowlist_filters_by_id() -> None:
    assert evaluate_guild(
        "allowlist", guild_id=42, allow_guilds=[42, 100],
    ) is PolicyDecision.ALLOW
    assert evaluate_guild(
        "allowlist", guild_id=999, allow_guilds=[42, 100],
    ) is PolicyDecision.DENY


def test_guild_blank_policy_defaults_to_allowlist() -> None:
    assert evaluate_guild(
        "", guild_id=1, allow_guilds=[],
    ) is PolicyDecision.DENY
