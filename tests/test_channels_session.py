"""tests/test_channels_session.py -- session key derivation."""
from __future__ import annotations

from channels.session import (
    channel_session_key, dm_session_key, parse_session_key,
    thread_session_key,
)


def test_dm_session_key_shape() -> None:
    assert dm_session_key("discord", 12345) == "arch:discord:dm:12345"


def test_channel_session_key_shape() -> None:
    assert channel_session_key("discord", 67890) == "arch:discord:channel:67890"


def test_thread_session_key_shape() -> None:
    assert thread_session_key("discord", 42) == "arch:discord:thread:42"


def test_session_keys_are_transport_namespaced() -> None:
    # Two transports with overlapping numeric ids must produce different
    # keys so their bubbles never merge.
    a = channel_session_key("discord", 100)
    b = channel_session_key("web", 100)
    assert a != b


def test_parse_session_key_round_trips() -> None:
    key = channel_session_key("discord", 999)
    scheme, transport, kind, ident = parse_session_key(key)
    assert scheme == "arch"
    assert transport == "discord"
    assert kind == "channel"
    assert ident == "999"


def test_parse_session_key_returns_blanks_for_garbage() -> None:
    assert parse_session_key("") == ("", "", "", "")
    assert parse_session_key("not a session") == ("", "", "", "")
