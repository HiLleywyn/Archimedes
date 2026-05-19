"""ai/usage.py -- per-turn model usage ledger.

One :class:`TurnMeter` follows a single chat turn. Every model call -- the
chat model itself, and any model a tool invokes (vision, image, video) --
records a :class:`ModelCall` into it. At the end the meter renders a compact
footer showing the model, wall time, token count and cost, itemised per model
when a turn touched more than one.

The meter is carried on :class:`ai.tools.ToolContext` so a tool handler can
reach it, and stashed in the chat cog's ``out`` side-channel so the reply can
be stamped with the footer.
"""
from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class ModelCall:
    """One model invocation within a turn."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    label: str = ""

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def _short_model(model: str) -> str:
    """Drop the provider prefix: ``openai/gpt-4o-mini`` -> ``gpt-4o-mini``."""
    return model.rsplit("/", 1)[-1] if model else "unknown"


def _fmt_tokens(count: int) -> str:
    if count >= 1000:
        return f"{count / 1000:.1f}k"
    return str(count)


def _fmt_cost(cost: float) -> str:
    """Format a USD cost. Generation costs are tiny, so keep the figures."""
    if cost <= 0:
        return "$0"
    if cost < 0.01:
        return f"${cost:.4f}"
    if cost < 1:
        return f"${cost:.3f}"
    return f"${cost:.2f}"


class TurnMeter:
    """Collects every model call in one turn and renders a usage footer."""

    def __init__(self) -> None:
        self._calls: list[ModelCall] = []
        self._started = time.monotonic()

    def record(
        self,
        model: str | None,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0.0,
        label: str = "chat",
    ) -> None:
        """Record one model call."""
        self._calls.append(ModelCall(
            model=model or "unknown",
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            cost=float(cost or 0.0),
            label=label,
        ))

    def record_usage(
        self, model: str | None, usage: dict | None, *, label: str = "chat",
    ) -> None:
        """Record from a raw OpenAI/OpenRouter-style ``usage`` dict.

        Tolerates both the chat shape (``prompt_tokens``/``completion_tokens``)
        and the sidecar shape (``input_tokens``/``output_tokens``).
        """
        usage = usage or {}
        self.record(
            model,
            input_tokens=(usage.get("prompt_tokens")
                          or usage.get("input_tokens") or 0),
            output_tokens=(usage.get("completion_tokens")
                           or usage.get("output_tokens") or 0),
            cost=usage.get("cost") or 0.0,
            label=label,
        )

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._started

    @property
    def calls(self) -> list[ModelCall]:
        return list(self._calls)

    def total_tokens(self) -> int:
        return sum(call.tokens for call in self._calls)

    def total_cost(self) -> float:
        return sum(call.cost for call in self._calls)

    def footer_lines(self) -> list[str]:
        """Footer text as a list of lines (one per model, plus a total).

        A turn that recorded nothing yields just the wall time. A single-model
        turn is one line; a multi-model turn lists each call and a total.
        """
        elapsed = f"{self.elapsed_s:.1f}s"
        calls = self._calls
        if not calls:
            return [elapsed]

        if len(calls) == 1:
            call = calls[0]
            parts = [_short_model(call.model), elapsed]
            if call.tokens:
                parts.append(f"{_fmt_tokens(call.tokens)} tok")
            parts.append(_fmt_cost(call.cost))
            return [" · ".join(parts)]

        lines: list[str] = []
        for call in calls:
            parts = [_short_model(call.model)]
            if call.tokens:
                parts.append(f"{_fmt_tokens(call.tokens)} tok")
            parts.append(_fmt_cost(call.cost))
            lines.append(" · ".join(parts))
        total = ["total", elapsed]
        if self.total_tokens():
            total.append(f"{_fmt_tokens(self.total_tokens())} tok")
        total.append(_fmt_cost(self.total_cost()))
        lines.append(" · ".join(total))
        return lines

    def footer_text(self, prefix: str = "") -> str:
        """The footer as one string, ``prefix`` stamped on each line."""
        return "\n".join(prefix + line for line in self.footer_lines())
