"""tests/test_sidecar_guards.py -- guardrails for the agent sidecar.

The Node sidecar is deliberately stateless per turn: it opens one WebSocket,
runs one turn and closes. Conversation history, memory and traits all live in
the Python bot. The OpenRouter Agent SDK also offers a stateful mode -- a
persistent turn-state accessor and approval-gated tool pausing. Adopting it
would split one turn's state across two runtimes, which the bridge is
designed to avoid.

This test fails the build if that stateful surface appears in the sidecar
source, so the property is enforced and not merely documented. If a future
change genuinely needs it, that change must revisit the bridge design and
remove this guard deliberately.
"""
from __future__ import annotations

import os
import re

_SIDECAR_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "agent-sidecar", "src",
)

# SDK identifiers that switch the Agent SDK into its stateful mode.
_FORBIDDEN_TOKENS = ("StateAccessor", "requireApproval", "approveToolCalls")
# A bare `state:` key -- the shape used to hand persistent state to callModel.
# The lookbehind keeps unrelated identifiers such as `readyState` from
# matching.
_FORBIDDEN_PATTERNS = (re.compile(r"(?<![A-Za-z0-9_])state\s*:"),)


def _strip_comments(source: str) -> str:
    """Drop // line comments and /* */ block comments.

    The guard scans real code only: a comment that names the stateful APIs
    while explaining why they are banned must not trip it. Stripping can only
    remove text, so it never turns clean code into a false positive.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    source = re.sub(r"//[^\n]*", "", source)
    return source


def _sidecar_sources() -> list[tuple[str, str]]:
    """Every .ts file under agent-sidecar/src, as (name, contents) pairs."""
    out: list[tuple[str, str]] = []
    for name in sorted(os.listdir(_SIDECAR_SRC)):
        if name.endswith(".ts"):
            path = os.path.join(_SIDECAR_SRC, name)
            with open(path, encoding="utf-8") as handle:
                out.append((name, handle.read()))
    return out


def test_sidecar_src_is_present() -> None:
    """The directory the guard protects must exist and hold sources."""
    assert os.path.isdir(_SIDECAR_SRC), _SIDECAR_SRC
    assert _sidecar_sources(), "no .ts sources found in agent-sidecar/src"


def test_sidecar_stays_stateless() -> None:
    """The sidecar must not adopt the Agent SDK's stateful-mode surface."""
    offenders: list[str] = []
    for name, source in _sidecar_sources():
        code = _strip_comments(source)
        for token in _FORBIDDEN_TOKENS:
            if token in code:
                offenders.append(f"{name}: {token}")
        for pattern in _FORBIDDEN_PATTERNS:
            if pattern.search(code):
                offenders.append(f"{name}: /{pattern.pattern}/")
    assert not offenders, (
        "agent sidecar uses the Agent SDK stateful-mode surface ("
        + ", ".join(offenders)
        + "). The sidecar is intentionally stateless per turn -- stateful "
        "agent state belongs on the Python side of the bridge. See "
        "tests/test_sidecar_guards.py."
    )
