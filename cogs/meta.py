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
    C_BLURPLE, C_GOLD, C_INFO, C_PURPLE, C_TEAL, CategoryPaginator,
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
            f"(for example `{p}note`, `{p}task`).",
        )
        .field(
            "Bot meta",
            f"`/help` -- this menu (also `{p}help`)\n"
            f"`{p}ping` -- check the bot's latency\n"
            f"`{p}about` -- version and backend info",
        )
        .field(
            "Good to know",
            "Personal notes, tasks and events are private: Archimedes "
            "answers them in your DMs and tidies the command away. Group "
            "items are shared and answered in the channel.",
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

    cats["Notes (.note)"] = _render([
        card(
            f"Notes -- {p}note",
            color=C_INFO,
            description=(
                "Private notes, kept per user across every server. "
                "Archimedes answers note commands in your DMs."
            ),
        )
        .field(f"`{p}note` or `{p}note list`", "List your notes.")
        .field(
            f"`{p}note add <text>`",
            f"Add a note. The first line becomes the title, the rest is the "
            f"body.\nExample: `{p}note add Wifi -- guest network is open`",
        )
        .field(
            f"`{p}note show <id>`",
            f"Open a single note.\nExample: `{p}note show 12`",
        )
        .field(f"`{p}note edit <id> <text>`", "Replace a note's text.")
        .field(f"`{p}note del <id>`", "Delete a note.")
        .field(
            f"`{p}note share <id> @user [edit]`",
            f"Share a note with someone. Add `edit` to let them change it.\n"
            f"Example: `{p}note share 12 @Sam edit`",
        )
        .field(
            f"`{p}note unshare <id> @user`",
            "Stop sharing a note with that user.",
        )
        .field(
            f"`{p}note copy <id> <dest>` / `{p}note move <id> <dest>`",
            f"Copy or move a note. Destination is `me`, an `@user`, or "
            f"`#<groupid>`.\nExample: `{p}note copy 12 #5`",
        )
        .field(
            "Target a group",
            f"Start the text with `#<groupid>` to file the note in a group.\n"
            f"Example: `{p}note add #5 Buy projector cable`",
        ),
    ], "Notes")

    cats["Tasks (.task)"] = _render([
        card(
            f"Tasks -- {p}task  (basics)",
            color=C_TEAL,
            description=(
                f"Tasks organised into named lists. `{p}task`, `{p}tasks` "
                f"and `{p}todo` all work."
            ),
        )
        .field(
            f"`{p}task` or `{p}task list [~list]`",
            f"List your tasks. Add a list name to filter.\n"
            f"Example: `{p}task list ~shopping`",
        )
        .field(
            f"`{p}task add [~list] <text>`",
            f"Add a task. `~<list>` picks a list (default `general`).\n"
            f"Example: `{p}task add ~shopping milk and eggs`",
        )
        .field(
            f"`{p}task lists`",
            "Show every task list and how many tasks are open in each.",
        )
        .field(
            f"`{p}task done <id>`",
            f"Mark a task done.\nExample: `{p}task done 7`",
        )
        .field(f"`{p}task undone <id>`", "Reopen a completed task.")
        .field(
            "Lists and groups",
            f"`~<name>` targets a list; `#<groupid>` targets a group.\n"
            f"Example: `{p}task add #5 ~launch ship the build`",
        ),
        card(
            f"Tasks -- {p}task  (dates, reminders and sharing)",
            color=C_TEAL,
        )
        .field(
            f"`{p}task due <id> <when>`",
            f"Set a due date; `clear` removes it.\n"
            f"Example: `{p}task due 7 2026-06-01 14:30`",
        )
        .field(
            f"`{p}task remind <id> <when>`",
            f"Set a reminder; Archimedes DMs you when it falls due.\n"
            f"Example: `{p}task remind 7 in 2h`",
        )
        .field(
            "Time formats",
            "Relative: `in 30m`, `in 2h`, `in 3d`, `in 1w`. "
            "Absolute (UTC): `2026-06-01` or `2026-06-01 14:30`.",
        )
        .field(f"`{p}task edit <id> <text>`", "Replace a task's text.")
        .field(f"`{p}task del <id>`", "Delete a task.")
        .field(
            f"`{p}task share <id> @user [edit]`",
            f"Share a task, optionally with edit access. "
            f"`{p}task unshare <id> @user` stops it.",
        )
        .field(
            f"`{p}task copy <id> <dest>` / `{p}task move <id> <dest>`",
            "Copy or move a task to `me`, an `@user`, or `#<groupid>`.",
        ),
    ], "Tasks")

    cats["Events (.event)"] = _render([
        card(
            f"Events -- {p}event",
            color=C_GOLD,
            description=(
                f"Calendar events with optional reminders. `{p}event`, "
                f"`{p}cal` and `{p}calendar` all work."
            ),
        )
        .field(f"`{p}event` or `{p}event list`", "List your upcoming events.")
        .field(
            f"`{p}event add <when> | <title>`",
            f"Add an event. Put the time before the `|`.\n"
            f"Example: `{p}event add in 2d | Team sync`",
        )
        .field(
            f"`{p}event show <id>`",
            f"Open a single event.\nExample: `{p}event show 4`",
        )
        .field(
            f"`{p}event when <id> <when>`",
            f"Reschedule an event.\n"
            f"Example: `{p}event when 4 2026-07-01 09:00`",
        )
        .field(
            f"`{p}event remind <id> <when>`",
            f"Set a reminder; `clear` removes it.\n"
            f"Example: `{p}event remind 4 in 1h`",
        )
        .field(f"`{p}event edit <id> <text>`", "Replace an event's text.")
        .field(f"`{p}event del <id>`", "Delete an event.")
        .field(
            f"`{p}event share <id> @user [edit]`",
            f"Share an event. `{p}event unshare <id> @user` stops it.",
        )
        .field(
            f"`{p}event copy <id> <dest>` / `{p}event move <id> <dest>`",
            "Copy or move an event to `me`, an `@user`, or `#<groupid>`.",
        ),
    ], "Events")

    cats["Groups (.group)"] = _render([
        card(
            f"Groups -- {p}group  (members)",
            color=C_BLURPLE,
            description=(
                "A group is a shared space for notes, tasks and events. "
                "Every member can see and edit the group's items, and "
                "replies post in the channel. You can be in many groups."
            ),
        )
        .field(f"`{p}group` or `{p}group list`", "List the groups you belong to.")
        .field(
            f"`{p}group create <name>`",
            f"Create a group in this server.\n"
            f"Example: `{p}group create Launch crew`",
        )
        .field(
            f"`{p}group show <id>`",
            "Show a group's members and how many items it holds.",
        )
        .field(
            f"`{p}group invite <id> @user`",
            f"Invite someone to a group you own.\n"
            f"Example: `{p}group invite 5 @Sam`",
        )
        .field(f"`{p}group invites`", "List group invitations waiting for you.")
        .field(
            f"`{p}group join <id>` / `{p}group decline <id>`",
            "Accept or decline a pending invitation.",
        )
        .field(f"`{p}group leave <id>`", "Leave a group you are in.")
        .field(
            f"`{p}group kick <id> @user`",
            "Remove a member from a group you own.",
        ),
        card(
            f"Groups -- {p}group  (admin and sharing)",
            color=C_BLURPLE,
        )
        .field(f"`{p}group rename <id> <name>`", "Rename a group you own.")
        .field(
            f"`{p}group transfer <id> @user`",
            "Hand ownership of the group to another member.",
        )
        .field(
            f"`{p}group delete <id>`",
            "Delete a group you own and every item inside it.",
        )
        .field(
            f"`{p}group duplicate <id>`",
            "Clone a group's items into a fresh group you own.",
        )
        .field(
            "Filing items in a group",
            f"Start an `add` or `list` argument with `#<groupid>`.\n"
            f"Example: `{p}task add #5 ship the release notes`",
        )
        .field(
            "Moving items around",
            "The `copy` and `move` subcommands on any note, task or event "
            "take `me`, an `@user`, or `#<groupid>` as the destination.",
        ),
    ], "Groups")

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
            "Tools and search",
            f"`{p}ai websearch` -- web search backend\n"
            f"`{p}ai tools` -- enable or disable agent tools\n"
            f"`{p}ai plugins` / `{p}ai reloadtools` -- Lua plugins",
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

    @commands.command(name="help")
    async def help_cmd(self, ctx: ArchimedesContext) -> None:
        """Browse everything Archimedes can do, with usage and examples."""
        await CategoryPaginator.send(ctx, build_help_categories(Config.PREFIX))

    @app_commands.command(
        name="help",
        description="Browse everything Archimedes can do, with usage and examples.",
    )
    async def help_slash(self, interaction: discord.Interaction) -> None:
        """The /help slash command: the same catalogue as .help."""
        await CategoryPaginator.respond(
            interaction, build_help_categories(Config.PREFIX),
        )

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
