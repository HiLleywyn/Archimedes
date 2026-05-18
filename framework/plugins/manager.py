"""framework/plugins/manager.py -- the plugin lifecycle manager.

:class:`PluginManager` is the single owner of every Lua plugin. It:

* discovers bundled plugins shipped in ``plugins/`` and records them,
* loads every enabled plugin on boot (bundled from disk, marketplace plugins
  from the Lua source stored in the database, so an install survives a
  redeploy of the container),
* installs, updates, enables, disables and uninstalls plugins on request,
* registers and tears down each plugin's prefix commands, agent tools and
  background loops live, with no restart.

All mutating operations return a short human-readable status string so the
``.ai plugins`` command surface can echo the outcome straight back.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

from config import Config
from framework.plugins.api import LuaApi, build_commands, command_help_embed
from framework.plugins.events import (
    EVENT_NAMES, PluginEventBus, PluginEventDispatcher,
)
from framework.plugins.registry import PluginRegistry, RegistryError
from framework.plugins.runtime import (
    LuaPlugin, PluginError, compile_plugin, lupa_available,
)

log = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_PLUGIN_DIR = os.path.join(_REPO_ROOT, "plugins")

ORIGIN_BUNDLED = "bundled"
ORIGIN_MARKETPLACE = "marketplace"


@dataclass
class LoadedPlugin:
    """A plugin that is compiled, activated and wired into the bot."""

    plugin: LuaPlugin
    api: LuaApi
    command_names: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    loop_tasks: list[asyncio.Task] = field(default_factory=list)
    event_names: list[str] = field(default_factory=list)


class PluginManager:
    """Owns discovery, persistence and the live lifecycle of every plugin."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self.db = bot.db
        self.registry = PluginRegistry(
            Config.PLUGIN_REGISTRY_REPO,
            Config.PLUGIN_REGISTRY_REF,
            Config.GITHUB_TOKEN,
        )
        self._loaded: dict[str, LoadedPlugin] = {}
        self._errors: dict[str, str] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self.available = lupa_available()
        # The cross-plugin event bus, plus the gateway-event handler map
        # (plugin id -> {event name -> Lua handler}). The dispatcher cog is
        # added once, in startup().
        self.event_bus = PluginEventBus()
        self._event_handlers: dict[str, dict[str, object]] = {}
        self._dispatcher: PluginEventDispatcher | None = None
        # Live event-handler tasks. Held so the loop cannot garbage-collect a
        # fire-and-forget task before it finishes.
        self._event_tasks: set[asyncio.Task] = set()

    # ── boot ──────────────────────────────────────────────────────────────────
    async def startup(self) -> None:
        """Discover bundled plugins and load every enabled plugin."""
        if not Config.PLUGINS_ENABLED:
            log.info("plugins disabled by configuration")
            return
        if not self.available:
            log.warning("lupa is not installed -- Lua plugins are unavailable")
            return
        self._loop = asyncio.get_running_loop()
        # One dispatcher cog feeds Discord gateway events to every plugin.
        # Added once and kept across reloads; discord.py fans events to it
        # independently of the chat and sidecar cogs.
        if self._dispatcher is None:
            self._dispatcher = PluginEventDispatcher(self.bot, self)
            await self.bot.add_cog(self._dispatcher)
        await self._sync_bundled()
        rows = await self.db.list_installed_plugins()
        loaded = 0
        for row in rows:
            if not row["enabled"]:
                continue
            source = await self._source_for(row)
            if source is None:
                continue
            if await self._load(row["plugin_id"], source, row["origin"]):
                loaded += 1
        log.info("plugins: %d loaded, %d known, %d failed",
                 loaded, len(rows), len(self._errors))

    async def _sync_bundled(self) -> None:
        """Register every ``plugins/*.lua`` file as a bundled plugin row."""
        if not os.path.isdir(_PLUGIN_DIR):
            return
        seen: set[str] = set()
        for fname in sorted(os.listdir(_PLUGIN_DIR)):
            if not fname.endswith(".lua"):
                continue
            plugin_id = fname[:-4]
            path = os.path.join(_PLUGIN_DIR, fname)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    source = fh.read()
                plugin = compile_plugin(source, expected_id=plugin_id)
            except (PluginError, OSError) as exc:
                log.warning("bundled plugin %s is invalid: %s", fname, exc)
                self._errors[plugin_id] = str(exc)
                continue
            seen.add(plugin_id)
            m = plugin.manifest
            await self.db.upsert_installed_plugin(
                plugin_id=m.id, name=m.name, version=m.version,
                origin=ORIGIN_BUNDLED, description=m.description,
                author=m.author, category=m.category, source="", source_repo="",
            )
        # A bundled row whose file has gone is dropped so the registry stays
        # an honest mirror of what ships in plugins/.
        for row in await self.db.list_installed_plugins():
            if row["origin"] == ORIGIN_BUNDLED and row["plugin_id"] not in seen:
                await self.db.delete_installed_plugin(row["plugin_id"])

    async def _source_for(self, row: dict) -> str | None:
        """The Lua source for an installed-plugin row, or ``None`` if missing."""
        if row["origin"] == ORIGIN_BUNDLED:
            path = os.path.join(_PLUGIN_DIR, f"{row['plugin_id']}.lua")
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return fh.read()
            except OSError as exc:
                self._errors[row["plugin_id"]] = f"bundled file unreadable: {exc}"
                return None
        if not row["source"]:
            self._errors[row["plugin_id"]] = "no stored source"
            return None
        return row["source"]

    # ── load / unload ─────────────────────────────────────────────────────────
    async def _load(self, plugin_id: str, source: str, origin: str) -> bool:
        """Compile, activate and wire one plugin into the bot."""
        if plugin_id in self._loaded:
            return True
        try:
            plugin = compile_plugin(source, expected_id=plugin_id)
        except PluginError as exc:
            self._errors[plugin_id] = str(exc)
            log.warning("plugin %s failed to compile: %s", plugin_id, exc)
            return False

        api = LuaApi(plugin, db=self.db, bot=self.bot, loop=self._loop,
                     manager=self)
        try:
            api.activate()
            if plugin.on_load is not None:
                await api.run_in_worker(plugin.on_load)
            commands = build_commands(api, plugin)
            self._claim_command_space(plugin_id, commands)
        except Exception as exc:  # noqa: BLE001
            self._errors[plugin_id] = str(exc)
            log.warning("plugin %s failed to activate: %s", plugin_id, exc)
            return False

        added: list[str] = []
        try:
            for cmd in commands:
                self.bot.add_command(cmd)
                added.append(cmd.name)
        except Exception as exc:  # noqa: BLE001
            for name in added:
                self.bot.remove_command(name)
            self._errors[plugin_id] = f"command registration failed: {exc}"
            log.warning("plugin %s command registration failed: %s",
                        plugin_id, exc)
            return False

        tool_names: list[str] = []
        if self.bot.tools is not None:
            for spec in api.build_tools():
                self.bot.tools.register(spec)
                tool_names.append(spec.name)

        loop_tasks: list[asyncio.Task] = []
        for loop_def in plugin.loops:
            if not isinstance(loop_def, dict):
                continue
            runner = api.make_loop_runner(loop_def)
            if runner is not None:
                loop_tasks.append(asyncio.create_task(runner()))

        event_names = self._register_events(plugin_id, plugin)

        self._loaded[plugin_id] = LoadedPlugin(
            plugin=plugin, api=api, command_names=added,
            tool_names=tool_names, loop_tasks=loop_tasks,
            event_names=event_names,
        )
        self._errors.pop(plugin_id, None)
        log.info("plugin loaded: %s v%s (%d command(s), %d tool(s), "
                 "%d event(s))", plugin_id, plugin.manifest.version,
                 len(added), len(tool_names), len(event_names))
        return True

    def _register_events(self, plugin_id: str, plugin: LuaPlugin) -> list[str]:
        """Wire a plugin's ``events`` table into the dispatcher and the bus."""
        gateway: dict[str, object] = {}
        names: list[str] = []
        for name, handler in plugin.events.items():
            if name in EVENT_NAMES:
                gateway[name] = handler
            else:
                self.event_bus.subscribe(plugin_id, name, handler)
            names.append(name)
        if gateway:
            self._event_handlers[plugin_id] = gateway
        return names

    def _claim_command_space(self, plugin_id: str, commands: list) -> None:
        """Raise if any command name or alias is already taken."""
        for cmd in commands:
            for label in [cmd.name, *cmd.aliases]:
                existing = self.bot.get_command(label)
                if existing is not None:
                    raise PluginError(
                        f"command name `{label}` is already in use"
                    )

    async def _unload(self, plugin_id: str) -> None:
        """Tear a plugin's commands, tools, loops and events out of the bot."""
        loaded = self._loaded.pop(plugin_id, None)
        if loaded is None:
            return
        # on_unload runs while the plugin is still live so the hook can still
        # reach the document store, HTTP client and the rest of `arch`.
        if loaded.plugin.on_unload is not None:
            try:
                await loaded.api.run_in_worker(loaded.plugin.on_unload)
            except Exception:  # noqa: BLE001
                log.exception("plugin %s on_unload failed", plugin_id)
        self._event_handlers.pop(plugin_id, None)
        self.event_bus.unsubscribe_plugin(plugin_id)
        for name in loaded.command_names:
            self.bot.remove_command(name)
        if self.bot.tools is not None:
            for name in loaded.tool_names:
                self.bot.tools.unregister(name)
        for task in loaded.loop_tasks:
            task.cancel()
        log.info("plugin unloaded: %s", plugin_id)

    # ── event dispatch ────────────────────────────────────────────────────────
    async def dispatch_event(self, event_name: str, payload: dict) -> None:
        """Fan one Discord gateway event out to every subscribed plugin."""
        for plugin_id, handlers in list(self._event_handlers.items()):
            handler = handlers.get(event_name)
            if handler is None:
                continue
            loaded = self._loaded.get(plugin_id)
            if loaded is None:
                continue
            self._spawn_event(
                self._run_event(plugin_id, loaded.api, handler, payload))

    async def emit(self, event_name: str, payload, *, source: str = "") -> int:
        """Deliver a custom ``arch.emit`` event to its subscribers.

        Returns the number of plugins the event reached. The payload is
        tagged with ``_event`` and ``_from`` so a handler can tell the origin.
        """
        data = dict(payload) if isinstance(payload, dict) else {"value": payload}
        data.setdefault("_event", event_name)
        data.setdefault("_from", source)
        count = 0
        for plugin_id, handler in self.event_bus.subscribers(event_name):
            loaded = self._loaded.get(plugin_id)
            if loaded is None:
                continue
            self._spawn_event(
                self._run_event(plugin_id, loaded.api, handler, data))
            count += 1
        return count

    def _spawn_event(self, coro) -> None:
        """Start an event-handler task and hold a reference until it ends."""
        task = asyncio.create_task(coro)
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)

    async def _run_event(self, plugin_id: str, api, handler, payload) -> None:
        """Run one plugin's event handler, isolating its failures."""
        try:
            await api.run_in_worker(api.invoke_event, handler, payload)
        except Exception:  # noqa: BLE001
            log.exception("plugin %s event handler failed", plugin_id)

    # ── admin operations ──────────────────────────────────────────────────────
    async def enable(self, plugin_id: str) -> str:
        row = await self.db.get_installed_plugin(plugin_id)
        if row is None:
            return f"No plugin `{plugin_id}` is installed."
        if row["enabled"] and plugin_id in self._loaded:
            return f"`{plugin_id}` is already enabled."
        await self.db.update_installed_plugin(plugin_id, enabled=True)
        source = await self._source_for(row)
        if source is None:
            return f"`{plugin_id}` is enabled but its source is missing."
        ok = await self._load(plugin_id, source, row["origin"])
        if not ok:
            return (f"`{plugin_id}` is enabled but failed to load: "
                    f"{self._errors.get(plugin_id, 'unknown error')}")
        return f"Enabled and loaded `{plugin_id}`."

    async def disable(self, plugin_id: str) -> str:
        row = await self.db.get_installed_plugin(plugin_id)
        if row is None:
            return f"No plugin `{plugin_id}` is installed."
        await self.db.update_installed_plugin(plugin_id, enabled=False)
        await self._unload(plugin_id)
        return f"Disabled `{plugin_id}`."

    async def install(self, plugin_id: str, *, actor_id: int) -> str:
        if not self.available:
            return "Plugin support is unavailable (lupa is not installed)."
        plugin_id = plugin_id.strip().lower()
        existing = await self.db.get_installed_plugin(plugin_id)
        if existing is not None:
            if existing["origin"] == ORIGIN_BUNDLED:
                return f"`{plugin_id}` is a bundled plugin -- it is already here."
            return f"`{plugin_id}` is already installed. Use update instead."
        if not self.registry.configured:
            return "No plugin marketplace is configured."
        try:
            entry, source = await self.registry.fetch_source(plugin_id)
        except RegistryError as exc:
            return f"Install failed: {exc}"
        try:
            plugin = compile_plugin(source, expected_id=plugin_id)
        except PluginError as exc:
            return f"Install failed: the plugin is invalid -- {exc}"
        m = plugin.manifest
        await self.db.upsert_installed_plugin(
            plugin_id=m.id, name=m.name, version=m.version,
            origin=ORIGIN_MARKETPLACE, description=m.description,
            author=m.author, category=m.category, source=source,
            source_repo=self.registry.repo, enabled=True, installed_by=actor_id,
        )
        ok = await self._load(plugin_id, source, ORIGIN_MARKETPLACE)
        if not ok:
            return (f"Installed `{plugin_id}` but it failed to load: "
                    f"{self._errors.get(plugin_id, 'unknown error')}")
        return f"Installed and loaded `{m.name}` v{m.version}."

    async def uninstall(self, plugin_id: str) -> str:
        row = await self.db.get_installed_plugin(plugin_id)
        if row is None:
            return f"No plugin `{plugin_id}` is installed."
        if row["origin"] == ORIGIN_BUNDLED:
            return (f"`{plugin_id}` is bundled with the bot and cannot be "
                    f"uninstalled. Disable it instead.")
        await self._unload(plugin_id)
        await self.db.delete_installed_plugin(plugin_id)
        return (f"Uninstalled `{plugin_id}`. Its stored data was kept -- "
                f"reinstalling restores it.")

    async def update(self, plugin_id: str) -> str:
        row = await self.db.get_installed_plugin(plugin_id)
        if row is None:
            return f"No plugin `{plugin_id}` is installed."
        if row["origin"] == ORIGIN_BUNDLED:
            return f"`{plugin_id}` is bundled -- it updates with the bot itself."
        if not self.registry.configured:
            return "No plugin marketplace is configured."
        try:
            entry, source = await self.registry.fetch_source(plugin_id)
        except RegistryError as exc:
            return f"Update failed: {exc}"
        try:
            plugin = compile_plugin(source, expected_id=plugin_id)
        except PluginError as exc:
            return f"Update failed: the new version is invalid -- {exc}"
        m = plugin.manifest
        if m.version == row["version"] and source == row["source"]:
            return f"`{plugin_id}` is already up to date (v{m.version})."
        old_version = row["version"]
        await self.db.upsert_installed_plugin(
            plugin_id=m.id, name=m.name, version=m.version,
            origin=ORIGIN_MARKETPLACE, description=m.description,
            author=m.author, category=m.category, source=source,
            source_repo=self.registry.repo,
        )
        await self._unload(plugin_id)
        if row["enabled"]:
            ok = await self._load(plugin_id, source, ORIGIN_MARKETPLACE)
            if not ok:
                return (f"Updated `{plugin_id}` to v{m.version} but it failed "
                        f"to load: {self._errors.get(plugin_id, 'unknown')}")
        return f"Updated `{plugin_id}` from v{old_version} to v{m.version}."

    async def reload(self, plugin_id: str | None = None) -> str:
        """Reload one plugin, or every enabled plugin, from current source."""
        if not self.available:
            return "Plugin support is unavailable (lupa is not installed)."
        if plugin_id is not None:
            row = await self.db.get_installed_plugin(plugin_id)
            if row is None:
                return f"No plugin `{plugin_id}` is installed."
            await self._unload(plugin_id)
            if not row["enabled"]:
                return f"`{plugin_id}` is reloaded but stays disabled."
            source = await self._source_for(row)
            if source is None:
                return f"Cannot reload `{plugin_id}`: source missing."
            ok = await self._load(plugin_id, source, row["origin"])
            return (f"Reloaded `{plugin_id}`." if ok else
                    f"Reload failed: {self._errors.get(plugin_id, 'unknown')}")
        for pid in list(self._loaded):
            await self._unload(pid)
        self._errors.clear()
        await self.startup()
        return (f"Reloaded all plugins: {len(self._loaded)} active, "
                f"{len(self._errors)} failed.")

    async def shutdown(self) -> None:
        """Unload every plugin and the dispatcher. Called on bot shutdown."""
        for pid in list(self._loaded):
            await self._unload(pid)
        if self._dispatcher is not None:
            try:
                await self.bot.remove_cog(self._dispatcher.qualified_name)
            except Exception:  # noqa: BLE001
                pass
            self._dispatcher = None

    # ── queries ───────────────────────────────────────────────────────────────
    async def list_plugins(self) -> list[dict]:
        """Every installed plugin with its live state, for the admin surface."""
        rows = await self.db.list_installed_plugins()
        out: list[dict] = []
        for row in rows:
            pid = row["plugin_id"]
            loaded = self._loaded.get(pid)
            out.append({
                "id": pid,
                "name": row["name"],
                "version": row["version"],
                "origin": row["origin"],
                "category": row["category"],
                "description": row["description"],
                "author": row["author"],
                "enabled": bool(row["enabled"]),
                "loaded": loaded is not None,
                "commands": list(loaded.command_names) if loaded else [],
                "tools": list(loaded.tool_names) if loaded else [],
                "error": self._errors.get(pid, ""),
            })
        return out

    async def get_plugin(self, plugin_id: str) -> dict | None:
        for entry in await self.list_plugins():
            if entry["id"] == plugin_id:
                return entry
        return None

    def is_loaded(self, plugin_id: str) -> bool:
        return plugin_id in self._loaded

    @property
    def loaded_count(self) -> int:
        return len(self._loaded)

    def help_categories(self, prefix: str) -> dict[str, list]:
        """One help section of embeds per loaded plugin, for the meta cog."""
        cats: dict[str, list] = {}
        for loaded in self._loaded.values():
            plugin = loaded.plugin
            if not plugin.commands:
                continue
            embeds = [
                command_help_embed(plugin, cmd, prefix)
                for cmd in plugin.commands if isinstance(cmd, dict)
            ]
            if embeds:
                cats[plugin.manifest.name] = embeds
        return cats
