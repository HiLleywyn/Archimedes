"""ai/agent_control.py -- per-turn agent controls: next-turn parameters and
tool-call approval.

Two cross-cutting agent features live here, kept out of :mod:`ai.tools` and
:mod:`ai.agent_sidecar` so the in-process loop and the sidecar bridge share
one implementation:

  * **Next-turn parameters.** A tool may steer the next model turn by
    returning a ``next_turn`` block in its result -- a different model, a new
    temperature, a tighter token budget, or extra instructions. This is the
    Python-native shape of the OpenRouter Agent SDK's ``nextTurnParams``: the
    tool decides at run time, the orchestrator applies the change before the
    model is asked again. :func:`split_next_turn` peels the directive off a
    raw tool return; :func:`apply_next_turn` folds it into the in-process
    loop's working parameters. The sidecar forwards the same directive over
    the bridge for the SDK's ``nextTurnParams`` to consume.

  * **Tool-call approval.** A tool call may be gated on a human yes/no before
    it runs. :func:`needs_approval` decides whether a given tool is gated --
    by an explicit ``requires_approval`` flag on the tool, by tool name, or
    by risk tier, the latter two driven by config. :func:`request_tool_approval`
    asks the turn's approver (a button prompt in the channel, supplied by the
    chat cog through :class:`ai.tools.ToolContext`) and returns the decision.
    When no approver is reachable a gated call is denied, never silently run.

Neither feature reaches for the Agent SDK's persistent state surface: a
next-turn directive rides the existing tool-result frame within one turn, and
approval is resolved entirely on the Python side before the turn ends. The
sidecar stays stateless per turn.
"""
from __future__ import annotations

import logging

from config import Config

log = logging.getLogger(__name__)

# Parameters a tool may change for the next model turn. Kept deliberately
# small: every key here is one both the sidecar and the in-process loop can
# honour identically, so a directive behaves the same on either path.
NEXT_TURN_KEYS = ("model", "instructions", "temperature", "max_output_tokens")

# Hard ceilings on a next-turn directive. Bot tool handlers are trusted, but a
# Lua plugin tool is less so -- these bound a directive to sane values.
_MAX_INSTRUCTION_CHARS = 8000
_MAX_OUTPUT_TOKENS = 8000


def sanitize_next_turn(raw) -> dict | None:
    """Validate a raw ``next_turn`` directive into a clean, bounded dict.

    Returns the subset of :data:`NEXT_TURN_KEYS` that carried a well-typed,
    in-range value, or ``None`` when nothing usable remains. An unknown key, a
    wrong type or an out-of-range number is dropped silently -- a malformed
    directive must never break the turn.
    """
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    for key in NEXT_TURN_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key in ("model", "instructions"):
            if isinstance(value, str) and value.strip():
                text = value.strip()
                if key == "instructions":
                    text = text[:_MAX_INSTRUCTION_CHARS]
                out[key] = text
        elif key == "temperature":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = max(0.0, min(2.0, float(value)))
        elif key == "max_output_tokens":
            if (isinstance(value, int) and not isinstance(value, bool)
                    and value > 0):
                out[key] = min(int(value), _MAX_OUTPUT_TOKENS)
    return out or None


def split_next_turn(result):
    """Peel a ``next_turn`` directive off a raw tool return.

    Returns ``(result_without_next_turn, directive_or_None)``. The directive
    is removed from the result so it never reaches the execution pipeline or
    the model -- it is turn-control data, not tool output.
    """
    if not isinstance(result, dict) or "next_turn" not in result:
        return result, None
    cleaned = {key: value for key, value in result.items()
               if key != "next_turn"}
    return cleaned, sanitize_next_turn(result.get("next_turn"))


def apply_next_turn(
    directive: dict | None,
    *,
    convo: list[dict],
    model: str | None,
    temperature: float,
    max_tokens: int,
) -> tuple[str | None, float, int]:
    """Fold a next-turn directive into the in-process loop's parameters.

    ``convo`` is mutated in place when the directive carries ``instructions``
    (appended as a system message, mirroring how the sidecar forwards
    ``instructions`` to the SDK). The model, temperature and token budget are
    returned updated so the caller can rebind its working values.
    """
    if not directive:
        return model, temperature, max_tokens
    if directive.get("model"):
        model = directive["model"]
    if directive.get("temperature") is not None:
        temperature = directive["temperature"]
    if directive.get("max_output_tokens"):
        max_tokens = directive["max_output_tokens"]
    instructions = directive.get("instructions")
    if instructions:
        convo.append({"role": "system", "content": instructions})
    return model, temperature, max_tokens


def needs_approval(spec) -> bool:
    """Is a tool call gated on a human yes/no before it may run?

    A tool is gated when its :class:`~ai.tools.ToolSpec` sets
    ``requires_approval``, when its name is listed in ``AGENT_APPROVAL_TOOLS``,
    or when its risk tier is listed in ``AGENT_APPROVAL_RISKS``. With both
    config lists empty -- the default -- approval is off and no call is gated.
    """
    if spec is None:
        return False
    if getattr(spec, "requires_approval", False):
        return True
    name = getattr(spec, "name", "")
    if name and name in Config.AGENT_APPROVAL_TOOLS:
        return True
    risk = getattr(spec, "risk", "")
    if risk and risk in Config.AGENT_APPROVAL_RISKS:
        return True
    return False


async def request_tool_approval(name: str, args: dict, ctx) -> bool:
    """Ask the turn's approver to clear one gated tool call.

    The approver -- set on :class:`~ai.tools.ToolContext` by the chat cog --
    surfaces a human prompt and resolves to the decision. When no approver is
    reachable the call is denied: a gated tool is never run unreviewed.
    """
    approver = getattr(ctx, "approver", None)
    if approver is None:
        log.info("tool approval: %s denied (no approver available)", name)
        return False
    try:
        decided = bool(await approver(name, args))
    except Exception as exc:  # noqa: BLE001
        log.warning("tool approval for %s failed: %s", name, exc)
        return False
    log.info("tool approval: %s %s", name,
             "approved" if decided else "rejected")
    return decided
