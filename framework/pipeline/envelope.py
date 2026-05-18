"""framework/pipeline/envelope.py -- the tool contract: a strict result envelope.

This is the spine of the execution pipeline. Every tool result, whatever
produced it -- a generic Python tool, a Lua plugin handler, a failed call --
is wrapped into one fixed shape before anything downstream sees it::

    {
      "status":  "ok" | "error",   -- the only two outcomes
      "tool":    "<tool name>",    -- who produced this
      "version": "<contract ver>", -- the envelope contract version
      "data":    <payload> | None, -- present on ok, None on error
      "error":   {code, message} | None,
      "meta":    { ... },          -- timing and pipeline bookkeeping
    }

The envelope is *versioned and never mutated*. Downstream stages -- the
validation gate, the processing pipeline, the injection formatter -- all
assume this shape is exact, so the wrapping done here is the single place
that has to be right.

This module is pure: no event loop, no database, no model. It is the cheap,
deterministic layer that turns a raw return value into a contract object.
"""
from __future__ import annotations

import time

# The envelope contract version. Bump only on a breaking shape change; the
# validation gate checks this field is present and non-empty.
ENVELOPE_VERSION = "1"

STATUS_OK = "ok"
STATUS_ERROR = "error"

# The exact, complete key set of a contract envelope. The validation gate
# forbids anything outside it -- that is how schema drift is caught early.
ENVELOPE_KEYS = ("status", "tool", "version", "data", "error", "meta")


def _build_meta(extra: dict | None) -> dict:
    """A fresh meta block: a wrap timestamp plus any caller-supplied keys."""
    meta: dict = {"wrapped_at": int(time.time())}
    if extra:
        for key, value in extra.items():
            meta[str(key)] = value
    return meta


def ok_envelope(tool: str, data, *, meta: dict | None = None) -> dict:
    """Build a well-formed success envelope carrying ``data``."""
    return {
        "status": STATUS_OK,
        "tool": str(tool or "unknown"),
        "version": ENVELOPE_VERSION,
        "data": data,
        "error": None,
        "meta": _build_meta(meta),
    }


def error_envelope(
    tool: str, code: str, message: str, *, meta: dict | None = None,
) -> dict:
    """Build a well-formed error envelope.

    An error envelope never carries data: a failed call has no payload, only
    a machine-readable ``code`` and a human-readable ``message``.
    """
    return {
        "status": STATUS_ERROR,
        "tool": str(tool or "unknown"),
        "version": ENVELOPE_VERSION,
        "data": None,
        "error": {
            "code": str(code or "tool_error"),
            "message": str(message or "the tool failed"),
        },
        "meta": _build_meta(meta),
    }


def is_envelope(value) -> bool:
    """True when ``value`` already has the shape of a contract envelope.

    A bare ``{"error": "..."}`` from a legacy tool is *not* an envelope -- it
    has no ``status`` -- so :func:`wrap_result` will still wrap it correctly.
    """
    if not isinstance(value, dict):
        return False
    if value.get("status") not in (STATUS_OK, STATUS_ERROR):
        return False
    return all(key in value for key in ("tool", "version", "data", "error"))


def wrap_result(tool: str, raw, *, meta: dict | None = None) -> dict:
    """Wrap a raw tool return value into a contract envelope.

    A tool handler is allowed to return:

    * an envelope already -- it is passed through, with ``meta`` merged in;
    * a dict carrying a truthy ``error`` key -- the long-standing failure
      convention, turned into an error envelope (an optional ``code`` key is
      honoured);
    * any other dict -- it becomes the ``data`` of an ok envelope;
    * any non-dict value -- wrapped as ``{"value": <it>}`` so ``data`` is
      always an object.

    This keeps the Lua runtime and the generic tools dumb: they return plain
    values, and the contract is enforced here, at the Python boundary.
    """
    if is_envelope(raw):
        env = {key: raw.get(key) for key in ENVELOPE_KEYS}
        merged = dict(env.get("meta") or {})
        if meta:
            for key, value in meta.items():
                merged[str(key)] = value
        env["meta"] = merged or _build_meta(None)
        if not env.get("tool"):
            env["tool"] = str(tool or "unknown")
        return env

    if isinstance(raw, dict):
        failure = raw.get("error")
        if failure:
            code = raw.get("code") or "tool_error"
            return error_envelope(tool, code, failure, meta=meta)
        return ok_envelope(tool, raw, meta=meta)

    if raw is None:
        return ok_envelope(tool, {}, meta=meta)
    return ok_envelope(tool, {"value": raw}, meta=meta)
