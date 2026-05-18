"""ai/lua_plugins.py -- optional Lua plugin loader.

Drop a ``.lua`` file in ``plugins/`` to register extra tools without
touching Python. Each file must return a table of tool definitions::

    return {
      {
        name = "fun.coinflip",
        description = "Flip a coin.",
        parameters = { type = "object", properties = {} },
        handler = function(args) return { result = "heads" } end,
      },
    }

Loading is best-effort: when the optional ``lupa`` dependency is missing,
or a file errors, the bot logs a warning and carries on. Lua handlers run
synchronously in a thread so a slow plugin never blocks the event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os

from ai.tools import RISK_SAFE, ToolRegistry, ToolSpec

log = logging.getLogger(__name__)

_PLUGIN_DIR = os.path.join(os.path.dirname(__file__), "..", "plugins")


def _lua_to_py(value):
    """Convert a Lua table / scalar returned by a handler into Python."""
    try:
        from lupa import lua_type
    except Exception:  # noqa: BLE001
        lua_type = None  # type: ignore

    if lua_type is not None and lua_type(value) == "table":
        keys = list(value.keys())
        if keys and all(isinstance(k, int) for k in keys):
            return [_lua_to_py(value[k]) for k in keys]
        return {str(k): _lua_to_py(value[k]) for k in keys}
    return value


def load_plugins(registry: ToolRegistry) -> int:
    """Load every ``plugins/*.lua`` file into ``registry``. Returns tool count."""
    plugin_dir = os.path.abspath(_PLUGIN_DIR)
    if not os.path.isdir(plugin_dir):
        return 0

    try:
        from lupa import LuaRuntime
    except Exception:  # noqa: BLE001
        log.info("lupa not installed -- Lua plugins disabled")
        return 0

    loaded = 0
    for fname in sorted(os.listdir(plugin_dir)):
        if not fname.endswith(".lua"):
            continue
        path = os.path.join(plugin_dir, fname)
        try:
            lua = LuaRuntime(unpack_returned_tuples=True)
            with open(path, "r", encoding="utf-8") as fh:
                table = lua.execute(fh.read())
            if table is None:
                continue
            for entry in table.values():
                spec = _build_spec(entry, fname)
                if spec is not None:
                    registry.register(spec)
                    loaded += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("Lua plugin %s failed to load: %s", fname, exc)
    if loaded:
        log.info("Loaded %d tool(s) from Lua plugins", loaded)
    return loaded


def _build_spec(entry, source: str) -> ToolSpec | None:
    try:
        name = str(entry["name"])
        description = str(entry["description"])
        params = _lua_to_py(entry["parameters"]) or {"type": "object", "properties": {}}
        lua_handler = entry["handler"]
    except Exception as exc:  # noqa: BLE001
        log.warning("malformed Lua tool in %s: %s", source, exc)
        return None

    async def handler(args: dict, _ctx) -> dict:
        def _call():
            result = lua_handler(args)
            return _lua_to_py(result)

        result = await asyncio.to_thread(_call)
        if isinstance(result, (dict, list)):
            return result if isinstance(result, dict) else {"result": result}
        try:
            return {"result": json.loads(json.dumps(result, default=str))}
        except Exception:  # noqa: BLE001
            return {"result": str(result)}

    return ToolSpec(
        name=name,
        description=description,
        parameters=params,
        handler=handler,
        category="plugin",
        risk=RISK_SAFE,
    )
