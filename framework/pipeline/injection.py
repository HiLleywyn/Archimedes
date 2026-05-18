"""framework/pipeline/injection.py -- the LLM injection formatter.

The final gate before the model. A processed envelope still carries
machinery the model has no business seeing: the contract ``version``, the
wrap timestamp, the gate verdict, stage bookkeeping. This layer strips all of
it and emits the smallest clean structure that still answers the model's
question.

What the model is shown:

  * ``tool``   -- which tool produced this,
  * ``status`` -- ok or error,
  * ``data``   -- the payload (on success only),
  * ``error``  -- the failure message (on error only),
  * ``notes``  -- a short list of what the pipeline trimmed, when it trimmed
    anything, so the model knows it is looking at a bounded view.

Everything else -- ``version``, ``meta``, timings, gate and stage records --
is internal noise and is dropped here. The result is serialised to compact
JSON and hard-capped, so one tool result can never blow the context window.
"""
from __future__ import annotations

import json

from framework.pipeline.envelope import STATUS_ERROR

# Hard ceiling on the serialised tool message handed back to the model.
DEFAULT_MAX_CHARS = 4000


def _notes(envelope: dict) -> list[str]:
    """The pipeline's trim notes, if any -- the one piece of meta worth showing."""
    meta = envelope.get("meta")
    if not isinstance(meta, dict):
        return []
    pipeline = meta.get("pipeline")
    if not isinstance(pipeline, dict):
        return []
    notes = pipeline.get("notes")
    return [str(note) for note in notes] if isinstance(notes, list) else []


def format_envelope(envelope: dict, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Reduce a processed envelope to the minimal JSON string the model sees.

    Strips ``version``, ``meta`` and every other internal field. Returns
    compact JSON, hard-capped at ``max_chars`` -- a tool result can never
    exceed that, whatever the tool did.
    """
    status = envelope.get("status")
    view: dict = {
        "tool": str(envelope.get("tool") or "unknown"),
        "status": status,
    }

    if status == STATUS_ERROR:
        error = envelope.get("error")
        if isinstance(error, dict):
            view["error"] = error.get("message") or error.get("code") or "failed"
        else:
            view["error"] = str(error) if error else "failed"
    else:
        view["data"] = envelope.get("data")

    notes = _notes(envelope)
    if notes:
        view["notes"] = notes

    text = json.dumps(view, default=str, ensure_ascii=False,
                       separators=(",", ":"))
    if len(text) > max_chars:
        text = text[:max_chars] + " ...(tool output truncated)"
    return text
