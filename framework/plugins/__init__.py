"""framework/plugins -- the Lua plugin system.

Archimedes plugins are single ``.lua`` files. A plugin can register prefix
commands (with nested subcommand groups), agent tools the model can call,
background loops, and event handlers that react to Discord activity -- all
without touching Python.

The pieces:

* :mod:`runtime`  -- the sandboxed Lua runtime, value marshalling and the
  plugin-file contract (the ``manifest`` / ``commands`` / ``tools`` /
  ``loops`` / ``events`` table a plugin returns, plus ``on_load`` /
  ``on_unload``).
* :mod:`api`      -- the ``arch`` global and per-call ``ctx`` table handed to
  Lua handlers: embeds, the document store, the key/value store, the HTTP
  client, the Discord read/write helpers, JSON and encoding utilities.
* :mod:`net`      -- the SSRF-guarded HTTP client behind ``arch.http``.
* :mod:`util`     -- the pure helpers behind ``arch.json`` / ``base64`` /
  ``hash`` / ``uuid`` / ``random``.
* :mod:`events`   -- the gateway event dispatcher and the cross-plugin event
  bus behind ``M.events`` and ``arch.emit``.
* :mod:`registry` -- the marketplace client that searches and downloads
  plugins from the Archimedes-Plugins GitHub repository.
* :mod:`manager`  -- :class:`~framework.plugins.manager.PluginManager`, which
  installs, enables, disables, updates and live-loads plugins and keeps the
  installed set persisted so it survives a restart.
"""
from __future__ import annotations

from framework.plugins.manager import PluginManager
from framework.plugins.runtime import LuaPlugin, PluginError, PluginManifest

__all__ = ["PluginManager", "LuaPlugin", "PluginError", "PluginManifest"]
