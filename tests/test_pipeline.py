"""tests/test_pipeline.py -- offline tests for the tool-execution pipeline.

These exercise every layer of :mod:`framework.pipeline` -- the contract
envelope, the Pydantic validation gate, the deterministic processing stages,
the injection formatter and the transform functions -- plus the end-to-end
``run_pipeline`` path. They need no network, database or model.
"""
from __future__ import annotations

import json

from framework.pipeline import (
    PipelineResult,
    run_pipeline,
)
from framework.pipeline.envelope import (
    ENVELOPE_VERSION,
    error_envelope,
    is_envelope,
    ok_envelope,
    wrap_result,
)
from framework.pipeline.injection import format_envelope
from framework.pipeline.processing import ProcessingOptions, process_envelope
from framework.pipeline.transforms import aggregate, project_fields, slice_items
from framework.pipeline.validation import validate_envelope


# ── the contract envelope ─────────────────────────────────────────────────────
def test_wrap_result_shapes_ok_error_and_scalars() -> None:
    ok = wrap_result("demo", {"answer": 42})
    assert ok["status"] == "ok"
    assert ok["tool"] == "demo"
    assert ok["version"] == ENVELOPE_VERSION
    assert ok["data"] == {"answer": 42}
    assert ok["error"] is None

    # The long-standing {"error": "..."} convention becomes an error envelope.
    bad = wrap_result("demo", {"error": "query is required"})
    assert bad["status"] == "error"
    assert bad["data"] is None
    assert bad["error"]["message"] == "query is required"
    assert bad["error"]["code"] == "tool_error"

    # An explicit code is honoured.
    coded = wrap_result("demo", {"error": "nope", "code": "denied"})
    assert coded["error"]["code"] == "denied"

    # A non-dict return is wrapped so data is always an object.
    scalar = wrap_result("demo", "heads")
    assert scalar["data"] == {"value": "heads"}
    assert wrap_result("demo", None)["data"] == {}


def test_wrap_result_passes_through_an_existing_envelope() -> None:
    original = ok_envelope("inner", {"x": 1})
    rewrapped = wrap_result("outer", original, meta={"round": 3})
    assert rewrapped["tool"] == "inner"  # the envelope's own tool wins
    assert rewrapped["data"] == {"x": 1}
    assert rewrapped["meta"]["round"] == 3


def test_is_envelope_distinguishes_envelopes_from_raw_dicts() -> None:
    assert is_envelope(ok_envelope("t", {}))
    assert is_envelope(error_envelope("t", "c", "m"))
    assert not is_envelope({"error": "just a raw tool error"})
    assert not is_envelope({"status": "ok"})  # missing the rest of the keys
    assert not is_envelope("a string")


# ── the Pydantic validation gate ──────────────────────────────────────────────
def test_validation_gate_passes_a_well_formed_envelope() -> None:
    clean = validate_envelope(ok_envelope("demo", {"a": 1}))
    assert clean["status"] == "ok"
    assert clean["meta"]["gate"] == "passed"


def test_validation_gate_rejects_inconsistent_status() -> None:
    # status error with no error block is a contract violation.
    broken = {
        "status": "error", "tool": "demo", "version": "1",
        "data": None, "error": None, "meta": {},
    }
    rejected = validate_envelope(broken)
    assert rejected["status"] == "error"
    assert rejected["error"]["code"] == "schema_violation"

    # status ok carrying an error block is equally rejected.
    contradictory = {
        "status": "ok", "tool": "demo", "version": "1", "data": {},
        "error": {"code": "c", "message": "m"}, "meta": {},
    }
    assert validate_envelope(contradictory)["error"]["code"] == "schema_violation"


