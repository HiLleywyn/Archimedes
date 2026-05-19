"""tests/test_usage.py -- the per-turn usage ledger."""
from __future__ import annotations

import time

from ai.usage import TurnMeter


def test_single_call_footer_has_model_time_tokens_cost() -> None:
    meter = TurnMeter()
    meter._started = time.monotonic() - 2.0
    meter.record_usage(
        "openai/gpt-4o-mini",
        {"prompt_tokens": 800, "completion_tokens": 600, "cost": 0.0009},
    )
    lines = meter.footer_lines()
    assert len(lines) == 1
    line = lines[0]
    assert "gpt-4o-mini" in line       # model, provider prefix dropped
    assert "s" in line                 # wall time
    assert "tok" in line               # tokens
    assert "$0.0009" in line           # cost


def test_multi_step_lists_each_model_and_a_total() -> None:
    meter = TurnMeter()
    meter.record_usage("openai/gpt-4o-mini", {"cost": 0.0006})
    meter.record_usage("x-ai/grok-imagine", {"cost": 0.04}, label="image")
    meter.record_usage("openai/gpt-4o-mini", {"cost": 0.0011})
    lines = meter.footer_lines()
    # one line per call, plus a total line
    assert len(lines) == 4
    assert lines[-1].startswith("total")
    assert "grok-imagine" in lines[1]
    assert abs(meter.total_cost() - 0.0417) < 1e-9


def test_empty_meter_reports_only_time() -> None:
    meter = TurnMeter()
    lines = meter.footer_lines()
    assert len(lines) == 1
    assert lines[0].endswith("s")


def test_record_usage_tolerates_sidecar_token_keys() -> None:
    """The sidecar reports input_tokens/output_tokens, not prompt/completion."""
    meter = TurnMeter()
    meter.record_usage(
        "openai/gpt-4o-mini",
        {"input_tokens": 100, "output_tokens": 50, "cost": 0.0},
    )
    assert meter.total_tokens() == 150
