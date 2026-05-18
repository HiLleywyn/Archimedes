"""framework/pipeline/processing.py -- the deterministic tool-output pipeline.

A validated envelope passes through ordered, deterministic stages before it
is shown to the model. This layer decides what reality the model sees.

Stages, in order:

  1. schema filtering        -- drop fields a tool did not declare it returns
  2. deterministic compression -- bound string length, list size and nesting
  3. summarisation           -- optional, strictly controlled, off by default

Stages 1 and 2 are pure: given the same envelope and options they always
produce the same result, with no model and no I/O. That is the whole point
-- compression here is mechanical, never a guess, so it cannot hallucinate.

Stage 3 exists for the rare case where a payload is large *and* unstructured.
It is disabled unless a summariser callable is explicitly supplied, and even
then it only ever runs on whole string fields. The default pipeline never
summarises.

Every transformation is recorded as a short human-readable note in
``meta.pipeline.notes`` so the injection formatter can tell the model, in one
line, what was trimmed. Nothing is dropped silently.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from framework.pipeline.envelope import STATUS_ERROR

# Defaults. Compression that is too aggressive starves the model of detail;
# compression that is too loose floods the context window. These are the
# tuned middle ground; the orchestrator may override them per call.
DEFAULT_MAX_STRING = 1200
DEFAULT_MAX_LIST = 25
DEFAULT_MAX_DEPTH = 8


@dataclass
class ProcessingOptions:
    """Knobs for one run of the processing pipeline."""

    max_string: int = DEFAULT_MAX_STRING
    max_list: int = DEFAULT_MAX_LIST
    max_depth: int = DEFAULT_MAX_DEPTH
    # When set, schema filtering keeps only these top-level keys of an ok
    # envelope's data object. None means the tool declared no result schema,
    # so filtering is skipped.
    result_fields: tuple[str, ...] | None = None
    # An optional, strictly-controlled summariser: ``fn(text) -> str``. Left
    # unset, stage 3 is a no-op. The default pipeline never summarises.
    summarizer: object | None = None
    # A string field longer than this is offered to the summariser (when one
    # is set). Below it, plain compression handles the field.
    summarize_over: int = 4000


@dataclass
class _Stats:
    """Mutable tally of what compression touched, turned into notes at the end."""

    strings_truncated: int = 0
    lists_capped: list[tuple[int, int]] = field(default_factory=list)
    depth_trimmed: int = 0
    summarised: int = 0

    def notes(self) -> list[str]:
        out: list[str] = []
        if self.strings_truncated:
            out.append(
                f"truncated {self.strings_truncated} long text "
                f"field{'s' if self.strings_truncated != 1 else ''}"
            )
        for kept, total in self.lists_capped:
            out.append(f"showed {kept} of {total} list items")
        if self.depth_trimmed:
            out.append(
                f"trimmed {self.depth_trimmed} deeply nested "
                f"branch{'es' if self.depth_trimmed != 1 else ''}"
            )
        if self.summarised:
            out.append(
                f"summarised {self.summarised} oversized text "
                f"field{'s' if self.summarised != 1 else ''}"
            )
        return out

    @property
    def touched(self) -> bool:
        return bool(
            self.strings_truncated or self.lists_capped
            or self.depth_trimmed or self.summarised
        )


def _filter_schema(data, fields: tuple[str, ...] | None):
    """Stage 1: keep only declared top-level fields of a data object."""
    if fields is None or not isinstance(data, dict):
        return data, False
    allowed = set(fields)
    kept = {key: value for key, value in data.items() if key in allowed}
    dropped = len(data) - len(kept)
    return kept, dropped > 0


def _compress(value, opts: ProcessingOptions, stats: _Stats, depth: int):
    """Stage 2: bound string length, list size and nesting depth, recursively."""
    if depth > opts.max_depth:
        stats.depth_trimmed += 1
        return "<nested data trimmed>"

    if isinstance(value, str):
        summarizer = opts.summarizer
        if summarizer is not None and len(value) > opts.summarize_over:
            try:
                summary = summarizer(value)
            except Exception:  # noqa: BLE001 -- a bad summariser must not break the pipeline
                summary = None
            if isinstance(summary, str) and summary:
                stats.summarised += 1
                return _compress(summary, opts, stats, depth)
        if len(value) > opts.max_string:
            stats.strings_truncated += 1
            dropped = len(value) - opts.max_string
            return value[:opts.max_string] + f" ...(+{dropped} chars)"
        return value

    if isinstance(value, list):
        total = len(value)
        rows = value
        if total > opts.max_list:
            rows = value[:opts.max_list]
            stats.lists_capped.append((opts.max_list, total))
        return [_compress(item, opts, stats, depth + 1) for item in rows]

    if isinstance(value, dict):
        return {
            str(key): _compress(item, opts, stats, depth + 1)
            for key, item in value.items()
        }

    return value


def process_envelope(
    envelope: dict, options: ProcessingOptions | None = None,
) -> dict:
    """Run a validated envelope through the deterministic processing stages.

    Returns a new envelope dict. The structure (status / tool / version /
    error) is preserved exactly; only ``data`` is filtered and compressed,
    and ``meta.pipeline`` records every stage that ran.
    """
    opts = options or ProcessingOptions()
    out = dict(envelope)
    out["meta"] = dict(envelope.get("meta") or {})
    stats = _Stats()
    stages: list[str] = []

    if out.get("status") == STATUS_ERROR:
        # An error envelope has no data; only keep its message bounded so a
        # giant traceback cannot still blow the context window.
        err = dict(out.get("error") or {})
        message = str(err.get("message", ""))
        if len(message) > opts.max_string:
            err["message"] = message[:opts.max_string] + " ...(truncated)"
            stats.strings_truncated += 1
        out["error"] = err
        stages.append("error-bounded")
    else:
        data = out.get("data")
        data, filtered = _filter_schema(data, opts.result_fields)
        if filtered:
            stages.append("schema-filter")
        data = _compress(data, opts, stats, depth=0)
        stages.append("compress")
        if stats.summarised:
            stages.append("summarise")
        out["data"] = data

    out["meta"]["pipeline"] = {
        "stages": stages,
        "compressed": stats.touched,
        "notes": stats.notes(),
    }
    return out
