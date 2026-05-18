"""framework/pipeline/transforms.py -- deterministic transform functions.

Pure computation, no intelligence. These back the ``transform.*`` agent
tools: jobs the model would otherwise do by eye -- pick the top N rows, keep
only certain fields, sum a column -- and get wrong. Done here they are exact.

Every function is pure and total: it never raises and never touches I/O. Bad
input comes back as ``{"error": "..."}``, which the pipeline turns into a
clean error envelope, so a model that calls one of these with nonsense gets a
structured rejection rather than a crash.

These remove load from the model and remove a whole class of arithmetic and
ordering mistakes from its answers.
"""
from __future__ import annotations

_AGGREGATIONS = ("sum", "min", "max", "mean", "count")


def _as_number(value):
    """Coerce a value to a float, or ``None`` if it is not numeric."""
    if isinstance(value, bool):  # bool is an int subclass -- exclude it
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (TypeError, ValueError):
            return None
    return None


def _sort_key(field: str, order: str):
    """A key function that sorts rows by ``field``, missing values last."""
    descending = str(order or "desc").lower() != "asc"

    def key(row):
        value = row.get(field) if isinstance(row, dict) else None
        number = _as_number(value)
        if number is not None:
            return (0, number)
        if value is None:
            # Missing values sort to the end whichever way we are going.
            return (2 if not descending else -1, "")
        return (1, str(value))

    return key, descending


def slice_items(
    items, n, *, key: str | None = None, order: str = "desc",
) -> dict:
    """Return the top ``n`` items of a list.

    With ``key`` the list is first sorted by that field of each object
    (``order`` is ``desc`` or ``asc``); without it the list's existing order
    is kept and the first ``n`` are taken. Always reports the original
    ``total`` so the model knows how much it did not see.
    """
    if not isinstance(items, list):
        return {"error": "items must be a list"}
    try:
        count = int(n)
    except (TypeError, ValueError):
        return {"error": "n must be an integer"}
    if count < 0:
        return {"error": "n must not be negative"}

    rows = list(items)
    total = len(rows)
    if key:
        key_fn, descending = _sort_key(str(key), order)
        try:
            rows = sorted(rows, key=key_fn, reverse=descending)
        except TypeError:
            return {"error": "items are not comparable on that key"}
    chosen = rows[:count]
    return {"items": chosen, "returned": len(chosen), "total": total}


def project_fields(items, fields) -> dict:
    """Keep only ``fields`` on each object of a list -- drop every other column.

    Trims wide rows down to what the model actually asked for. Non-object
    items pass through untouched.
    """
    if not isinstance(items, list):
        return {"error": "items must be a list"}
    if not isinstance(fields, list) or not fields:
        return {"error": "fields must be a non-empty list of field names"}
    wanted = [str(name) for name in fields]
    out = []
    for row in items:
        if isinstance(row, dict):
            out.append({name: row.get(name) for name in wanted})
        else:
            out.append(row)
    return {"items": out, "returned": len(out), "fields": wanted}


def aggregate(items, *, field: str | None = None, op: str = "sum") -> dict:
    """Reduce a list of numbers to one metric.

    ``op`` is ``sum`` / ``min`` / ``max`` / ``mean`` / ``count``. With
    ``field`` the numeric value is read from that key of each object;
    without it each item is treated as a number directly. Non-numeric items
    are skipped and counted in ``skipped``.
    """
    if not isinstance(items, list):
        return {"error": "items must be a list"}
    operation = str(op or "sum").lower().strip()
    if operation not in _AGGREGATIONS:
        return {"error": f"op must be one of {', '.join(_AGGREGATIONS)}"}

    numbers: list[float] = []
    skipped = 0
    for row in items:
        raw = row.get(field) if (field and isinstance(row, dict)) else row
        number = _as_number(raw)
        if number is None:
            skipped += 1
        else:
            numbers.append(number)

    if operation == "count":
        return {"op": "count", "value": len(numbers), "skipped": skipped}
    if not numbers:
        return {"error": "no numeric values to aggregate"}

    if operation == "sum":
        value: float = sum(numbers)
    elif operation == "min":
        value = min(numbers)
    elif operation == "max":
        value = max(numbers)
    else:  # mean
        value = sum(numbers) / len(numbers)

    # Hand back a whole number as an int when it is exactly one.
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return {"op": operation, "value": value, "count": len(numbers),
            "skipped": skipped}
