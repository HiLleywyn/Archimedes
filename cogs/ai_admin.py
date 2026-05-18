"""cogs/ai_admin.py -- the .ai staff control surface.

One place for operators to tune the bot: feature flags, system prompts,
persona, the per-guild model picker, web-search backend, the agent tool
registry, Lua plugins, the memory sidecar, the emoji index and an audit
feed. Every mutating command is gated behind Manage Server and audited.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from config import Config
from framework.audit import (
    SCOPE_AI, SEVERITY_DANGER, SEVERITY_WARN,
    log_staff_action, recent_staff_actions,
)
from framework.context import ArchimedesContext
from framework.embed import card
from framework.middleware import guild_only, require_manage_guild
from framework.ui import (
    C_INFO, C_NAVY, C_PURPLE, C_SUCCESS, C_WARNING,
    CategoryPaginator, clip, fmt_ts,
)
from ai.client import complete_default
from ai.memory import guild_scope, user_scope
from ai.models import (
    TOOL_CATEGORIES, catalog_for, category as get_category,
    clear_guild_default, list_guild_defaults, resolve_model, set_guild_default,
)

log = logging.getLogger(__name__)

_AI_FLAGS = {
    "chat": "ai_chat_enabled",
    "commentary": "ai_commentary_enabled",
    "flavor": "ai_flavor_enabled",
    "events": "ai_events_enabled",
    "ambient": "ai_ambient_enabled",
}
_PROMPT_FEATURES = {
    "chat": "ai_promptchat",
    "commentary": "ai_promptcommentary",
    "events": "ai_promptevents",
    "flavor": "ai_promptflavor",
}
_SEARCH_BACKENDS = ("ddg", "brave")
_RISK_ICON = {"read": "🟢", "safe": "🔵", "mutate": "🟠", "danger": "🔴"}
_STATE_ICON = {True: "🟢", False: "🔴"}
_ORIGIN_ICON = {"bundled": "📦", "marketplace": "🛰️"}


class AIAdmin(commands.Cog):
    """Staff-facing AI control surface."""

    def __init__(self, bot) -> None:
        self.bot = bot

    # ── help ──────────────────────────────────────────────────────────────────
    def _help_categories(self, p: str) -> dict[str, list[discord.Embed]]:
        def page(title: str, lines: list[str]) -> discord.Embed:
            return card(title, color=C_PURPLE, description="\n".join(lines)).footer(
                f"Use {p}ai <subcommand>"
            ).build()

        return {
            "Overview": [page("AI Control Surface", [
                f"`{p}ai` is the one place to configure Archimedes.",
                "",
                "Config -- flags, prompts, persona, history",
                "Models -- per-guild model picker",
                "Web Search -- search backend",
                "Tools -- the agent tool registry",
                "Plugins -- install and manage Lua plugins",
                "Memory -- long-term facts and passive learning",
                "Emojis -- custom emoji meaning index",
                "Audit -- staff action feed",
                "",
                "All mutating commands require Manage Server.",
            ])],
            "Config": [page("AI Config", [
                f"`{p}ai status` -- feature flags + provider status",
                f"`{p}ai toggle <chat|commentary|flavor|events|ambient>`",
                f"`{p}ai test` -- send a test prompt to the model",
                f"`{p}ai prompt <feature> [text|reset]` -- custom system prompt",
                f"`{p}ai persona [name]` -- display name (blank resets)",
                f"`{p}ai clearhistory [@user]` -- wipe conversation history",
                f"`{p}ai forget` -- wipe only YOUR memory",
                f"`{p}ai recontext [@user|server|channel]` -- rebuild context",
            ])],
            "Models": [page("Model Picker", [
                f"`{p}ai model list` -- picks for every category",
                f"`{p}ai model show <category>` -- catalog for one category",
                f"`{p}ai model set <category> <provider:model|index>`",
                f"`{p}ai model reset <category>` -- revert to env default",
                "",
                "Categories: " + ", ".join(c.key for c in TOOL_CATEGORIES),
            ])],
            "Web Search": [page("Web Search", [
                f"`{p}ai websearch status`",
                f"`{p}ai websearch backend <ddg|brave>`",
                f"`{p}ai websearch reset`",
                "",
                "ddg needs no key; brave needs BRAVE_SEARCH_API_KEY.",
            ])],
            "Tools": [page("Agent Tools", [
                f"`{p}ai tools list` -- registered tools",
                f"`{p}ai tools info <name>` -- one tool's schema",
                f"`{p}ai tools enable|disable <name>`",
            ])],
            "Plugins": [page("Lua Plugin Manager", [
                f"`{p}ai plugins` / `list` -- installed plugins",
                f"`{p}ai plugins info <id>` -- one plugin's details",
                f"`{p}ai plugins search [query]` -- browse the marketplace",
                f"`{p}ai plugins install <id>` -- install from the marketplace",
                f"`{p}ai plugins uninstall <id>` -- remove a plugin",
                f"`{p}ai plugins enable|disable <id>`",
                f"`{p}ai plugins update [id]` -- pull the latest version",
                f"`{p}ai plugins reload [id]` -- recompile and reload",
            ])],
            "Memory": [page("Memory Sidecar", [
                f"`{p}ai memory facts [scope]` -- list long-term facts",
                f"`{p}ai memory remember <scope> <key> <value>`",
                f"`{p}ai memory forget <scope> <key>`",
                f"`{p}ai memory listen <on|off>` -- passive episode capture",
            ])],
            "Emojis": [page("Custom Emoji Index", [
                f"`{p}ai emojis stats` -- coverage",
                f"`{p}ai emojis index [force]` -- index custom emojis",
                f"`{p}ai emojis show` -- browse meanings",
                f"`{p}ai emojis set <emoji> <text>` -- manual override",
            ])],
            "Audit": [page("AI Audit Feed", [
                f"`{p}ai audit [limit]` -- recent staff actions",
            ])],
        }

    @commands.group(name="ai", invoke_without_command=True)
    @guild_only
    async def ai(self, ctx: ArchimedesContext) -> None:
        """AI control surface. Run .ai help for the full reference."""
        await CategoryPaginator.send(ctx, self._help_categories(ctx.prefix))

    @ai.command(name="help")
    @guild_only
    async def ai_help(self, ctx: ArchimedesContext) -> None:
        """Full .ai command reference."""
        await CategoryPaginator.send(ctx, self._help_categories(ctx.prefix))

    # ── config ────────────────────────────────────────────────────────────────
    @ai.command(name="status")
    @guild_only
    async def ai_status(self, ctx: ArchimedesContext) -> None:
        """Show feature flags and provider status."""
        flags = await ctx.db.get_ai_flags(ctx.guild_id)
        chat_model = await resolve_model(ctx.db, ctx.guild_id, "chat")
        has_key = bool(Config.OPENROUTER_API_KEY) or Config.CHAT_BACKEND == "ollama"
        b = card("AI Status", color=C_PURPLE)
        b.field("Backend", Config.CHAT_BACKEND, True)
        b.field("Provider key", "configured" if has_key else "MISSING", True)
        b.field("Chat model", f"{chat_model.provider}:{chat_model.model}", False)
        for feat in _AI_FLAGS:
            b.field(feat, "ON" if flags.get(feat) else "OFF", True)
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai.command(name="toggle")
    @guild_only
    @require_manage_guild
    async def ai_toggle(self, ctx: ArchimedesContext, feature: str) -> None:
        """Toggle an AI feature flag on/off."""
        key = (feature or "").strip().lower()
        col = _AI_FLAGS.get(key)
        if not col:
            await ctx.reply_error(f"Unknown feature. Valid: {', '.join(_AI_FLAGS)}")
            return
        settings = await ctx.db.get_guild_settings(ctx.guild_id)
        new_val = not bool(settings.get(col, False))
        await ctx.db.update_guild_setting(ctx.guild_id, col, new_val)
        await ctx.reply_success(f"{key} is now {'ON' if new_val else 'OFF'}.", title="AI Toggle")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="toggle", details=f"{key}={new_val}",
        )

    @ai.command(name="test")
    @guild_only
    @require_manage_guild
    async def ai_test(self, ctx: ArchimedesContext) -> None:
        """Send a test prompt to the model provider."""
        result = await complete_default(
            [
                {"role": "system", "content": "You are Archimedes, a Discord companion."},
                {"role": "user", "content": "Say 'AI is working' in one short sentence."},
            ],
            max_tokens=40,
        )
        if not result:
            await ctx.reply_error("AI call failed or returned nothing. Check the provider key.")
            return
        await ctx.reply_success(result, title="AI Test")

    @ai.command(name="prompt")
    @guild_only
    @require_manage_guild
    async def ai_prompt(self, ctx: ArchimedesContext, feature: str, *, prompt: str = "") -> None:
        """Set or reset a custom system prompt for a feature."""
        feat = (feature or "").strip().lower()
        col = _PROMPT_FEATURES.get(feat)
        if not col:
            await ctx.reply_error(f"Unknown feature. Valid: {', '.join(_PROMPT_FEATURES)}")
            return
        text = (prompt or "").strip()
        if not text or text.lower() == "reset":
            await ctx.db.update_guild_setting(ctx.guild_id, col, None)
            await ctx.reply_success(f"Reset {feat} prompt to default.", title="AI Prompt")
        else:
            await ctx.db.update_guild_setting(ctx.guild_id, col, text)
            await ctx.reply_success(f"Updated {feat} prompt ({len(text)} chars).", title="AI Prompt")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="prompt", details=feat,
        )

    @ai.command(name="persona")
    @guild_only
    @require_manage_guild
    async def ai_persona(self, ctx: ArchimedesContext, *, name: str = "") -> None:
        """Set or reset the AI persona display name."""
        value = (name or "").strip() or None
        await ctx.db.update_guild_setting(ctx.guild_id, "ai_persona_name", value)
        await ctx.reply_success(
            f"Persona name set to {value}." if value else "Persona name reset to default.",
            title="AI Persona",
        )

    @ai.command(name="clearhistory")
    @guild_only
    @require_manage_guild
    async def ai_clearhistory(self, ctx: ArchimedesContext, member: discord.Member | None = None) -> None:
        """Wipe AI conversation history for a member or the whole server."""
        if member is not None:
            n = await ctx.db.clear_ai_conversation(member.id, ctx.guild_id)
            await ctx.reply_success(f"Cleared {n} row(s) for {member.mention}.", title="AI History")
            return
        if not await ctx.confirm("Clear ALL AI conversation history on this server?"):
            await ctx.reply_error("Cancelled.")
            return
        n = await ctx.db.clear_all_ai_conversations(ctx.guild_id)
        await ctx.reply_success(f"Cleared {n} row(s) server-wide.", title="AI History")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="clearhistory", severity=SEVERITY_WARN, details="scope=guild",
        )

    @ai.command(name="forget", aliases=["forgetme"])
    @guild_only
    async def ai_forget(self, ctx: ArchimedesContext) -> None:
        """Wipe only YOUR stored memory in this server."""
        cleared = await ctx.db.clear_ai_user_memory(ctx.author.id, ctx.guild_id)
        await ctx.reply_success(
            "Your AI memory was wiped. The next conversation builds fresh."
            if cleared else "Nothing to clear -- you had no stored memory.",
            title="Forgotten",
        )

    @ai.command(name="recontext", aliases=["rebuild"])
    @guild_only
    async def ai_recontext(self, ctx: ArchimedesContext, target: str | None = None) -> None:
        """Rebuild Archimedes's context. No arg = you; @user/server/channel need Manage Server."""
        scope = "user"
        member = None
        if target:
            low = target.strip().lstrip("@").lower()
            if low in ("server", "guild", "all"):
                scope = "server"
            elif low in ("channel", "here"):
                scope = "channel"
            else:
                try:
                    member = await commands.MemberConverter().convert(ctx, target)
                except commands.BadArgument:
                    await ctx.reply_error("Use a @member, `server`, or `channel`.")
                    return

        privileged = scope in ("server", "channel") or (
            member is not None and member.id != ctx.author.id
        )
        if privileged and not ctx.author.guild_permissions.manage_guild:
            await ctx.reply_error("That scope needs Manage Server.")
            return

        if scope == "server":
            if not await ctx.confirm("Wipe ALL Archimedes memory for this server?"):
                await ctx.reply_error("Cancelled.")
                return
            deleted = await ctx.db.wipe_ai_guild_state(ctx.guild_id)
            await ctx.reply_success(
                f"Wiped {sum(deleted.values())} row(s) of server AI state.",
                title="Context Rebuilt -- Server",
            )
            await log_staff_action(
                ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
                action="recontext", severity=SEVERITY_DANGER, details="scope=server",
            )
        elif scope == "channel":
            n = await ctx.db.wipe_ai_channel_context(ctx.guild_id, ctx.channel.id)
            await ctx.reply_success(
                f"Cleared {n} channel-context row(s) here.",
                title="Context Rebuilt -- Channel",
            )
        else:
            tgt = member or ctx.author
            deleted = await ctx.db.wipe_ai_user_state(tgt.id, ctx.guild_id)
            await ctx.reply_success(
                f"Wiped {sum(deleted.values())} row(s) of context for {tgt.display_name}.",
                title="Context Rebuilt -- User",
            )

    # ── model picker ──────────────────────────────────────────────────────────
    @ai.group(name="model", invoke_without_command=True)
    @guild_only
    async def ai_model(self, ctx: ArchimedesContext) -> None:
        """Per-guild model defaults. Subcommands: list, show, set, reset."""
        await self.ai_model_list(ctx)

    @ai_model.command(name="list")
    @guild_only
    async def ai_model_list(self, ctx: ArchimedesContext) -> None:
        """Show per-category model defaults."""
        picks = await list_guild_defaults(ctx.db, ctx.guild_id)
        b = card("AI Model Defaults", color=C_PURPLE)
        for cat in TOOL_CATEGORIES:
            pick = picks.get(cat.key)
            current = f"{pick.provider}:{pick.model}" if pick else "env default"
            b.field(cat.label, current, True)
        b.footer("Use .ai model set <category> <provider:model|index>")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_model.command(name="show")
    @guild_only
    async def ai_model_show(self, ctx: ArchimedesContext, category: str) -> None:
        """Show the curated catalog for one category."""
        cat = get_category((category or "").strip().lower())
        if cat is None:
            await ctx.reply_error("Valid: " + ", ".join(c.key for c in TOOL_CATEGORIES))
            return
        catalog = catalog_for(cat.key)
        effective = await resolve_model(ctx.db, ctx.guild_id, cat.key)
        lines = [
            f"{i}. {opt.label or opt.model}  -  `{opt.provider}:{opt.model}`"
            for i, opt in enumerate(catalog, 1)
        ] or ["(no curated entries)"]
        b = card(f"Catalog -- {cat.label}", color=C_PURPLE, description="\n".join(lines))
        b.field("Effective now", f"`{effective.provider}:{effective.model}`", False)
        b.footer(f"Use .ai model set {cat.key} <index|provider:model>")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_model.command(name="set")
    @guild_only
    @require_manage_guild
    async def ai_model_set(self, ctx: ArchimedesContext, category: str, *, value: str) -> None:
        """Set a per-guild model default for a category."""
        cat = get_category((category or "").strip().lower())
        if cat is None:
            await ctx.reply_error("Valid: " + ", ".join(c.key for c in TOOL_CATEGORIES))
            return
        raw = (value or "").strip()
        if raw.isdigit():
            catalog = catalog_for(cat.key)
            idx = int(raw)
            if idx < 1 or idx > len(catalog):
                await ctx.reply_error(f"Index out of range (1-{len(catalog)}).")
                return
            opt = catalog[idx - 1]
            provider, model = opt.provider, opt.model
        elif ":" in raw:
            provider, _, model = raw.partition(":")
            provider, model = provider.strip().lower(), model.strip()
        else:
            await ctx.reply_error("Format: `provider:model` or a catalog index.")
            return
        if provider not in ("openrouter", "ollama") or not model:
            await ctx.reply_error("Provider must be openrouter or ollama.")
            return
        await set_guild_default(
            ctx.db, ctx.guild_id, cat.key, provider, model, updated_by=ctx.author.id,
        )
        await ctx.reply_success(f"{cat.key} default set to `{provider}:{model}`.", title="Model Picker")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="model_set", details=f"{cat.key}={provider}:{model}",
        )

    @ai_model.command(name="reset")
    @guild_only
    @require_manage_guild
    async def ai_model_reset(self, ctx: ArchimedesContext, category: str) -> None:
        """Revert a category to the env default."""
        cat = get_category((category or "").strip().lower())
        if cat is None:
            await ctx.reply_error("Valid: " + ", ".join(c.key for c in TOOL_CATEGORIES))
            return
        await clear_guild_default(ctx.db, ctx.guild_id, cat.key)
        await ctx.reply_success(f"{cat.key} reverted to env default.", title="Model Picker")

    # ── web search ────────────────────────────────────────────────────────────
    @ai.group(name="websearch", invoke_without_command=True)
    @guild_only
    async def ai_websearch(self, ctx: ArchimedesContext) -> None:
        """Web search backend config."""
        await self.ai_websearch_status(ctx)

    @ai_websearch.command(name="status")
    @guild_only
    async def ai_websearch_status(self, ctx: ArchimedesContext) -> None:
        """Show the current web search backend."""
        settings = await ctx.db.get_guild_settings(ctx.guild_id)
        guild_backend = settings.get("search_backend")
        effective = guild_backend or Config.SEARCH_BACKEND
        b = card("Web Search Backend", color=C_PURPLE)
        b.field("Guild override", guild_backend or "not set", True)
        b.field("Env default", Config.SEARCH_BACKEND, True)
        b.field("Effective", effective, True)
        b.field("Brave key", "set" if Config.BRAVE_SEARCH_API_KEY else "not set", True)
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_websearch.command(name="backend")
    @guild_only
    @require_manage_guild
    async def ai_websearch_backend(self, ctx: ArchimedesContext, backend: str) -> None:
        """Set the web search backend (ddg|brave)."""
        val = (backend or "").strip().lower()
        if val not in _SEARCH_BACKENDS:
            await ctx.reply_error(f"Valid: {', '.join(_SEARCH_BACKENDS)}")
            return
        hint = ""
        if val == "brave" and not Config.BRAVE_SEARCH_API_KEY:
            hint = " (BRAVE_SEARCH_API_KEY is not set)"
        await ctx.db.update_guild_setting(ctx.guild_id, "search_backend", val)
        await ctx.reply_success(f"Search backend set to {val}.{hint}", title="Web Search")

    @ai_websearch.command(name="reset")
    @guild_only
    @require_manage_guild
    async def ai_websearch_reset(self, ctx: ArchimedesContext) -> None:
        """Revert the search backend to the env default."""
        await ctx.db.update_guild_setting(ctx.guild_id, "search_backend", None)
        await ctx.reply_success("Search backend reset to env default.", title="Web Search")

    # ── tools ─────────────────────────────────────────────────────────────────
    @ai.group(name="tools", invoke_without_command=True)
    @guild_only
    async def ai_tools(self, ctx: ArchimedesContext) -> None:
        """Agent tool registry."""
        await self.ai_tools_list(ctx)

    @ai_tools.command(name="list")
    @guild_only
    async def ai_tools_list(self, ctx: ArchimedesContext) -> None:
        """List every registered agent tool."""
        registry = self.bot.tools
        specs = sorted(registry.all(), key=lambda s: s.name)
        if not specs:
            await ctx.reply_error("No tools registered.")
            return
        lines = []
        for s in specs:
            risk = _RISK_ICON.get(s.risk, "?")
            state = _STATE_ICON[registry.is_enabled(s.name)]
            lines.append(f"{state}{risk} `{s.name}` -- {clip(s.description, 70)}")
        b = card("Agent Tools", color=C_INFO, description="\n".join(lines))
        b.footer("🟢 read · 🔵 safe · 🟠 mutate · 🔴 danger")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_tools.command(name="info")
    @guild_only
    async def ai_tools_info(self, ctx: ArchimedesContext, *, name: str) -> None:
        """Show the full schema for one tool."""
        spec = self.bot.tools.get((name or "").strip())
        if spec is None:
            await ctx.reply_error(f"Tool `{name}` not found.")
            return
        b = card(f"Tool -- {spec.name}", color=C_INFO)
        b.field("Category", spec.category, True)
        b.field("Risk", spec.risk, True)
        b.field("Enabled", "yes" if self.bot.tools.is_enabled(spec.name) else "no", True)
        b.field("Description", spec.description, False)
        props = (spec.parameters or {}).get("properties", {})
        if props:
            b.field("Parameters", ", ".join(props.keys()), False)
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_tools.command(name="enable")
    @guild_only
    @require_manage_guild
    async def ai_tools_enable(self, ctx: ArchimedesContext, *, name: str) -> None:
        """Enable a tool."""
        if self.bot.tools.get(name.strip()) is None:
            await ctx.reply_error(f"Tool `{name}` not found.")
            return
        self.bot.tools.set_enabled(name.strip(), True)
        await ctx.reply_success(f"Tool `{name.strip()}` enabled.", title="Tools")

    @ai_tools.command(name="disable")
    @guild_only
    @require_manage_guild
    async def ai_tools_disable(self, ctx: ArchimedesContext, *, name: str) -> None:
        """Disable a tool."""
        if self.bot.tools.get(name.strip()) is None:
            await ctx.reply_error(f"Tool `{name}` not found.")
            return
        self.bot.tools.set_enabled(name.strip(), False)
        await ctx.reply_success(f"Tool `{name.strip()}` disabled.", title="Tools")

    # ── plugins ───────────────────────────────────────────────────────────────
    @ai.group(name="plugins", aliases=["plugin"], invoke_without_command=True)
    @guild_only
    async def ai_plugins(self, ctx: ArchimedesContext) -> None:
        """The Lua plugin manager."""
        await self._plugins_list(ctx)

    @ai_plugins.command(name="list", aliases=["ls"])
    @guild_only
    async def ai_plugins_list(self, ctx: ArchimedesContext) -> None:
        """List every installed plugin and its state."""
        await self._plugins_list(ctx)

    async def _plugins_list(self, ctx: ArchimedesContext) -> None:
        mgr = self.bot.plugins
        if mgr is None or not mgr.available:
            await ctx.reply_error("Plugin support is unavailable on this bot.")
            return
        plugins = await mgr.list_plugins()
        if not plugins:
            await ctx.reply(
                embed=card("Lua Plugins", color=C_PURPLE,
                           description="No plugins are installed.").build(),
                mention_author=False,
            )
            return
        lines = []
        for p in plugins:
            icon = "⚪" if not p["enabled"] else ("🟢" if p["loaded"] else "🔴")
            origin = _ORIGIN_ICON.get(p["origin"], "")
            lines.append(
                f"{icon}{origin} `{p['id']}` v{p['version']} -- "
                f"{clip(p['description'] or p['name'], 64)}"
            )
        b = card("Lua Plugins", color=C_PURPLE, description="\n".join(lines))
        b.footer("🟢 active  🔴 failed  ⚪ disabled    📦 bundled  🛰️ marketplace")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_plugins.command(name="info", aliases=["show"])
    @guild_only
    async def ai_plugins_info(self, ctx: ArchimedesContext, plugin_id: str) -> None:
        """Show one plugin's manifest, commands and tools."""
        mgr = self.bot.plugins
        plugin = None
        if mgr is not None:
            plugin = await mgr.get_plugin(plugin_id.strip().lower())
        if plugin is None:
            await ctx.reply_error(f"No plugin `{plugin_id}` is installed.")
            return
        b = card(f"Plugin -- {plugin['name']}", color=C_PURPLE,
                 description=plugin["description"] or "(no description)")
        b.field("ID", plugin["id"], True)
        b.field("Version", plugin["version"], True)
        b.field("Origin", plugin["origin"], True)
        b.field("Category", plugin["category"], True)
        b.field("Author", plugin["author"] or "unknown", True)
        b.field("State", "enabled" if plugin["enabled"] else "disabled", True)
        b.field("Loaded", "yes" if plugin["loaded"] else "no", True)
        if plugin["commands"]:
            b.field("Commands", ", ".join(f"`{c}`" for c in plugin["commands"]))
        if plugin["tools"]:
            b.field("Agent tools", ", ".join(f"`{t}`" for t in plugin["tools"]))
        if plugin["error"]:
            b.field("Last error", clip(plugin["error"], 600))
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_plugins.command(name="search", aliases=["browse", "find"])
    @guild_only
    async def ai_plugins_search(
        self, ctx: ArchimedesContext, *, query: str = "",
    ) -> None:
        """Search the plugin marketplace."""
        from framework.plugins.registry import RegistryError

        mgr = self.bot.plugins
        if mgr is None or not mgr.available:
            await ctx.reply_error("Plugin support is unavailable on this bot.")
            return
        if not mgr.registry.configured:
            await ctx.reply_error("No plugin marketplace is configured.")
            return
        async with ctx.typing():
            try:
                results = await mgr.registry.search(query)
            except RegistryError as exc:
                await ctx.reply_error(f"Marketplace error: {exc}")
                return
        if not results:
            await ctx.reply(
                embed=card("Marketplace", color=C_PURPLE, description=(
                    f"No plugins match `{query}`." if query
                    else "The marketplace catalogue is empty.")).build(),
                mention_author=False,
            )
            return
        installed = {p["id"] for p in await mgr.list_plugins()}
        lines = []
        for entry in results[:25]:
            mark = "  (installed)" if entry.get("id") in installed else ""
            lines.append(
                f"`{entry.get('id')}` v{entry.get('version', '?')} -- "
                f"{clip(str(entry.get('description', '')), 66)}{mark}"
            )
        b = card(f"Marketplace -- {mgr.registry.repo}", color=C_PURPLE,
                 description="\n".join(lines))
        b.footer(f"{len(results)} result(s)  -  "
                 f"{ctx.prefix}ai plugins install <id>")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_plugins.command(name="install", aliases=["add", "get"])
    @guild_only
    @require_manage_guild
    async def ai_plugins_install(
        self, ctx: ArchimedesContext, plugin_id: str,
    ) -> None:
        """Install a plugin from the marketplace."""
        mgr = self.bot.plugins
        if mgr is None:
            await ctx.reply_error("Plugin support is unavailable on this bot.")
            return
        async with ctx.typing():
            result = await mgr.install(plugin_id.strip().lower(),
                                       actor_id=ctx.author.id)
        await ctx.reply_success(result, title="Plugin Install")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="plugin_install", details=plugin_id,
        )

    @ai_plugins.command(name="uninstall", aliases=["remove", "rm"])
    @guild_only
    @require_manage_guild
    async def ai_plugins_uninstall(
        self, ctx: ArchimedesContext, plugin_id: str,
    ) -> None:
        """Uninstall a marketplace plugin."""
        mgr = self.bot.plugins
        if mgr is None:
            await ctx.reply_error("Plugin support is unavailable on this bot.")
            return
        result = await mgr.uninstall(plugin_id.strip().lower())
        await ctx.reply_success(result, title="Plugin Uninstall")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="plugin_uninstall", severity=SEVERITY_WARN, details=plugin_id,
        )

    @ai_plugins.command(name="enable")
    @guild_only
    @require_manage_guild
    async def ai_plugins_enable(
        self, ctx: ArchimedesContext, plugin_id: str,
    ) -> None:
        """Enable an installed plugin and load it."""
        mgr = self.bot.plugins
        if mgr is None:
            await ctx.reply_error("Plugin support is unavailable on this bot.")
            return
        result = await mgr.enable(plugin_id.strip().lower())
        await ctx.reply_success(result, title="Plugins")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="plugin_enable", details=plugin_id,
        )

    @ai_plugins.command(name="disable")
    @guild_only
    @require_manage_guild
    async def ai_plugins_disable(
        self, ctx: ArchimedesContext, plugin_id: str,
    ) -> None:
        """Disable an installed plugin and unload it."""
        mgr = self.bot.plugins
        if mgr is None:
            await ctx.reply_error("Plugin support is unavailable on this bot.")
            return
        result = await mgr.disable(plugin_id.strip().lower())
        await ctx.reply_success(result, title="Plugins")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="plugin_disable", details=plugin_id,
        )

    @ai_plugins.command(name="update", aliases=["upgrade"])
    @guild_only
    @require_manage_guild
    async def ai_plugins_update(
        self, ctx: ArchimedesContext, plugin_id: str,
    ) -> None:
        """Pull the latest version of a marketplace plugin."""
        mgr = self.bot.plugins
        if mgr is None:
            await ctx.reply_error("Plugin support is unavailable on this bot.")
            return
        async with ctx.typing():
            result = await mgr.update(plugin_id.strip().lower())
        await ctx.reply_success(result, title="Plugin Update")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="plugin_update", details=plugin_id,
        )

    @ai_plugins.command(name="reload")
    @guild_only
    @require_manage_guild
    async def ai_plugins_reload(
        self, ctx: ArchimedesContext, plugin_id: str | None = None,
    ) -> None:
        """Recompile and reload one plugin, or every plugin."""
        mgr = self.bot.plugins
        if mgr is None:
            await ctx.reply_error("Plugin support is unavailable on this bot.")
            return
        async with ctx.typing():
            target = plugin_id.strip().lower() if plugin_id else None
            result = await mgr.reload(target)
        await ctx.reply_success(result, title="Plugin Reload")
        await log_staff_action(
            ctx.db, scope=SCOPE_AI, guild_id=ctx.guild_id, actor_id=ctx.author.id,
            action="plugin_reload", details=plugin_id or "all",
        )

    # ── memory ────────────────────────────────────────────────────────────────
    @ai.group(name="memory", aliases=["mem"], invoke_without_command=True)
    @guild_only
    async def ai_memory(self, ctx: ArchimedesContext) -> None:
        """Memory sidecar controls."""
        p = ctx.prefix
        await ctx.reply(
            embed=card("Memory Sidecar", color=C_INFO, description=(
                f"`{p}ai memory facts [scope]` -- list facts\n"
                f"`{p}ai memory remember <scope> <key> <value>`\n"
                f"`{p}ai memory forget <scope> <key>`\n"
                f"`{p}ai memory listen <on|off>` -- passive episode capture"
            )).build(),
            mention_author=False,
        )

    @ai_memory.command(name="facts")
    @guild_only
    async def ai_memory_facts(self, ctx: ArchimedesContext, *, scope: str | None = None) -> None:
        """List long-term facts for a scope (defaults to this guild)."""
        target = scope or guild_scope(ctx.guild_id)
        facts = await self.bot.memory.get_facts(target, limit=20)
        if not facts:
            await ctx.reply(
                embed=card("Facts", color=C_INFO,
                           description=f"No facts in scope `{target}`.").build(),
                mention_author=False,
            )
            return
        b = card(f"Facts -- {target}", color=C_INFO)
        for f in facts:
            b.field(f.key, f"{clip(f.value, 300)}\n_{f.source}, {fmt_ts(f.updated_at)}_", False)
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_memory.command(name="remember")
    @guild_only
    @require_manage_guild
    async def ai_memory_remember(
        self, ctx: ArchimedesContext, scope: str, key: str, *, value: str,
    ) -> None:
        """Manually add or overwrite a fact. scope = guild | user:<id>."""
        target = self._resolve_scope(ctx, scope)
        await self.bot.memory.upsert_fact(target, key, value, confidence=1.0, source="admin")
        await ctx.reply_success(f"Recorded `{key}` in scope `{target}`.", title="Fact saved")

    @ai_memory.command(name="forget")
    @guild_only
    @require_manage_guild
    async def ai_memory_forget(self, ctx: ArchimedesContext, scope: str, *, key: str) -> None:
        """Delete a fact by scope and key."""
        target = self._resolve_scope(ctx, scope)
        status = await ctx.db.execute(
            "DELETE FROM archimedes_facts WHERE scope=$1 AND key=$2", target, key,
        )
        await ctx.reply_success(f"Removed `{key}` from `{target}` ({status}).", title="Fact removed")

    @ai_memory.command(name="listen")
    @guild_only
    @require_manage_guild
    async def ai_memory_listen(self, ctx: ArchimedesContext, setting: str) -> None:
        """Toggle passive episode capture in this channel (on|off)."""
        val = (setting or "").strip().lower()
        if val in ("on", "enable", "true", "1"):
            await ctx.db.execute(
                "INSERT INTO archimedes_passive_channels (guild_id, channel_id, enabled_by) "
                "VALUES ($1,$2,$3) ON CONFLICT DO NOTHING",
                ctx.guild_id, ctx.channel.id, ctx.author.id,
            )
            await ctx.reply_success("Passive capture is now ON in this channel.", title="Listening")
        elif val in ("off", "disable", "false", "0"):
            await ctx.db.execute(
                "DELETE FROM archimedes_passive_channels WHERE guild_id=$1 AND channel_id=$2",
                ctx.guild_id, ctx.channel.id,
            )
            await ctx.reply_success("Passive capture is now OFF in this channel.", title="Silent")
        else:
            await ctx.reply_error("Use `on` or `off`.")

    def _resolve_scope(self, ctx: ArchimedesContext, scope: str) -> str:
        low = (scope or "").strip().lower()
        if low in ("guild", "server"):
            return guild_scope(ctx.guild_id)
        if low.startswith("user:"):
            try:
                return user_scope(int(low.split(":", 1)[1]), ctx.guild_id)
            except ValueError:
                pass
        return scope

    # ── emoji index ───────────────────────────────────────────────────────────
    @ai.group(name="emojis", invoke_without_command=True)
    @guild_only
    async def ai_emojis(self, ctx: ArchimedesContext) -> None:
        """Custom emoji meaning index."""
        await self.ai_emojis_stats(ctx)

    @ai_emojis.command(name="stats")
    @guild_only
    async def ai_emojis_stats(self, ctx: ArchimedesContext) -> None:
        """Show index coverage."""
        from ai.emoji_index import DEFAULT_MAX_AGE_DAYS

        total = len(getattr(ctx.guild, "emojis", []) or [])
        rows = await ctx.db.get_all_emoji_meanings(ctx.guild_id)
        stale = await ctx.db.get_stale_emoji_meaning_ids(ctx.guild_id, DEFAULT_MAX_AGE_DAYS)
        b = card("Emoji Index", color=C_NAVY)
        b.field("Server emojis", str(total), True)
        b.field("Indexed", f"{len(rows)} / {total}", True)
        b.field("Stale", str(len(stale)), True)
        b.footer(f"{ctx.prefix}ai emojis index to refresh")
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_emojis.command(name="index")
    @guild_only
    @require_manage_guild
    async def ai_emojis_index(self, ctx: ArchimedesContext, flag: str | None = None) -> None:
        """Index this server's custom emojis (pass 'force' to re-index all)."""
        from ai.emoji_index import DEFAULT_MAX_AGE_DAYS, index_guild

        if not getattr(ctx.guild, "emojis", None):
            await ctx.reply_error("This server has no custom emojis.")
            return
        force = bool(flag and flag.lstrip("-").lower() in ("force", "f", "all"))
        await ctx.reply_success(
            "Indexing emojis -- vision synthesis takes a few seconds each.",
            title="Emoji Index",
        )
        try:
            stats = await index_guild(
                ctx.db, ctx.guild, force=force, max_age_days=DEFAULT_MAX_AGE_DAYS,
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("emoji index failed")
            await ctx.reply_error(f"Index failed: {exc}")
            return
        b = card("Emoji Index Complete",
                 color=C_WARNING if stats["vision_down"] else C_SUCCESS)
        b.field("Total", str(stats["total"]), True)
        b.field("Indexed", str(stats["indexed"]), True)
        b.field("Skipped", str(stats["skipped"]), True)
        if stats["failed"]:
            b.field("Failed", str(stats["failed"]), True)
        if stats["pruned"]:
            b.field("Pruned", str(stats["pruned"]), True)
        await ctx.reply(embed=b.build(), mention_author=False)

    @ai_emojis.command(name="show")
    @guild_only
    async def ai_emojis_show(self, ctx: ArchimedesContext) -> None:
        """Browse indexed emoji meanings."""
        rows = await ctx.db.get_all_emoji_meanings(ctx.guild_id)
        if not rows:
            await ctx.reply_error("No emoji meanings indexed. Run `.ai emojis index`.")
            return
        live = {int(e.id): e for e in ctx.guild.emojis}
        pages: list[discord.Embed] = []
        per_page = 12
        for start in range(0, len(rows), per_page):
            chunk = rows[start:start + per_page]
            lines = []
            for r in chunk:
                emoji = live.get(int(r["emoji_id"]))
                raw = str(emoji) if emoji else f":{r['name']}:"
                lines.append(f"{raw} **{r['name']}** -- {clip(r['description'], 120)}")
            pages.append(card(
                f"Emoji Meanings ({start + 1}-{start + len(chunk)} of {len(rows)})",
                color=C_INFO, description="\n\n".join(lines),
            ).build())
        await ctx.paginate(pages)

    @ai_emojis.command(name="set")
    @guild_only
    @require_manage_guild
    async def ai_emojis_set(
        self, ctx: ArchimedesContext, emoji: discord.Emoji, *, description: str,
    ) -> None:
        """Manually override the stored description for one emoji."""
        if emoji.guild_id != ctx.guild_id:
            await ctx.reply_error("That emoji isn't from this server.")
            return
        await ctx.db.upsert_emoji_meaning(
            ctx.guild_id, int(emoji.id), emoji.name, description.strip()[:220],
            animated=bool(emoji.animated), source="manual",
        )
        await ctx.reply_success(f"Meaning set for {emoji}.", title="Emoji Meaning")

    # ── audit ─────────────────────────────────────────────────────────────────
    @ai.command(name="audit")
    @guild_only
    @require_manage_guild
    async def ai_audit(self, ctx: ArchimedesContext, limit: int = 25) -> None:
        """Show recent AI-scope staff actions."""
        limit = max(1, min(100, int(limit)))
        rows = await recent_staff_actions(
            ctx.db, guild_id=ctx.guild_id, scope=SCOPE_AI, limit=limit,
        )
        if not rows:
            await ctx.reply(
                embed=card("AI Audit", color=C_NAVY,
                           description="No audit entries yet.").build(),
                mention_author=False,
            )
            return
        lines = []
        for r in rows:
            actor = f"<@{r['actor_id']}>"
            lines.append(
                f"`{fmt_ts(r['created_at'])}` {actor} **{r['action']}** "
                f"{clip(r['details'], 80)}"
            )
        b = card("AI Audit", color=C_NAVY, description="\n".join(lines))
        await ctx.reply(embed=b.build(), mention_author=False)


async def setup(bot) -> None:
    await bot.add_cog(AIAdmin(bot))
