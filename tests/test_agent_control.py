"""tests/test_agent_control.py -- next-turn parameters and tool approval.

These cover :mod:`ai.agent_control` offline: the next-turn directive
validator and applier, and the tool-call approval gate. No Discord token,
database, model key or network is needed.
"""
from __future__ import annotations

import types

from ai.agent_control import (
    apply_next_turn,
    needs_approval,
    request_tool_approval,
    sanitize_next_turn,
    split_next_turn,
)


def test_sanitize_next_turn_whitelists_known_keys() -> None:
    cleaned = sanitize_next_turn({
        "model": "  openai/gpt-4o  ",
        "temperature": 0.2,
        "max_output_tokens": 256,
        "instructions": "be terse",
        "unknown": "dropped",
    })
    assert cleaned == {
        "model": "openai/gpt-4o",
        "temperature": 0.2,
        "max_output_tokens": 256,
        "instructions": "be terse",
    }


def test_sanitize_next_turn_rejects_bad_types_and_bounds() -> None:
    # A wrong type for a key drops that key silently.
    assert sanitize_next_turn({"model": 5}) is None
    assert sanitize_next_turn({"temperature": "hot"}) is None
    assert sanitize_next_turn({"max_output_tokens": 0}) is None
    # A bool is not accepted as a token count.
    assert sanitize_next_turn({"max_output_tokens": True}) is None
    # Temperature is clamped into range, never rejected for being extreme.
    assert sanitize_next_turn({"temperature": 9.0}) == {"temperature": 2.0}
    assert sanitize_next_turn({"temperature": -1.0}) == {"temperature": 0.0}
    # A non-dict directive, or one with nothing usable, is nothing.
    assert sanitize_next_turn("nope") is None
    assert sanitize_next_turn({}) is None


def test_split_next_turn_peels_directive() -> None:
    result, directive = split_next_turn({
        "answer": 42, "next_turn": {"temperature": 0.1},
    })
    assert result == {"answer": 42}
    assert directive == {"temperature": 0.1}
    # A result with no directive is returned untouched.
    plain, none = split_next_turn({"answer": 42})
    assert plain == {"answer": 42} and none is None
    # A non-dict result is passed straight through.
    assert split_next_turn("text") == ("text", None)


def test_apply_next_turn_updates_params_and_convo() -> None:
    convo: list[dict] = [{"role": "user", "content": "hi"}]
    model, temperature, max_tokens = apply_next_turn(
        {"model": "m2", "temperature": 0.3, "max_output_tokens": 128,
         "instructions": "stay terse"},
        convo=convo, model="m1", temperature=0.85, max_tokens=600,
    )
    assert (model, temperature, max_tokens) == ("m2", 0.3, 128)
    # Instructions ride in as an appended system message.
    assert convo[-1] == {"role": "system", "content": "stay terse"}
    # No directive leaves every parameter as it was.
    unchanged = apply_next_turn(
        None, convo=convo, model="m1", temperature=0.85, max_tokens=600,
    )
    assert unchanged == ("m1", 0.85, 600)


def test_needs_approval_honours_spec_flag(monkeypatch) -> None:
    from config import Config

    monkeypatch.setattr(Config, "AGENT_APPROVAL_TOOLS", [])
    monkeypatch.setattr(Config, "AGENT_APPROVAL_RISKS", [])

    gated = types.SimpleNamespace(
        name="x", risk="read", requires_approval=True)
    ungated = types.SimpleNamespace(
        name="y", risk="read", requires_approval=False)
    assert needs_approval(gated) is True
    assert needs_approval(ungated) is False
    assert needs_approval(None) is False


def test_needs_approval_honours_config(monkeypatch) -> None:
    from config import Config

    monkeypatch.setattr(Config, "AGENT_APPROVAL_TOOLS", ["files.write"])
    monkeypatch.setattr(Config, "AGENT_APPROVAL_RISKS", ["mutate"])

    by_name = types.SimpleNamespace(
        name="files.write", risk="read", requires_approval=False)
    by_risk = types.SimpleNamespace(
        name="files.delete", risk="mutate", requires_approval=False)
    ungated = types.SimpleNamespace(
        name="data.web_search", risk="read", requires_approval=False)
    assert needs_approval(by_name) is True
    assert needs_approval(by_risk) is True
    assert needs_approval(ungated) is False


async def test_request_tool_approval_denies_without_approver() -> None:
    """No approver means no human to ask, so a gated call is denied."""
    ctx = types.SimpleNamespace(approver=None)
    assert await request_tool_approval("files.write", {}, ctx) is False


async def test_request_tool_approval_uses_approver() -> None:
    async def yes(_name, _args) -> bool:
        return True

    async def no(_name, _args) -> bool:
        return False

    async def boom(_name, _args) -> bool:
        raise RuntimeError("approver crashed")

    assert await request_tool_approval(
        "t", {}, types.SimpleNamespace(approver=yes)) is True
    assert await request_tool_approval(
        "t", {}, types.SimpleNamespace(approver=no)) is False
    # An approver that raises fails closed: the call is denied, not run.
    assert await request_tool_approval(
        "t", {}, types.SimpleNamespace(approver=boom)) is False
