"""framework/pipeline/validation.py -- the Pydantic validation gate.

This is the bouncer. A tool envelope is run through a strict Pydantic model
before it is allowed any further down the pipeline. There is no best-effort
repair and no fallback: a structure either validates or it does not.

A structure that does not validate never reaches the model. It is rejected
and, in its place, a well-formed error envelope is returned describing the
failure -- so a malformed tool output is converted into a structured error
rather than silently corrupting the model's view of the world.

The model below mirrors :mod:`framework.pipeline.envelope` exactly:
``extra="forbid"`` means an envelope with an unexpected key fails the gate,
which is how schema drift is caught the moment it appears.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from framework.pipeline.envelope import STATUS_ERROR, STATUS_OK, error_envelope

log = logging.getLogger(__name__)


class EnvelopeError(BaseModel):
    """The ``error`` block of an error envelope: a code and a message."""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str

    @field_validator("code", "message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("must not be blank")
        return text


class ToolEnvelope(BaseModel):
    """The strict contract model for a tool result envelope."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "error"]
    tool: str
    version: str
    data: Any = None
    error: EnvelopeError | None = None
    meta: dict[str, Any] = {}

    @field_validator("tool", "version")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @model_validator(mode="after")
    def _status_is_consistent(self) -> "ToolEnvelope":
        """An ok envelope carries data and no error; an error one, the reverse."""
        if self.status == STATUS_OK and self.error is not None:
            raise ValueError("an ok envelope must not carry an error block")
        if self.status == STATUS_ERROR and self.error is None:
            raise ValueError("an error envelope must carry an error block")
        if self.status == STATUS_ERROR and self.data is not None:
            raise ValueError("an error envelope must not carry data")
        return self


def _format_errors(exc: ValidationError) -> str:
    """Collapse a Pydantic ValidationError into one short diagnostic string."""
    parts: list[str] = []
    for entry in exc.errors():
        loc = ".".join(str(piece) for piece in entry.get("loc", ())) or "envelope"
        parts.append(f"{loc}: {entry.get('msg', 'invalid')}")
    return "; ".join(parts)[:280]


def validate_envelope(raw: dict) -> dict:
    """Run an envelope through the gate. Always returns a clean envelope dict.

    On success the normalised envelope is returned (a plain dict, ready for
    the processing stage). On failure the input is rejected and a fresh error
    envelope is returned in its place -- the gate never lets a malformed
    structure through, and never raises.
    """
    try:
        model = ToolEnvelope.model_validate(raw)
    except ValidationError as exc:
        tool = ""
        if isinstance(raw, dict):
            tool = str(raw.get("tool") or "").strip()
        detail = _format_errors(exc)
        log.warning("tool envelope rejected by the gate (tool=%r): %s",
                    tool or "unknown", detail)
        return error_envelope(
            tool or "unknown",
            "schema_violation",
            f"tool output failed validation: {detail}",
            meta={"gate": "rejected"},
        )
    envelope = model.model_dump()
    envelope.setdefault("meta", {})
    envelope["meta"]["gate"] = "passed"
    return envelope
