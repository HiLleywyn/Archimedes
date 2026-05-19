"""cogs/meta.py -- help, ping and about for the bot itself.

The `/help` slash command and the `.help` prefix command both render the
same catalogue: a dropdown of sections, each paginated with Prev / Next
buttons, every command shown with its usage and a worked example.
"""
from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands

from config import Config
from framework.context import ArchimedesContext
from framework.embed import CardBuilder, card
from framework.ui import (
    C_BLURPLE, C_INFO, C_PURPLE, C_TEAL, CategoryPaginator,
)


def _render(builders: list[CardBuilder], hint: str) -> list[discord.Embed]:
    """Stamp a shared footer (with a page counter) on each card and build it."""
    total = len(builders)
    pages: list[discord.Embed] = []
    for i, b in enumerate(builders):
        b.footer(f"{hint}  -  page {i + 1}/{total}" if total > 1 else hint)
        pages.append(b.build())
    return pages


def build_help_categories(p: str) -> dict[str, list[discord.Embed]]:
    """Build the full help catalogue keyed by section name.

    ``p`` is the command prefix; every example is rendered with it so the
    help stays correct even if the prefix is changed.
    """
    cats: dict[str, list[discord.Embed]] = {}

    cats["Getting started"] = _render([
        card(
            "Archimedes -- help",
            color=C_PURPLE,
            description=(
                "Archimedes is a memory-backed AI chat companion. It learns "
                "who it is talking to and answers in a persona-driven voice.\n\n"
                "Pick a section from the menu below, then use **Prev** and "
                "**Next** to page through it."
            ),
        )
        .field(
            "Two ways to use it",
            f"**Chat** -- `@`mention Archimedes or reply to one of its "
            f"messages.\n"
            f"**Commands** -- prefix commands start with `{p}` "
            f"(for example `{p}help`, `{p}coinflip`).",
        )
        .field(
            "Bot meta",
            f"`/help` -- this menu (also `{p}help`)\n"
            f"`{p}ping` -- check the bot's latency\n"
            f"`{p}about` -- version and backend info",
        )
        .field(
            "Good to know",
            "Archimedes can be extended with Lua plugins that add extra "
            "commands and tools. Server moderators install them; see the "
            "Plugins section below.",
        ),
    ], "Getting started")

    cats["Chatting with Archimedes"] = _render([
        card(
            "Chatting with Archimedes",
            color=C_INFO,
            description="Talk to Archimedes in plain language -- no command needed.",
        )
        .field(
            "Mention it",
            "`@Archimedes how do I center a div?`\n"
            "Archimedes answers with a memory-backed reply and remembers the "
            "exchange for next time.",
        )
        .field(
            "Reply to continue",
            "Reply to any Archimedes message to keep the same conversation "
            "going -- you do not need to mention it again.",
        )
        .field(
            f"`{p}ask <question>`",
            f"Ask a question without a mention. It still reads your "
            f"conversation history for context.\n"
            f"Example: `{p}ask summarise the rules of chess`",
        )
        .field(
            "Regenerate and Continue",
            "Every reply carries buttons: **Regenerate** for a fresh take, "
            "**Continue** to extend an answer that was cut off.",
        )
        .field(
            "Reply style",
            f"`{p}arch chat` keeps replies inline in the channel; "
            f"`{p}arch threads` moves them into a thread. See the "
            f"`{p}arch` section.",
        ),
    ], "Chatting")

    cats["Settings (.arch)"] = _render([
        card(
            f"Your settings -- {p}arch",
            color=C_PURPLE,
            description=(
                f"`{p}arch` (aliases `{p}a`, `{p}archimedes`) tunes how "
                f"Archimedes treats you and shows what it has learned."
            ),
        )
        .field(f"`{p}arch`", "Open your settings overview.")
        .field(f"`{p}arch chat`", "Archimedes replies inline in the channel.")
        .field(
            f"`{p}arch threads`",
            "Archimedes replies inside its own thread to keep channels tidy.",
        )
        .field(
            f"`{p}arch ctx`",
            f"Show what Archimedes has learned about you here.\n"
            f"Example: `{p}arch ctx`",
        )
        .field(
            f"`{p}arch ctx @user`",
            "Look up what Archimedes has learned about another member.",
        )
        .field(
            f"`{p}arch ctx server`",
            "Show server-wide context: facts and recent episodes.",
        )
        .field(
            f"`{p}arch ctx clear`",
            "Wipe everything Archimedes learned about you in this server.",
        ),
        card(
            "Your settings -- saved answers and privacy",
            color=C_PURPLE,
        )
        .field(
            f"`{p}arch save`",
            f"Reply to one of Archimedes's messages with `{p}arch save` to "
            f"bookmark that question-and-answer pair.",
        )
        .field(
            f"`{p}arch saved [num]`",
            f"Browse your bookmarks, or open one by its number.\n"
            f"Example: `{p}arch saved 0`",
        )
        .field(
            f"`{p}arch unsave <num>`",
            f"Drop a bookmark by its number.\nExample: `{p}arch unsave 2`",
        )
        .field(
            f"`{p}arch optout`",
            "Stop Archimedes learning about you and wipe what it knows.",
        )
        .field(
            f"`{p}arch optin`",
            "Opt back in. Everyone starts opted in.",
        ),
    ], "Settings")

    cats["Plugins"] = _render([
        card(
            "Plugins",
            color=C_TEAL,
            description=(
                "Archimedes is extended with Lua plugins -- each one adds "
                "prefix commands, agent tools the model can call, or both. "
                "The `coinflip` plugin ships built in as a worked example; "
                "this menu grows a live section for every plugin that is "
                "loaded, so the help always matches what is installed."
            ),
        )
        .field(
            "What a plugin adds",
            "A plugin can register prefix commands you run yourself and "
            "agent tools Archimedes calls for you mid-conversation. Every "
            "loaded plugin gets its own section in this menu.",
        )
        .field(
            "The marketplace",
            f"Beyond the built-in `coinflip`, more plugins -- a notes, "
            f"tasks, events and groups productivity suite among them -- "
            f"install from the marketplace. Browse it with "
            f"`{p}ai plugins search`.",
        )
        .field(
            "Managing plugins",
            f"Server moderators install, update, enable and disable plugins "
            f"with `{p}ai plugins` -- see the Staff controls section.",
        ),
    ], "Plugins")

    cats["Staff controls (.ai)"] = _render([
        card(
            f"Staff controls -- {p}ai",
            color=C_BLURPLE,
            description=(
                f"The AI control surface for moderators. Most subcommands "
                f"need the **Manage Server** permission. Run `{p}ai help` "
                f"for the full, paginated reference."
            ),
        )
        .field(
            "Configuration",
            f"`{p}ai status` -- feature flags and provider status\n"
            f"`{p}ai toggle <feature>` -- turn a feature on or off\n"
            f"`{p}ai test` -- send a test prompt to the model",
        )
        .field(
            "Voice",
            f"`{p}ai prompt <feature> <text>` -- custom system prompts\n"
            f"`{p}ai persona <name>` -- set the persona name\n"
            f"`{p}ai model` -- the per-guild model picker",
        )
        .field(
            "Tools and plugins",
            f"`{p}ai websearch` -- web search backend\n"
            f"`{p}ai tools` -- enable or disable agent tools\n"
            f"`{p}ai plugins` -- install, update and manage Lua plugins",
        )
        .field(
            "Memory and emojis",
            f"`{p}ai memory` -- long-term facts and passive capture\n"
            f"`{p}ai emojis` -- the custom emoji meaning index",
        )
        .field(
            "Housekeeping",
            f"`{p}ai audit` -- the recent staff audit feed\n"
            f"`{p}ai clearhistory` / `{p}ai forget` / `{p}ai recontext` -- "
            f"reset learned context",
        ),
    ], "Staff controls")

    return cats