def test_validation_gate_rejects_schema_drift() -> None:
    # An unexpected top-level key is how drift shows up -- the gate forbids it.
    drifted = {
        "status": "ok", "tool": "demo", "version": "1", "data": {},
        "error": None, "meta": {}, "surprise": True,
    }
    rejected = validate_envelope(drifted)
    assert rejected["status"] == "error"
    assert rejected["error"]["code"] == "schema_violation"

    # A blank tool name is rejected too.
    blank = {
        "status": "ok", "tool": "  ", "version": "1", "data": {},
        "error": None, "meta": {},
    }
    assert validate_envelope(blank)["error"]["code"] == "schema_violation"


def test_validation_gate_rejects_non_dict_input() -> None:
    assert validate_envelope("not an envelope")["status"] == "error"
    assert validate_envelope([1, 2, 3])["error"]["code"] == "schema_violation"


# ── the deterministic processing pipeline ─────────────────────────────────────
def test_processing_truncates_long_strings_and_notes_it() -> None:
    env = ok_envelope("demo", {"blob": "x" * 5000})
    out = process_envelope(env, ProcessingOptions(max_string=100))
    assert out["data"]["blob"].startswith("x" * 100)
    assert "(+4900 chars)" in out["data"]["blob"]
    assert out["meta"]["pipeline"]["compressed"] is True
    assert any("truncated" in note for note in out["meta"]["pipeline"]["notes"])


def test_processing_caps_long_lists_and_reports_the_total() -> None:
    env = ok_envelope("demo", {"rows": list(range(100))})
    out = process_envelope(env, ProcessingOptions(max_list=10))
    assert len(out["data"]["rows"]) == 10
    assert any("10 of 100" in note for note in out["meta"]["pipeline"]["notes"])


def test_processing_filters_to_declared_fields() -> None:
    env = ok_envelope("demo", {"keep": 1, "also": 2, "drift": 3})
    out = process_envelope(env, ProcessingOptions(result_fields=("keep", "also")))
    assert out["data"] == {"keep": 1, "also": 2}
    assert "schema-filter" in out["meta"]["pipeline"]["stages"]


def test_processing_trims_excessive_nesting() -> None:
    env = ok_envelope("demo", {"l1": {"l2": {"l3": {"l4": "deep"}}}})
    out = process_envelope(env, ProcessingOptions(max_depth=2))
    assert out["data"]["l1"]["l2"]["l3"] == "<nested data trimmed>"
    assert out["meta"]["pipeline"]["compressed"] is True


def test_processing_bounds_an_error_message() -> None:
    env = error_envelope("demo", "boom", "y" * 3000)
    out = process_envelope(env, ProcessingOptions(max_string=50))
    assert len(out["error"]["message"]) < 100
    assert out["error"]["message"].endswith("...(truncated)")


# ── the injection formatter ───────────────────────────────────────────────────
def test_injection_strips_internal_fields() -> None:
    env = process_envelope(ok_envelope("demo", {"a": 1}), ProcessingOptions())
    text = format_envelope(env)
    payload = json.loads(text)
    assert payload == {"tool": "demo", "status": "ok", "data": {"a": 1}}
    assert "version" not in text
    assert "meta" not in text
    assert "wrapped_at" not in text


def test_injection_surfaces_compression_notes() -> None:
    env = process_envelope(
        ok_envelope("demo", {"rows": list(range(100))}),
        ProcessingOptions(max_list=5),
    )
    payload = json.loads(format_envelope(env))
    assert payload["notes"]
    assert any("of 100" in note for note in payload["notes"])


def test_injection_formats_an_error_compactly() -> None:
    env = process_envelope(error_envelope("demo", "denied", "no access"),
                           ProcessingOptions())
    payload = json.loads(format_envelope(env))
    assert payload["status"] == "error"
    assert payload["error"] == "no access"
    assert "data" not in payload


def test_injection_hard_caps_the_output() -> None:
    env = ok_envelope("demo", {"blob": "z" * 100000})
    text = format_envelope(env, max_chars=500)
    assert len(text) <= 540
    assert text.endswith("...(tool output truncated)")


# ── the deterministic transform functions ─────────────────────────────────────
def test_transform_slice_takes_top_n() -> None:
    out = slice_items([5, 4, 3, 2, 1], 3)
    assert out == {"items": [5, 4, 3], "returned": 3, "total": 5}


