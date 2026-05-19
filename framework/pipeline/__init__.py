"""framework/pipeline -- the controlled tool-execution pipeline.

A tool result does not go straight from a handler to the model. It travels a
fixed, layered path, and every layer is deterministic machinery:

    raw tool return
      -> envelope    wrap into the strict contract shape
      -> validation  the Pydantic gate: pass, or become a structured error
      -> processing  schema filter, deterministic compression
      -> injection   strip internal noise, emit minimal clean JSON
      -> the model

The contract is rigid and the pipeline is deterministic, so the model's view
of a tool result is predictable no matter what the tool did. This package is
the single place that path is defined.

The pieces:

* :mod:`envelope`   -- the tool contract: the fixed ``status`` / ``tool`` /
  ``version`` / ``data`` / ``error`` / ``meta`` envelope.
* :mod:`validation` -- the Pydantic validation gate that hard-rejects any
  malformed envelope.
* :mod:`processing` -- the deterministic processing stages: schema filtering
  and bounded compression.
* :mod:`injection`  -- the formatter that reduces a processed envelope to the
  minimal JSON the model is shown.
* :mod:`transforms` -- pure deterministic functions behind the ``transform.*``
  agent tools.

:func:`run_pipeline` runs the whole path and is what the orchestrator calls.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import Config
from framework.pipeline.envelope import (
    ENVELOPE_VERSION,
    error_envelope,
    is_envelope,
    ok_envelope,
    wrap_result,
)
from framework.pipeline.injection import format_envelope
from framework.pipeline.processing import ProcessingOptions, process_envelope
from framework.pipeline.validation import validate_envelope

__all__ = [
    "ENVELOPE_VERSION",
    "PipelineResult",
    "ProcessingOptions",
    "error_envelope",
    "format_envelope",
    "is_envelope",
    "ok_envelope",
    "process_envelope",
    "run_pipeline",
    "validate_envelope",
    "wrap_result",
]


@dataclass
class PipelineResult:
    """The outcome of one full pipeline run.

    ``envelope`` is the validated, processed contract envelope (the internal
    record). ``injected`` is the compact JSON string the orchestrator hands
    back to the model -- meta and version already stripped.
    """

    envelope: dict
    injected: str
    status: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def data(self):
        """The processed payload, or ``None`` for an error result."""
        return self.envelope.get("data")


def _options(result_fields: tuple[str, ...] | None,
             verbatim: bool) -> ProcessingOptions:
    """Build the processing options, taking compression caps from Config.

    A ``verbatim`` tool -- one whose output is the whole point, like a
    workspace file read or a shell capture -- is exempt from string and list
    compression: its caps are lifted to the verbatim ceiling, so the model
    sees the result whole instead of trimmed down to a snippet.
    """
    if verbatim:
        cap = max(64, Config.PIPELINE_VERBATIM_MAX_CHARS)
        return ProcessingOptions(
            max_string=cap, max_list=cap, result_fields=result_fields,
        )
    return ProcessingOptions(
        max_string=max(64, Config.PIPELINE_MAX_STRING),
        max_list=max(1, Config.PIPELINE_MAX_LIST),
        result_fields=result_fields,
    )


def run_pipeline(
    tool: str,
    raw,
    *,
    meta: dict | None = None,
    result_fields: tuple[str, ...] | None = None,
    options: ProcessingOptions | None = None,
    verbatim: bool = False,
) -> PipelineResult:
    """Run a raw tool return value through the whole pipeline.

    ``tool`` is the tool name, ``raw`` whatever its handler returned. ``meta``
    is merged into the envelope's bookkeeping (the orchestrator passes timing
    and round number). ``result_fields``, when given, is the tool's declared
    result schema -- the processing stage filters ``data`` to those fields.

    ``verbatim`` lifts the compression caps and the injection ceiling for a
    tool whose output must not be trimmed (a file read, a shell capture), so
    its result reaches the model whole.

    The return value is a :class:`PipelineResult`; ``.injected`` is the string
    to put in the ``role: tool`` message.
    """
    envelope = wrap_result(tool, raw, meta=meta)
    envelope = validate_envelope(envelope)
    if options is None:
        opts = _options(result_fields, verbatim)
    else:
        opts = options
        if result_fields is not None:
            opts.result_fields = result_fields
    envelope = process_envelope(envelope, opts)
    inject_max = (Config.PIPELINE_VERBATIM_MAX_CHARS if verbatim
                  else Config.PIPELINE_INJECT_MAX_CHARS)
    injected = format_envelope(envelope, max_chars=max(256, inject_max))
    return PipelineResult(
        envelope=envelope,
        injected=injected,
        status=str(envelope.get("status") or "error"),
    )
