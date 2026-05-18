"""framework/plugins -- the Lua plugin system.

Archimedes plugins are single ``.lua`` files. A plugin can register prefix
commands (with nested subcommand groups), agent tools the model can call, and
background loops -- all without touching Python.

The pieces:

* :mod:`runtime`  -- the sandboxed Lua runtime, value marshalling and the
  plugin-file contract (the ``manifest`` / ``commands`` / ``tools`` / ``loops``
  table a plugin returns).
* :mod:`api`      -- the ``arch`` global and per-call ``ctx`` table handed to
  Lua handlers: embeds, the document store, replies, confirmations.
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