class Meta(commands.Cog):
    """Bot meta commands: help, ping, about."""

    def __init__(self, bot) -> None:
        self.bot = bot

    def _catalogue(self) -> dict[str, list[discord.Embed]]:
        """The static help plus a live section for every loaded plugin.

        Plugin sections slot in right after the static ``Plugins`` page so
        the menu reads: built-in topics, then one section per loaded plugin
        (coinflip, plus anything installed), then the staff controls.
        """
        prefix = Config.PREFIX
        static = build_help_categories(prefix)
        plugin_cats: dict[str, list[discord.Embed]] = {}
        if getattr(self.bot, "plugins", None) is not None:
            try:
                plugin_cats = self.bot.plugins.help_categories(prefix)
            except Exception:  # noqa: BLE001
                plugin_cats = {}
        if not plugin_cats:
            return static
        merged: dict[str, list[discord.Embed]] = {}
        for name, pages in static.items():
            merged[name] = pages
            if name == "Plugins":
                for pname, ppages in plugin_cats.items():
                    merged[pname] = ppages
        return merged

    @commands.command(name="help")
    async def help_cmd(self, ctx: ArchimedesContext) -> None:
        """Browse everything Archimedes can do, with usage and examples."""
        await CategoryPaginator.send(ctx, self._catalogue())

    @app_commands.command(
        name="help",
        description="Browse everything Archimedes can do, with usage and examples.",
    )
    async def help_slash(self, interaction: discord.Interaction) -> None:
        """The /help slash command: the same catalogue as .help."""
        await CategoryPaginator.respond(interaction, self._catalogue())

    @commands.command(name="ping")
    async def ping_cmd(self, ctx: ArchimedesContext) -> None:
        """Check the bot's latency."""
        start = time.monotonic()
        msg = await ctx.reply("Pinging...", mention_author=False)
        rtt = (time.monotonic() - start) * 1000
        gateway = self.bot.latency * 1000
        await msg.edit(content=None, embed=card(
            "Pong",
            color=C_INFO,
            description=f"Gateway: **{gateway:.0f}ms**\nRound-trip: **{rtt:.0f}ms**",
        ).build())

    @commands.command(name="about", aliases=["info"])
    async def about_cmd(self, ctx: ArchimedesContext) -> None:
        """About this bot."""
        b = (
            card("About Archimedes", color=C_PURPLE, description=(
                "A standalone AI chat bot: memory-backed conversation with "
                "per-user, per-channel and per-server context learning."
            ))
            .field("Backend", Config.CHAT_BACKEND, True)
            .field("Servers", str(len(self.bot.guilds)), True)
            .field("Prefix", f"`{Config.PREFIX}`", True)
        )
        await ctx.reply(embed=b.build(), mention_author=False)


async def setup(bot) -> None:
    await bot.add_cog(Meta(bot))
