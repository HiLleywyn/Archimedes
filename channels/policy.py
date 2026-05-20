"""channels/policy.py -- DM and guild access policies.

A channel decides whether an inbound message is allowed before the agent
ever sees it. The policy choices match OpenClaw's documented vocabulary:

  * DM policy:    ``open`` | ``allowlist`` | ``disabled``
  * Guild policy: ``open`` | ``allowlist`` | ``disabled``

``allowlist`` is the secure default. The list of allowed ids lives in
the environment (``DISCORD_DM_ALLOW_USERS``, ``DISCORD_GUILD_ALLOW``)
because at this layer the policy is a static config concern; per-guild
overrides remain inside ``guild_settings`` and the cog reads them
separately.

This module is pure logic -- no Discord types, no database. Channels
hand it the relevant ids and the env-derived lists; it returns one of
the three ``PolicyDecision`` outcomes.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum

from config import _env_list


class DMPolicy(str, Enum):
    OPEN = "open"
    ALLOWLIST = "allowlist"
    DISABLED = "disabled"


class GuildPolicy(str, Enum):
    OPEN = "open"
    ALLOWLIST = "allowlist"
    DISABLED = "disabled"


class PolicyDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


def _parse_dm(policy: str) -> DMPolicy:
    try:
        return DMPolicy((policy or "allowlist").lower())
    except ValueError:
        return DMPolicy.ALLOWLIST


def _parse_guild(policy: str) -> GuildPolicy:
    try:
        return GuildPolicy((policy or "allowlist").lower())
    except ValueError:
        return GuildPolicy.ALLOWLIST


def evaluate_dm(
    policy: str,
    *,
    user_id: int,
    allow_users: list[int] | None = None,
    owner_id: int = 0,
) -> PolicyDecision:
    """Decide whether to accept a DM. The bot owner is always allowed; the
    allowlist is taken from the environment when the caller passes None.
    """
    mode = _parse_dm(policy)
    if mode is DMPolicy.DISABLED:
        return PolicyDecision.DENY
    if mode is DMPolicy.OPEN:
        return PolicyDecision.ALLOW
    if owner_id and int(user_id) == int(owner_id):
        return PolicyDecision.ALLOW
    allowed = allow_users
    if allowed is None:
        allowed = _ids_from_env("DISCORD_DM_ALLOW_USERS")
    return (
        PolicyDecision.ALLOW
        if int(user_id) in allowed
        else PolicyDecision.DENY
    )


def evaluate_guild(
    policy: str,
    *,
    guild_id: int,
    allow_guilds: list[int] | None = None,
) -> PolicyDecision:
    """Decide whether to accept a guild message. The allowlist defaults
    from ``DISCORD_GUILD_ALLOW`` when none is supplied."""
    mode = _parse_guild(policy)
    if mode is GuildPolicy.DISABLED:
        return PolicyDecision.DENY
    if mode is GuildPolicy.OPEN:
        return PolicyDecision.ALLOW
    allowed = allow_guilds
    if allowed is None:
        allowed = _ids_from_env("DISCORD_GUILD_ALLOW")
    return (
        PolicyDecision.ALLOW
        if int(guild_id) in allowed
        else PolicyDecision.DENY
    )


def _ids_from_env(key: str) -> list[int]:
    out: list[int] = []
    for item in _env_list(key):
        try:
            out.append(int(item))
        except ValueError:
            continue
    return out