def test_transform_slice_sorts_by_key() -> None:
    rows = [{"name": "a", "score": 10}, {"name": "b", "score": 30},
            {"name": "c", "score": 20}]
    top = slice_items(rows, 2, key="score", order="desc")
    assert [r["name"] for r in top["items"]] == ["b", "c"]
    bottom = slice_items(rows, 2, key="score", order="asc")
    assert [r["name"] for r in bottom["items"]] == ["a", "c"]


def test_transform_slice_rejects_bad_input() -> None:
    assert "error" in slice_items("not a list", 3)
    assert "error" in slice_items([1, 2], "lots")
    assert "error" in slice_items([1, 2], -1)


def test_transform_project_keeps_only_named_fields() -> None:
    rows = [{"a": 1, "b": 2, "c": 3}, {"a": 4, "b": 5, "c": 6}]
    out = project_fields(rows, ["a", "c"])
    assert out["items"] == [{"a": 1, "c": 3}, {"a": 4, "c": 6}]
    assert "error" in project_fields(rows, [])
    assert "error" in project_fields("not a list", ["a"])


def test_transform_aggregate_computes_metrics() -> None:
    nums = [10, 20, 30, 40]
    assert aggregate(nums, op="sum")["value"] == 100
    assert aggregate(nums, op="mean")["value"] == 25
    assert aggregate(nums, op="min")["value"] == 10
    assert aggregate(nums, op="max")["value"] == 40
    assert aggregate(nums, op="count")["value"] == 4

    rows = [{"n": 1}, {"n": 2}, {"n": "skip"}]
    summed = aggregate(rows, field="n", op="sum")
    assert summed["value"] == 3
    assert summed["skipped"] == 1

    assert "error" in aggregate([], op="sum")
    assert "error" in aggregate([1, 2], op="median")


# ── the end-to-end pipeline ───────────────────────────────────────────────────
def test_run_pipeline_handles_a_successful_result() -> None:
    piped = run_pipeline("demo", {"answer": "yes"}, meta={"round": 1})
    assert isinstance(piped, PipelineResult)
    assert piped.ok
    payload = json.loads(piped.injected)
    assert payload["data"] == {"answer": "yes"}
    assert payload["tool"] == "demo"


def test_run_pipeline_converts_a_failure_into_a_structured_error() -> None:
    piped = run_pipeline("demo", {"error": "query is required"})
    assert not piped.ok
    assert piped.status == "error"
    payload = json.loads(piped.injected)
    assert payload["status"] == "error"
    assert payload["error"] == "query is required"


def test_run_pipeline_filters_to_declared_result_fields() -> None:
    piped = run_pipeline(
        "demo", {"keep": 1, "drift": 2}, result_fields=("keep",),
    )
    assert piped.data == {"keep": 1}


def test_run_pipeline_compresses_an_oversized_result() -> None:
    piped = run_pipeline("demo", {"rows": ["item " + "x" * 4000]})
    assert len(piped.injected) <= 4100
    notes = piped.envelope["meta"]["pipeline"]["notes"]
    assert notes  # the compression that happened was recorded


def test_run_pipeline_verbatim_skips_compression() -> None:
    # A verbatim tool's result is exempt from string and list compression: a
    # blob well past every default cap reaches the model whole and untrimmed.
    blob = "x" * 50000
    piped = run_pipeline("files.read", {"content": blob}, verbatim=True)
    payload = json.loads(piped.injected)
    assert payload["data"]["content"] == blob
    assert "notes" not in payload  # nothing was trimmed
    assert not piped.envelope["meta"]["pipeline"]["compressed"]


def test_run_pipeline_verbatim_keeps_long_lists_whole() -> None:
    piped = run_pipeline("files.grep", {"rows": list(range(500))},
                         verbatim=True)
    payload = json.loads(piped.injected)
    assert payload["data"]["rows"] == list(range(500))
