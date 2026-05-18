"""framework/plugins/runtime.py -- the sandboxed Lua runtime and plugin contract.

A plugin file ``return``s one table::

    return {
      manifest = {
        id          = "notes",        -- slug, [a-z0-9_-]+
        name        = "Notes",
        version     = "1.0.0",
        description = "Private notes.",
        author      = "HiLleywyn",
        category    = "Productivity",
        storage     = "productivity", -- optional document-store namespace
      },
      commands  = { <command>, ... }, -- prefix commands (see api.py)
      tools     = { <tool>, ... },    -- agent tools (see api.py)
      loops     = { <loop>, ... },    -- background jobs (see api.py)
      events    = { <name> = fn },    -- gateway + cross-plugin hooks
      on_load   = function() ... end, -- optional, run when the plugin loads
      on_unload = function() ... end, -- optional, run before it unloads
    }

:func:`compile_plugin` runs that file in a fresh, sandboxed runtime and returns
a :class:`LuaPlugin`. Loading never imports ``lupa`` at module scope, so the
bot still starts (with plugins disabled) when the optional dependency is
absent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,38}$")
_EVENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Stripped from every plugin runtime before the plugin file runs. Plugins get
# string / table / math / os.time and friends, never a way off the sandbox.
_SANDBOX_PRELUDE = """
os.execute = nil
os.exit = nil
os.remove = nil
os.rename = nil
os.getenv = nil
os.setlocale = nil
os.tmpname = nil
io = nil
dofile = nil
loadfile = nil
load = nil
loadstring = nil
require = nil
package = nil
collectgarbage = nil
debug = nil
"""


class PluginError(Exception):
    """A plugin file is malformed, unsafe, or failed to compile."""


@dataclass
class PluginManifest:
    """The validated ``manifest`` block of a plugin file."""

    id: str
    name: str
    version: str
    description: str = ""
    author: str = ""
    category: str = "General"
    storage: str = ""

    def __post_init__(self) -> None:
        if not self.storage:
            self.storage = self.id

    def as_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "version": self.version,
            "description": self.description, "author": self.author,
            "category": self.category, "storage": self.storage,
        }


@dataclass
class LuaPlugin:
    """A compiled plugin: its manifest plus the live Lua runtime and tables."""

    manifest: PluginManifest
    runtime: object  # lupa.LuaRuntime
    commands: list = field(default_factory=list)
    tools: list = field(default_factory=list)
    loops: list = field(default_factory=list)
    # event name -> Lua handler. A name in EVENT_NAMES is a Discord gateway
    # hook; any other name is a custom event delivered through arch.emit.
    events: dict = field(default_factory=dict)
    on_load: object | None = None    # a Lua function, or None
    on_unload: object | None = None  # a Lua function, or None


# ── Lua <-> Python marshalling ────────────────────────────────────────────────
def _lua_type(value):
    """Return ``"table"`` / ``"function"`` / ... for a Lua value, else ``None``."""
    try:
        from lupa import lua_type
    except Exception:  # noqa: BLE001
        return None
    return lua_type(value)


def lua_to_py(value):
    """Convert a Lua value into Python, keeping Lua functions callable as-is.

    Lua tables become a ``list`` when they are a clean 1..n sequence and a
    ``dict`` otherwise. Functions and scalars pass straight through.
    """
    if _lua_type(value) != "table":
        return value
    keys = list(value.keys())
    if not keys:
        return {}
    norm: list[int] = []
    for k in keys:
        if isinstance(k, int):
            norm.append(k)
        elif isinstance(k, float) and k.is_integer():
            norm.append(int(k))
        else:
            norm = []
            break
    if norm and sorted(norm) == list(range(1, len(norm) + 1)):
        return [lua_to_py(value[k]) for k in range(1, len(norm) + 1)]
    return {str(k): lua_to_py(value[k]) for k in keys}


def py_to_lua(runtime, value):
    """Convert a Python value into a Lua-native value (recursively)."""
    if isinstance(value, dict):
        return runtime.table_from(
            {k: py_to_lua(runtime, v) for k, v in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return runtime.table_from([py_to_lua(runtime, v) for v in value])
    return value


# ── manifest validation ───────────────────────────────────────────────────────
def _require_str(block: dict, key: str, *, required: bool = True) -> str:
    raw = block.get(key)
    if raw is None or raw == "":
        if required:
            raise PluginError(f"manifest is missing `{key}`")
        return ""
    if not isinstance(raw, str):
        raise PluginError(f"manifest `{key}` must be a string")
    return raw.strip()


def parse_manifest(block) -> PluginManifest:
    """Validate a raw manifest table into a :class:`PluginManifest`."""
    if not isinstance(block, dict):
        raise PluginError("plugin is missing its `manifest` table")
    plugin_id = _require_str(block, "id")
    if not _ID_RE.match(plugin_id):
        raise PluginError(
            f"plugin id {plugin_id!r} must be 2-39 chars of a-z, 0-9, _ or -"
        )
    storage = _require_str(block, "storage", required=False) or plugin_id
    if not _ID_RE.match(storage):
        raise PluginError(f"manifest `storage` namespace {storage!r} is invalid")
    return PluginManifest(
        id=plugin_id,
        name=_require_str(block, "name"),
        version=_require_str(block, "version", required=False) or "0.0.0",
        description=_require_str(block, "description", required=False),
        author=_require_str(block, "author", required=False),
        category=_require_str(block, "category", required=False) or "General",
        storage=storage,
    )


# ── compilation ───────────────────────────────────────────────────────────────
def lupa_available() -> bool:
    """True when the optional ``lupa`` dependency can be imported."""
    try:
        import lupa  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def new_runtime():
    """Create a fresh, sandboxed Lua runtime, or raise :class:`PluginError`."""
    try:
        from lupa import LuaRuntime
    except Exception as exc:  # noqa: BLE001
        raise PluginError("the `lupa` package is not installed") from exc
    runtime = LuaRuntime(
        unpack_returned_tuples=True,
        register_eval=False,
        register_builtins=False,
    )
    runtime.execute(_SANDBOX_PRELUDE)
    return runtime


def compile_plugin(source: str, *, expected_id: str | None = None) -> LuaPlugin:
    """Compile plugin Lua ``source`` into a :class:`LuaPlugin`.

    Raises :class:`PluginError` on any malformed manifest, syntax error, or
    (when ``expected_id`` is given) an id that does not match the file it was
    loaded as.
    """
    runtime = new_runtime()
    try:
        table = runtime.execute(source)
    except Exception as exc:  # noqa: BLE001
        raise PluginError(f"Lua error while loading: {exc}") from exc
    if _lua_type(table) != "table":
        raise PluginError("a plugin file must `return` a table")

    manifest = parse_manifest(lua_to_py(table["manifest"]))
    if expected_id is not None and manifest.id != expected_id:
        raise PluginError(
            f"manifest id {manifest.id!r} does not match file id {expected_id!r}"
        )

    commands = _as_list(lua_to_py(table["commands"]), "commands")
    tools = _as_list(lua_to_py(table["tools"]), "tools")
    loops = _as_list(lua_to_py(table["loops"]), "loops")
    _validate_commands(commands)

    events = _as_event_map(lua_to_py(table["events"]))
    on_load = _require_callable(table["on_load"], "on_load")
    on_unload = _require_callable(table["on_unload"], "on_unload")
    return LuaPlugin(
        manifest=manifest, runtime=runtime,
        commands=commands, tools=tools, loops=loops,
        events=events, on_load=on_load, on_unload=on_unload,
    )


def _as_list(value, label: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise PluginError(f"`{label}` must be an array of tables")
    return value


def _as_event_map(value) -> dict:
    """Validate the ``events`` table into a ``{name: lua_function}`` dict."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PluginError("`events` must be a table of name = function")
    events: dict = {}
    for name, handler in value.items():
        key = str(name)
        if not _EVENT_NAME_RE.match(key):
            raise PluginError(
                f"event name {key!r} must be lower-case letters, digits and _"
            )
        if _lua_type(handler) != "function":
            raise PluginError(f"event `{key}` must be a function")
        events[key] = handler
    return events


def _require_callable(value, label: str):
    """Return ``value`` if it is a Lua function or absent, else raise."""
    if value is None:
        return None
    if _lua_type(value) != "function":
        raise PluginError(f"`{label}` must be a function")
    return value


def _validate_commands(commands: list, *, depth: int = 0) -> None:
    for cmd in commands:
        if not isinstance(cmd, dict):
            raise PluginError("each command must be a table")
        if not cmd.get("name"):
            raise PluginError("a command is missing its `name`")
        subs = cmd.get("subcommands") or []
        if subs and not isinstance(subs, list):
            raise PluginError(f"`{cmd['name']}` subcommands must be an array")
        if not cmd.get("run") and not subs:
            raise PluginError(
                f"command `{cmd['name']}` needs a `run` handler or subcommands"
            )
        if depth >= 2:
            raise PluginError("commands may nest at most two levels deep")
        _validate_commands(subs, depth=depth + 1)
