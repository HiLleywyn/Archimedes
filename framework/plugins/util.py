"""framework/plugins/util.py -- pure helpers behind the plugin `arch` global.

These back ``arch.json``, ``arch.base64``, ``arch.hash``, ``arch.uuid`` and
``arch.random``. Every function here is pure: no event loop, no database, no
``lupa`` import. That keeps them trivially unit-testable offline and safe to
call straight from a plugin worker thread with no bridge.

Callers in ``api.py`` marshal Lua values to plain Python before handing them
here, and marshal results back, so these functions only ever see Python
``str`` / ``int`` / ``dict`` / ``list`` / ``None``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import random
import uuid

# Hash algorithms a plugin may name. Kept small and well known on purpose --
# a plugin has no business reaching for the exotic end of hashlib.
_HASH_ALGOS = ("sha256", "sha1", "md5")


def json_encode(value) -> str | None:
    """Serialise a value to a JSON string, or ``None`` if it cannot be."""
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return None


def json_decode(text):
    """Parse a JSON string into a value, or ``None`` on malformed input."""
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def b64_encode(text) -> str:
    """Base64-encode a string (UTF-8) and return ASCII text."""
    raw = text if isinstance(text, bytes) else str(text or "").encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def b64_decode(text) -> str | None:
    """Decode base64 text back to a UTF-8 string, or ``None`` if invalid."""
    try:
        return base64.b64decode(str(text or ""), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None


def hash_text(algo, text="") -> str | None:
    """Hex digest of ``text`` under ``algo`` (sha256 / sha1 / md5), or ``None``.

    An algorithm outside the allowlist returns ``None`` rather than raising,
    so a typo in a plugin degrades gracefully instead of crashing a handler.
    """
    name = str(algo or "").lower().strip()
    if name not in _HASH_ALGOS:
        return None
    digest = hashlib.new(name)
    digest.update(str(text or "").encode("utf-8"))
    return digest.hexdigest()


def make_uuid() -> str:
    """A fresh random UUID4 as a string."""
    return str(uuid.uuid4())


def rand(a=None, b=None):
    """A random float in [0, 1) with no args, or an int in [a, b] with two.

    Mirrors Lua's own ``math.random`` shape so it reads naturally to plugin
    authors. A single argument is treated as ``rand(1, a)``.
    """
    if a is None:
        return random.random()
    try:
        low = 1 if b is None else int(a)
        high = int(a) if b is None else int(b)
    except (TypeError, ValueError):
        return random.random()
    if high < low:
        low, high = high, low
    return random.randint(low, high)
