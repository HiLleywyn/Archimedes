"""cogs/productivity.py -- shared notes, tasks, events and groups.

Four prefix-command groups give every member a private productivity space and
a way to collaborate:

    .note   -- notes
    .task   -- tasks organised into named lists
    .event  -- calendar events
    .group  -- groups: invite members, share, transfer between owners

Privacy model. Every item has an owner. Personal items (owned by a user) are
private: the bot answers in the invoker's DMs and tidies the command message
away. Group items are shared with all group members and the bot answers in
the channel. A personal item can also be shared with specific users.

Scope tokens accepted at the start of an ``add`` / ``list`` argument:

    #<id>   -- target group <id> instead of your personal space
    ~<name> -- target task list <name> (tasks only; default list is 'general')

Reminders. Any task or event can carry a reminder time; a one-minute loop
DMs the owner (or every group member) when it falls due.
"""
from __future__ import annotations

import datetime as dt
import logging
import re

import discord
from discord.ext import commands, tasks

from framework.context import ArchimedesContext
from framework.embed import card
from framework.middleware import guild_only, no_bots
from framework.ui import (
    C_BLURPLE, C_ERROR, C_GOLD, C_INFO, C_NEUTRAL, C_SUCCESS, C_TEAL,
    Paginator, clip,
)

log = logging.getLogger(__name__)

# How often the reminder loop scans for due items.
_REMINDER_INTERVAL_S = 60
# Items shown per page in a list view.
_PAGE_SIZE = 10

_KIND_LABEL = {"note": "Note", "task": "Task", "event": "Event"}
_KIND_COLOR = {"note": C_INFO, "task": C_TEAL, "event": C_GOLD}

# Leading scope tokens on an add/list argument.
_SIGIL_RE = re.compile(r"^\s*(#\d+|~\S+)\s+")
# Relative time, e.g. "in 2h", "in 30 mins", "in 3 days".
_REL_RE = re.compile(
    r"^in\s+(\d+)\s*"
    r"(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|"
    r"d|day|days|w|week|weeks)$",
    re.IGNORECASE,
)


# ── pure helpers ───────────────────────────────────────────────────────────
def _parse_when(text: str) -> dt.datetime | None:
    """Parse a time string into a timezone-aware UTC datetime, or None.

    Accepts relative offsets (``in 30m``, ``in 2h``, ``in 3d``, ``in 1w``)
    and absolute ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM`` (treated as UTC).
    """
    text = (text or "").strip()
    if not text:
        return None
    rel = _REL_RE.match(text)
    if rel:
        amount = int(rel.group(1))
        unit = rel.group(2).lower()
        now = dt.datetime.now(dt.timezone.utc)
        if unit.startswith("m"):
            return now + dt.timedelta(minutes=amount)
        if unit.startswith("h"):
            return now + dt.timedelta(hours=amount)
        if unit.startswith("w"):
            return now + dt.timedelta(weeks=amount)
        return now + dt.timedelta(days=amount)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=dt.timezone.utc)
    return None


def _fmt_dt(value) -> str:
    """Render a datetime/epoch as a Discord timestamp (viewer-local time)."""
    if value is None:
        return "no date"
    try:
        if isinstance(value, (int, float)):
            epoch = int(value)
        else:
            epoch = int(value.timestamp())
    except (ValueError, OSError, OverflowError, AttributeError):
        return str(value)
    return f"<t:{epoch}:f>"


def _split_sigils(text: str) -> tuple[int | None, str | None, str]:
    """Peel leading ``#<id>`` / ``~<list>`` tokens off ``text``.

    Returns ``(group_id, list_name, remaining_text)``. Newlines in the
    remaining text are preserved so note bodies survive intact.
    """
    group_id: int | None = None
    list_name: str | None = None
    text = text or ""
    while True:
        match = _SIGIL_RE.match(text)
        if not match:
            break
        token = match.group(1)
        if token.startswith("#") and group_id is None:
            group_id = int(token[1:])
        elif token.startswith("~") and list_name is None:
            list_name = token[1:].lower()
        else:
            break
        text = text[match.end():]
    return group_id, list_name, text.strip()


# ── cog ────────────────────────────────────────────────────────────────────
class Productivity(commands.Cog):
    """Notes, tasks, calendar events and shareable groups."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._reminder_loop.start()

    def cog_unload(self) -> None:
        self._reminder_loop.cancel()

    # ── scope + access ─────────────────────────────────────────────────────
    async def _scope_for(
        self, ctx: ArchimedesContext, group_id: int | None,
    ) -> tuple[str, int]:
        """Resolve a scope token to ``(owner_kind, owner_id)``."""
        if group_id is None:
            return "user", ctx.author.id
        group = await ctx.db.get_productivity_group(group_id)
        if group is None:
            raise commands.BadArgument(f"There is no group #{group_id}.")
        if not await ctx.db.is_group_member(group_id, ctx.author.id):
            raise commands.BadArgument(
                f"You are not a member of group #{group_id}."
            )
        return "group", group_id

    async def _require_item(
        self, ctx: ArchimedesContext, item_id: int, *, need_edit: bool = False,
    ) -> tuple[dict, bool]:
        """Fetch an item the invoker may access, or raise a friendly error.

        Returns ``(item, can_edit)``.
        """
        item = await ctx.db.get_item(item_id)
        if item is None:
            raise commands.BadArgument(f"There is no item #{item_id}.")
        uid = ctx.author.id
        if item["owner_kind"] == "group":
            if not await ctx.db.is_group_member(item["owner_id"], uid):
                raise commands.BadArgument(
                    "That item belongs to a group you are not in."
                )
            return item, True
        if item["owner_id"] == uid:
            return item, True
        share = await ctx.db.get_item_share(item_id, uid)
        if share is None:
            raise commands.BadArgument("You do not have access to that item.")
        if need_edit and not share["can_edit"]:
            raise commands.BadArgument(
                "That item is shared with you as view-only."
            )
        return item, bool(share["can_edit"])

    async def _parse_dest(
        self, ctx: ArchimedesContext, token: str,
    ) -> tuple[str, int]:
        """Resolve a copy/move destination: ``me``, an @mention, or ``#id``."""
        token = (token or "").strip()
        first = token.split()[0] if token.split() else ""
        if first.lower() in ("me", "self", "mine"):
            return "user", ctx.author.id
        if first.startswith("#") and first[1:].isdigit():
            return await self._scope_for(ctx, int(first[1:]))
        mentions = [m for m in ctx.message.mentions if m.id != self.bot.user.id]
        if mentions:
            return "user", mentions[0].id
        raise commands.BadArgument(
            "Destination must be `me`, an @mention, or `#<groupid>`."
        )

    @staticmethod
    def _is_private(scope_kind: str) -> bool:
        """Personal scope is delivered privately; group scope in-channel."""
        return scope_kind != "group"

    # ── delivery ───────────────────────────────────────────────────────────
    async def _deliver(
        self, ctx: ArchimedesContext, pages: list[discord.Embed], *,
        private: bool,
    ) -> None:
        """Send embeds. Private -> DM the invoker; otherwise reply in-channel."""
        if not pages:
            pages = [card("Nothing here", color=C_NEUTRAL).build()]
        if not private:
            if len(pages) == 1:
                await ctx.reply(embed=pages[0], mention_author=False)
            else:
                await ctx.paginate(pages)
            return
        try:
            channel = ctx.author.dm_channel or await ctx.author.create_dm()
            if len(pages) == 1:
                await channel.send(embed=pages[0])
            else:
                view = Paginator(pages, author_id=ctx.author.id)
                await channel.send(embed=pages[0], view=view)
        except discord.Forbidden:
            await ctx.reply_error(
                "I could not DM you. Turn on direct messages from server "
                "members and run that again."
            )
            return
        if ctx.guild is not None:
            try:
                await ctx.message.delete()
            except discord.HTTPException:
                pass
            try:
                await ctx.send(
                    embed=card(
                        "Check your DMs",
                        description="Sent that to you privately.",
                        color=C_NEUTRAL,
                    ).build(),
                    delete_after=8,
                )
            except discord.HTTPException:
                pass

    # ── embeds ─────────────────────────────────────────────────────────────
    def _item_line(self, item: dict) -> str:
        kind = item["kind"]
        title = clip(item["title"], 80)
        head = f"`#{item['id']}`"
        if kind == "task":
            box = "[x]" if item["done"] else "[ ]"
            due = f"  due {_fmt_dt(item['due_at'])}" if item.get("due_at") else ""
            return f"{head} {box} {title}{due}"
        if kind == "event":
            return f"{head} {title} -- {_fmt_dt(item.get('due_at'))}"
        return f"{head} {title}"

    def _list_pages(
        self, title: str, items: list[dict], *, color: int, empty: str,
        footer: str = "",
    ) -> list[discord.Embed]:
        if not items:
            return [card(title, description=empty, color=C_NEUTRAL).build()]
        pages: list[discord.Embed] = []
        total = len(items)
        for start in range(0, total, _PAGE_SIZE):
            chunk = items[start:start + _PAGE_SIZE]
            body = "\n".join(self._item_line(it) for it in chunk)
            builder = card(title, description=clip(body, 4096), color=color)
            tail = footer or f"{total} item(s)"
            page_no = start // _PAGE_SIZE + 1
            pages_n = (total + _PAGE_SIZE - 1) // _PAGE_SIZE
            if pages_n > 1:
                tail = f"{tail}  -  page {page_no}/{pages_n}"
            builder.footer(tail)
            pages.append(builder.build())
        return pages

    async def _item_embed(self, ctx: ArchimedesContext, item: dict) -> discord.Embed:
        kind = item["kind"]
        label = _KIND_LABEL.get(kind, "Item")
        builder = card(
            f"{label} #{item['id']}: {clip(item['title'], 200)}",
            color=_KIND_COLOR.get(kind, C_INFO),
        )
        if item.get("body"):
            builder.field("Details", clip(item["body"], 1024), False)
        if kind == "task":
            builder.field("Status", "done" if item["done"] else "open", True)
            builder.field("List", item.get("list_name") or "general", True)
        if item.get("due_at"):
            builder.field(
                "Event time" if kind == "event" else "Due",
                _fmt_dt(item["due_at"]), True,
            )
        if item.get("remind_at"):
            state = "sent" if item.get("reminded") else "scheduled"
            builder.field(
                "Reminder", f"{_fmt_dt(item['remind_at'])} ({state})", True,
            )
        if item["owner_kind"] == "group":
            group = await ctx.db.get_productivity_group(item["owner_id"])
            owner = f"group #{item['owner_id']}"
            if group:
                owner = f"group {group['name']} (#{item['owner_id']})"
            builder.field("Owner", owner, True)
        else:
            owner = "you" if item["owner_id"] == ctx.author.id else "shared with you"
            builder.field("Owner", owner, True)
        builder.footer(f"{label.lower()} #{item['id']}")
        return builder.build()

    # ── generic operations shared by note / task / event ───────────────────
    async def _do_list(
        self, ctx: ArchimedesContext, kind: str, rest: str,
    ) -> None:
        group_id, list_name, remaining = _split_sigils(rest)
        owner_kind, owner_id = await self._scope_for(ctx, group_id)
        if kind == "task" and list_name is None and remaining:
            list_name = remaining.split()[0].lower()
        items = await ctx.db.list_items(
            owner_kind, owner_id, kind=kind,
            list_name=list_name if kind == "task" else None,
        )
        where = "your" if owner_kind == "user" else f"group #{owner_id}"
        title = f"{where} {kind}s"
        if kind == "task" and list_name:
            title = f"{where} tasks -- ~{list_name}"
        pages = self._list_pages(
            title, items, color=_KIND_COLOR[kind],
            empty=f"No {kind}s here yet. Add one with `{ctx.prefix}{kind} add`.",
        )
        await self._deliver(ctx, pages, private=self._is_private(owner_kind))

    async def _do_show(self, ctx: ArchimedesContext, item_id: int) -> None:
        item, _ = await self._require_item(ctx, item_id)
        embed = await self._item_embed(ctx, item)
        await self._deliver(
            ctx, [embed], private=self._is_private(item["owner_kind"]),
        )

    async def _do_edit(
        self, ctx: ArchimedesContext, item_id: int, text: str,
    ) -> None:
        item, _ = await self._require_item(ctx, item_id, need_edit=True)
        text = (text or "").strip()
        if not text:
            raise commands.BadArgument("Give the new text after the id.")
        title, _, body = text.partition("\n")
        await ctx.db.update_item(
            item_id, title=title.strip()[:300], body=body.strip(),
        )
        embed = card(
            "Updated", color=C_SUCCESS,
            description=f"Edited {item['kind']} `#{item_id}`.",
        ).build()
        await self._deliver(
            ctx, [embed], private=self._is_private(item["owner_kind"]),
        )

    async def _do_delete(self, ctx: ArchimedesContext, item_id: int) -> None:
        item, _ = await self._require_item(ctx, item_id, need_edit=True)
        await ctx.db.delete_item(item_id)
        embed = card(
            "Deleted", color=C_SUCCESS,
            description=f"Removed {item['kind']} `#{item_id}`.",
        ).build()
        await self._deliver(
            ctx, [embed], private=self._is_private(item["owner_kind"]),
        )

    async def _do_share(
        self, ctx: ArchimedesContext, item_id: int, rest: str,
    ) -> None:
        item, _ = await self._require_item(ctx, item_id)
        if item["owner_kind"] != "user" or item["owner_id"] != ctx.author.id:
            raise commands.BadArgument(
                "You can only share your own personal items. Group items are "
                "already shared with every member."
            )
        targets = [m for m in ctx.message.mentions if m.id != self.bot.user.id]
        if not targets:
            raise commands.BadArgument("Mention the user to share it with.")
        can_edit = "edit" in (rest or "").lower().split()
        target = targets[0]
        if target.id == ctx.author.id:
            raise commands.BadArgument("You already own that item.")
        await ctx.db.add_item_share(item_id, target.id, ctx.author.id, can_edit)
        access = "view and edit" if can_edit else "view"
        await self._notify_user(
            target,
            card(
                "An item was shared with you",
                color=C_INFO,
                description=(
                    f"{ctx.author.display_name} shared {item['kind']} "
                    f"`#{item_id}` ({clip(item['title'], 120)}) with you "
                    f"({access}). Open it with `{ctx.prefix}{item['kind']} "
                    f"show {item_id}`."
                ),
            ).build(),
        )
        embed = card(
            "Shared", color=C_SUCCESS,
            description=(
                f"{item['kind'].capitalize()} `#{item_id}` is now shared with "
                f"{target.display_name} ({access})."
            ),
        ).build()
        await self._deliver(ctx, [embed], private=True)

    async def _do_unshare(
        self, ctx: ArchimedesContext, item_id: int,
    ) -> None:
        item, _ = await self._require_item(ctx, item_id)
        if item["owner_kind"] != "user" or item["owner_id"] != ctx.author.id:
            raise commands.BadArgument("You can only unshare your own items.")
        targets = [m for m in ctx.message.mentions if m.id != self.bot.user.id]
        if not targets:
            raise commands.BadArgument("Mention the user to stop sharing with.")
        removed = await ctx.db.remove_item_share(item_id, targets[0].id)
        if not removed:
            raise commands.BadArgument(
                f"{targets[0].display_name} did not have access to that item."
            )
        embed = card(
            "Unshared", color=C_SUCCESS,
            description=(
                f"{targets[0].display_name} can no longer see "
                f"{item['kind']} `#{item_id}`."
            ),
        ).build()
        await self._deliver(ctx, [embed], private=True)

    async def _do_copy(
        self, ctx: ArchimedesContext, item_id: int, dest: str,
    ) -> None:
        item, _ = await self._require_item(ctx, item_id)
        owner_kind, owner_id = await self._parse_dest(ctx, dest)
        new_id = await ctx.db.create_item(
            owner_kind=owner_kind, owner_id=owner_id, kind=item["kind"],
            title=item["title"], body=item["body"],
            list_name=item.get("list_name") or "general",
            done=item["done"], due_at=item.get("due_at"),
            remind_at=item.get("remind_at"), created_by=ctx.author.id,
        )
        await self._announce_transfer(
            ctx, item, owner_kind, owner_id, new_id, verb="copied",
        )

    async def _do_move(
        self, ctx: ArchimedesContext, item_id: int, dest: str,
    ) -> None:
        item, _ = await self._require_item(ctx, item_id, need_edit=True)
        owner_kind, owner_id = await self._parse_dest(ctx, dest)
        if owner_kind == item["owner_kind"] and owner_id == item["owner_id"]:
            raise commands.BadArgument("That item is already there.")
        await ctx.db.update_item(
            item_id, owner_kind=owner_kind, owner_id=owner_id,
        )
        await self._announce_transfer(
            ctx, item, owner_kind, owner_id, item_id, verb="moved",
        )

    async def _announce_transfer(
        self, ctx: ArchimedesContext, item: dict, owner_kind: str,
        owner_id: int, item_id: int, *, verb: str,
    ) -> None:
        if owner_kind == "group":
            group = await ctx.db.get_productivity_group(owner_id)
            where = f"group {group['name']} (#{owner_id})" if group else f"group #{owner_id}"
        elif owner_id == ctx.author.id:
            where = "your personal space"
        else:
            target = self.bot.get_user(owner_id)
            where = target.display_name if target else f"user {owner_id}"
            if target is not None:
                await self._notify_user(
                    target,
                    card(
                        f"A {item['kind']} was {verb} to you",
                        color=C_INFO,
                        description=(
                            f"{ctx.author.display_name} {verb} "
                            f"{item['kind']} `#{item_id}` "
                            f"({clip(item['title'], 120)}) to you."
                        ),
                    ).build(),
                )
        embed = card(
            verb.capitalize(), color=C_SUCCESS,
            description=(
                f"{item['kind'].capitalize()} `#{item_id}` {verb} to {where}."
            ),
        ).build()
        await self._deliver(
            ctx, [embed], private=self._is_private(owner_kind),
        )

    async def _notify_user(self, user: discord.abc.User, embed) -> None:
        try:
            await user.send(embed=embed)
        except discord.HTTPException:
            pass

    # ── .note ──────────────────────────────────────────────────────────────
    @commands.group(name="note", aliases=["notes"], invoke_without_command=True)
    @no_bots
    async def note(self, ctx: ArchimedesContext, *, rest: str = "") -> None:
        """Notes. Run a subcommand or see your notes."""
        await self._do_list(ctx, "note", rest)

    @note.command(name="add", aliases=["new", "create"])
    @no_bots
    async def note_add(self, ctx: ArchimedesContext, *, rest: str = "") -> None:
        """Add a note. First line is the title, the rest is the body."""
        group_id, _list, text = _split_sigils(rest)
        if not text:
            raise commands.BadArgument(
                "Give the note text. The first line becomes the title."
            )
        owner_kind, owner_id = await self._scope_for(ctx, group_id)
        title, _, body = text.partition("\n")
        new_id = await ctx.db.create_item(
            owner_kind=owner_kind, owner_id=owner_id, kind="note",
            title=title.strip()[:300], body=body.strip(),
            created_by=ctx.author.id,
        )
        embed = card(
            "Note added", color=C_SUCCESS,
            description=f"Saved as note `#{new_id}`.",
        ).build()
        await self._deliver(ctx, [embed], private=self._is_private(owner_kind))

    @note.command(name="list", aliases=["ls", "all"])
    @no_bots
    async def note_list(self, ctx: ArchimedesContext, *, rest: str = "") -> None:
        """List your notes, or a group's notes with `#<groupid>`."""
        await self._do_list(ctx, "note", rest)

    @note.command(name="show", aliases=["view", "open"])
    @no_bots
    async def note_show(self, ctx: ArchimedesContext, item_id: int) -> None:
        """Show a single note by id."""
        await self._do_show(ctx, item_id)

    @note.command(name="edit")
    @no_bots
    async def note_edit(
        self, ctx: ArchimedesContext, item_id: int, *, text: str = "",
    ) -> None:
        """Replace a note's text."""
        await self._do_edit(ctx, item_id, text)

    @note.command(name="del", aliases=["delete", "rm", "remove"])
    @no_bots
    async def note_del(self, ctx: ArchimedesContext, item_id: int) -> None:
        """Delete a note by id."""
        await self._do_delete(ctx, item_id)

    @note.command(name="share")
    @no_bots
    async def note_share(
        self, ctx: ArchimedesContext, item_id: int, *, rest: str = "",
    ) -> None:
        """Share a personal note with a user: `share <id> @user [edit]`."""
        await self._do_share(ctx, item_id, rest)

    @note.command(name="unshare")
    @no_bots
    async def note_unshare(
        self, ctx: ArchimedesContext, item_id: int, *, rest: str = "",
    ) -> None:
        """Stop sharing a note with a user."""
        await self._do_unshare(ctx, item_id)

    @note.command(name="copy")
    @no_bots
    async def note_copy(
        self, ctx: ArchimedesContext, item_id: int, *, dest: str = "",
    ) -> None:
        """Copy a note to `me`, an @user, or `#<groupid>`."""
        await self._do_copy(ctx, item_id, dest)

    @note.command(name="move")
    @no_bots
    async def note_move(
        self, ctx: ArchimedesContext, item_id: int, *, dest: str = "",
    ) -> None:
        """Move a note to `me`, an @user, or `#<groupid>`."""
        await self._do_move(ctx, item_id, dest)

    # ── .task ──────────────────────────────────────────────────────────────
    @commands.group(
        name="task", aliases=["tasks", "todo"], invoke_without_command=True,
    )
    @no_bots
    async def task(self, ctx: ArchimedesContext, *, rest: str = "") -> None:
        """Tasks. Run a subcommand or see your tasks."""
        await self._do_list(ctx, "task", rest)

    @task.command(name="add", aliases=["new", "create"])
    @no_bots
    async def task_add(self, ctx: ArchimedesContext, *, rest: str = "") -> None:
        """Add a task. Use `~list` for a list, `#id` for a group."""
        group_id, list_name, text = _split_sigils(rest)
        if not text:
            raise commands.BadArgument("Give the task text.")
        owner_kind, owner_id = await self._scope_for(ctx, group_id)
        new_id = await ctx.db.create_item(
            owner_kind=owner_kind, owner_id=owner_id, kind="task",
            title=text[:300], list_name=list_name or "general",
            created_by=ctx.author.id,
        )
        embed = card(
            "Task added", color=C_SUCCESS,
            description=(
                f"Saved as task `#{new_id}` in list "
                f"`~{list_name or 'general'}`. Set a reminder with "
                f"`{ctx.prefix}task remind {new_id} <when>`."
            ),
        ).build()
        await self._deliver(ctx, [embed], private=self._is_private(owner_kind))

    @task.command(name="list", aliases=["ls", "all"])
    @no_bots
    async def task_list(self, ctx: ArchimedesContext, *, rest: str = "") -> None:
        """List tasks. Add a list name to filter, `#<groupid>` for a group."""
        await self._do_list(ctx, "task", rest)

    @task.command(name="lists")
    @no_bots
    async def task_lists(self, ctx: ArchimedesContext, *, rest: str = "") -> None:
        """Show every task list and how many tasks are open in each."""
        group_id, _list, _rest = _split_sigils(rest)
        owner_kind, owner_id = await self._scope_for(ctx, group_id)
        items = await ctx.db.list_items(owner_kind, owner_id, kind="task")
        buckets: dict[str, list[int]] = {}
        for it in items:
            name = it.get("list_name") or "general"
            buckets.setdefault(name, [0, 0])
            buckets[name][1] += 1
            if not it["done"]:
                buckets[name][0] += 1
        where = "your" if owner_kind == "user" else f"group #{owner_id}"
        if not buckets:
            body = "No task lists yet."
        else:
            body = "\n".join(
                f"`~{name}` -- {counts[0]} open / {counts[1]} total"
                for name, counts in sorted(buckets.items())
            )
        embed = card(
            f"{where} task lists", color=C_TEAL, description=body,
        ).build()
        await self._deliver(
            ctx, [embed], private=self._is_private(owner_kind),
        )

    @task.command(name="done", aliases=["complete", "check"])
    @no_bots
    async def task_done(self, ctx: ArchimedesContext, item_id: int) -> None:
        """Mark a task done."""
        item, _ = await self._require_item(ctx, item_id, need_edit=True)
        if item["kind"] != "task":
            raise commands.BadArgument(f"`#{item_id}` is not a task.")
        await ctx.db.update_item(item_id, done=True)
        embed = card(
            "Task done", color=C_SUCCESS,
            description=f"Marked task `#{item_id}` done.",
        ).build()
        await self._deliver(
            ctx, [embed], private=self._is_private(item["owner_kind"]),
        )

    @task.command(name="undone", aliases=["uncheck", "reopen"])
    @no_bots
    async def task_undone(self, ctx: ArchimedesContext, item_id: int) -> None:
        """Reopen a completed task."""
        item, _ = await self._require_item(ctx, item_id, need_edit=True)
        if item["kind"] != "task":
            raise commands.BadArgument(f"`#{item_id}` is not a task.")
        await ctx.db.update_item(item_id, done=False)
        embed = card(
            "Task reopened", color=C_SUCCESS,
            description=f"Task `#{item_id}` is open again.",
        ).build()
        await self._deliver(
            ctx, [embed], private=self._is_private(item["owner_kind"]),
        )

    @task.command(name="due")
    @no_bots
    async def task_due(
        self, ctx: ArchimedesContext, item_id: int, *, when: str = "",
    ) -> None:
        """Set a task's due date. Use `clear` to remove it."""
        await self._set_time(ctx, item_id, when, field="due_at", kinds=("task",))

    @task.command(name="remind")
    @no_bots
    async def task_remind(
        self, ctx: ArchimedesContext, item_id: int, *, when: str = "",
    ) -> None:
        """Set a reminder on a task. Use `clear` to remove it."""
        await self._set_time(
            ctx, item_id, when, field="remind_at", kinds=("task",),
        )

    @task.command(name="edit")
    @no_bots
    async def task_edit(
        self, ctx: ArchimedesContext, item_id: int, *, text: str = "",
    ) -> None:
        """Replace a task's text."""
        await self._do_edit(ctx, item_id, text)

    @task.command(name="del", aliases=["delete", "rm", "remove"])
    @no_bots
    async def task_del(self, ctx: ArchimedesContext, item_id: int) -> None:
        """Delete a task by id."""
        await self._do_delete(ctx, item_id)

    @task.command(name="share")
    @no_bots
    async def task_share(
        self, ctx: ArchimedesContext, item_id: int, *, rest: str = "",
    ) -> None:
        """Share a personal task with a user: `share <id> @user [edit]`."""
        await self._do_share(ctx, item_id, rest)

    @task.command(name="unshare")
    @no_bots
    async def task_unshare(
        self, ctx: ArchimedesContext, item_id: int, *, rest: str = "",
    ) -> None:
        """Stop sharing a task with a user."""
        await self._do_unshare(ctx, item_id)

    @task.command(name="copy")
    @no_bots
    async def task_copy(
        self, ctx: ArchimedesContext, item_id: int, *, dest: str = "",
    ) -> None:
        """Copy a task to `me`, an @user, or `#<groupid>`."""
        await self._do_copy(ctx, item_id, dest)

    @task.command(name="move")
    @no_bots
    async def task_move(
        self, ctx: ArchimedesContext, item_id: int, *, dest: str = "",
    ) -> None:
        """Move a task to `me`, an @user, or `#<groupid>`."""
        await self._do_move(ctx, item_id, dest)

    # ── .event ─────────────────────────────────────────────────────────────
    @commands.group(
        name="event", aliases=["events", "cal", "calendar"],
        invoke_without_command=True,
    )
    @no_bots
    async def event(self, ctx: ArchimedesContext, *, rest: str = "") -> None:
        """Calendar events. Run a subcommand or see your events."""
        await self._do_list(ctx, "event", rest)

    @event.command(name="add", aliases=["new", "create"])
    @no_bots
    async def event_add(self, ctx: ArchimedesContext, *, rest: str = "") -> None:
        """Add an event: `add <when> | <title>`. `<when>` can be relative."""
        group_id, _list, text = _split_sigils(rest)
        when_raw, sep, title = text.partition("|")
        if not sep or not title.strip():
            raise commands.BadArgument(
                "Use `event add <when> | <title>`, for example "
                "`event add in 2d | Team sync`."
            )
        when = _parse_when(when_raw)
        if when is None:
            raise commands.BadArgument(
                "Could not read that time. Try `in 2h`, `in 3d`, or "
                "`2026-06-01 14:30`."
            )
        owner_kind, owner_id = await self._scope_for(ctx, group_id)
        first_line, _, body = title.strip().partition("\n")
        new_id = await ctx.db.create_item(
            owner_kind=owner_kind, owner_id=owner_id, kind="event",
            title=first_line.strip()[:300], body=body.strip(),
            due_at=when, created_by=ctx.author.id,
        )
        embed = card(
            "Event added", color=C_SUCCESS,
            description=(
                f"Saved event `#{new_id}` for {_fmt_dt(when)}. Add a reminder "
                f"with `{ctx.prefix}event remind {new_id} <when>`."
            ),
        ).build()
        await self._deliver(ctx, [embed], private=self._is_private(owner_kind))

    @event.command(name="list", aliases=["ls", "all"])
    @no_bots
    async def event_list(self, ctx: ArchimedesContext, *, rest: str = "") -> None:
        """List your events, or a group's events with `#<groupid>`."""
        await self._do_list(ctx, "event", rest)

    @event.command(name="show", aliases=["view", "open"])
    @no_bots
    async def event_show(self, ctx: ArchimedesContext, item_id: int) -> None:
        """Show a single event by id."""
        await self._do_show(ctx, item_id)

    @event.command(name="when", aliases=["reschedule"])
    @no_bots
    async def event_when(
        self, ctx: ArchimedesContext, item_id: int, *, when: str = "",
    ) -> None:
        """Reschedule an event to a new time."""
        await self._set_time(
            ctx, item_id, when, field="due_at", kinds=("event",),
            allow_clear=False,
        )

    @event.command(name="remind")
    @no_bots
    async def event_remind(
        self, ctx: ArchimedesContext, item_id: int, *, when: str = "",
    ) -> None:
        """Set a reminder on an event. Use `clear` to remove it."""
        await self._set_time(
            ctx, item_id, when, field="remind_at", kinds=("event",),
        )

    @event.command(name="edit")
    @no_bots
    async def event_edit(
        self, ctx: ArchimedesContext, item_id: int, *, text: str = "",
    ) -> None:
        """Replace an event's text."""
        await self._do_edit(ctx, item_id, text)

    @event.command(name="del", aliases=["delete", "rm", "remove"])
    @no_bots
    async def event_del(self, ctx: ArchimedesContext, item_id: int) -> None:
        """Delete an event by id."""
        await self._do_delete(ctx, item_id)

    @event.command(name="share")
    @no_bots
    async def event_share(
        self, ctx: ArchimedesContext, item_id: int, *, rest: str = "",
    ) -> None:
        """Share a personal event with a user: `share <id> @user [edit]`."""
        await self._do_share(ctx, item_id, rest)

    @event.command(name="unshare")
    @no_bots
    async def event_unshare(
        self, ctx: ArchimedesContext, item_id: int, *, rest: str = "",
    ) -> None:
        """Stop sharing an event with a user."""
        await self._do_unshare(ctx, item_id)

    @event.command(name="copy")
    @no_bots
    async def event_copy(
        self, ctx: ArchimedesContext, item_id: int, *, dest: str = "",
    ) -> None:
        """Copy an event to `me`, an @user, or `#<groupid>`."""
        await self._do_copy(ctx, item_id, dest)

    @event.command(name="move")
    @no_bots
    async def event_move(
        self, ctx: ArchimedesContext, item_id: int, *, dest: str = "",
    ) -> None:
        """Move an event to `me`, an @user, or `#<groupid>`."""
        await self._do_move(ctx, item_id, dest)

    async def _set_time(
        self, ctx: ArchimedesContext, item_id: int, when: str, *,
        field: str, kinds: tuple[str, ...], allow_clear: bool = True,
    ) -> None:
        item, _ = await self._require_item(ctx, item_id, need_edit=True)
        if item["kind"] not in kinds:
            raise commands.BadArgument(
                f"`#{item_id}` is not a {' or '.join(kinds)}."
            )
        when = (when or "").strip()
        label = "reminder" if field == "remind_at" else "date"
        if allow_clear and when.lower() in ("clear", "none", "off"):
            updates = {field: None}
            if field == "remind_at":
                updates["reminded"] = False
            await ctx.db.update_item(item_id, **updates)
            text = f"Cleared the {label} on `#{item_id}`."
        else:
            parsed = _parse_when(when)
            if parsed is None:
                raise commands.BadArgument(
                    "Could not read that time. Try `in 2h`, `in 3d`, or "
                    "`2026-06-01 14:30`."
                )
            updates = {field: parsed}
            if field == "remind_at":
                updates["reminded"] = False
            await ctx.db.update_item(item_id, **updates)
            text = f"Set the {label} on `#{item_id}` to {_fmt_dt(parsed)}."
        embed = card("Updated", color=C_SUCCESS, description=text).build()
        await self._deliver(
            ctx, [embed], private=self._is_private(item["owner_kind"]),
        )

    # ── .group ─────────────────────────────────────────────────────────────
    @commands.group(
        name="group", aliases=["groups"], invoke_without_command=True,
    )
    @no_bots
    async def group(self, ctx: ArchimedesContext, *, _rest: str = "") -> None:
        """Productivity groups. Run a subcommand or see your groups."""
        await self._group_list(ctx)

    async def _group_list(self, ctx: ArchimedesContext) -> None:
        groups = await ctx.db.list_user_groups(ctx.author.id)
        if not groups:
            body = (
                "You are not in any groups yet. Create one with "
                f"`{ctx.prefix}group create <name>`."
            )
        else:
            lines = []
            for grp in groups:
                role = "owner" if grp["owner_id"] == ctx.author.id else "member"
                lines.append(f"`#{grp['id']}` {grp['name']} -- {role}")
            body = "\n".join(lines)
        embed = card(
            "Your groups", color=C_BLURPLE, description=body,
        ).footer(
            f"{ctx.prefix}group show <id> for details  -  "
            f"{ctx.prefix}group invites for pending invites"
        ).build()
        await self._deliver(ctx, [embed], private=True)

    @group.command(name="create", aliases=["new"])
    @guild_only
    @no_bots
    async def group_create(
        self, ctx: ArchimedesContext, *, name: str = "",
    ) -> None:
        """Create a new group in this server."""
        name = (name or "").strip()
        if not name:
            raise commands.BadArgument("Give the group a name.")
        existing = await ctx.db.list_user_groups(ctx.author.id)
        if any(g["name"].lower() == name.lower() for g in existing):
            raise commands.BadArgument(
                f"You are already in a group called `{name}`."
            )
        group_id = await ctx.db.create_productivity_group(
            ctx.guild_id, name[:100], ctx.author.id,
        )
        embed = card(
            "Group created", color=C_SUCCESS,
            description=(
                f"`{name}` is group `#{group_id}`. Invite members with "
                f"`{ctx.prefix}group invite {group_id} @user`."
            ),
        ).build()
        await self._deliver(ctx, [embed], private=True)

    @group.command(name="list", aliases=["ls", "mine"])
    @no_bots
    async def group_list(self, ctx: ArchimedesContext) -> None:
        """List the groups you belong to."""
        await self._group_list(ctx)

    @group.command(name="show", aliases=["view", "info"])
    @no_bots
    async def group_show(self, ctx: ArchimedesContext, group_id: int) -> None:
        """Show a group's members and item counts."""
        group = await ctx.db.get_productivity_group(group_id)
        if group is None:
            raise commands.BadArgument(f"There is no group #{group_id}.")
        if not await ctx.db.is_group_member(group_id, ctx.author.id):
            raise commands.BadArgument(
                "Only members can view that group."
            )
        members = await ctx.db.list_group_members(group_id)
        counts = await ctx.db.count_group_items(group_id)
        member_lines = []
        for uid in members:
            user = self.bot.get_user(uid)
            name = user.display_name if user else f"user {uid}"
            tag = " (owner)" if uid == group["owner_id"] else ""
            member_lines.append(f"- {name}{tag}")
        embed = (
            card(
                f"Group {group['name']} (#{group_id})", color=C_BLURPLE,
            )
            .field(
                "Members", clip("\n".join(member_lines) or "(none)", 1024),
                False,
            )
            .field("Notes", str(counts.get("note", 0)), True)
            .field("Tasks", str(counts.get("task", 0)), True)
            .field("Events", str(counts.get("event", 0)), True)
            .footer(
                f"{ctx.prefix}note list #{group_id}  -  "
                f"{ctx.prefix}task list #{group_id}  -  "
                f"{ctx.prefix}event list #{group_id}"
            )
        ).build()
        await self._deliver(ctx, [embed], private=False)

    @group.command(name="invite")
    @no_bots
    async def group_invite(
        self, ctx: ArchimedesContext, group_id: int, *, _rest: str = "",
    ) -> None:
        """Invite a user to a group: `invite <id> @user`."""
        group = await self._owned_group(ctx, group_id)
        targets = [m for m in ctx.message.mentions if m.id != self.bot.user.id]
        if not targets:
            raise commands.BadArgument("Mention the user to invite.")
        target = targets[0]
        if target.bot:
            raise commands.BadArgument("You cannot invite a bot.")
        if await ctx.db.is_group_member(group_id, target.id):
            raise commands.BadArgument(
                f"{target.display_name} is already in that group."
            )
        await ctx.db.create_group_invite(group_id, target.id, ctx.author.id)
        await self._notify_user(
            target,
            card(
                "Group invitation", color=C_INFO,
                description=(
                    f"{ctx.author.display_name} invited you to the group "
                    f"`{group['name']}` (#{group_id}). Accept with "
                    f"`{ctx.prefix}group join {group_id}` or decline with "
                    f"`{ctx.prefix}group decline {group_id}`."
                ),
            ).build(),
        )
        embed = card(
            "Invite sent", color=C_SUCCESS,
            description=(
                f"Invited {target.display_name} to `{group['name']}`."
            ),
        ).build()
        await self._deliver(ctx, [embed], private=True)

    @group.command(name="invites", aliases=["pending"])
    @no_bots
    async def group_invites(self, ctx: ArchimedesContext) -> None:
        """List group invitations waiting for you."""
        invites = await ctx.db.list_user_invites(ctx.author.id)
        if not invites:
            body = "You have no pending group invitations."
        else:
            lines = []
            for inv in invites:
                inviter = self.bot.get_user(inv["inviter_id"])
                who = inviter.display_name if inviter else f"user {inv['inviter_id']}"
                lines.append(
                    f"`#{inv['group_id']}` {inv['group_name']} -- from {who}"
                )
            body = "\n".join(lines)
        embed = card(
            "Your group invites", color=C_BLURPLE, description=body,
        ).footer(f"{ctx.prefix}group join <id> to accept").build()
        await self._deliver(ctx, [embed], private=True)

    @group.command(name="join", aliases=["accept"])
    @no_bots
    async def group_join(self, ctx: ArchimedesContext, group_id: int) -> None:
        """Accept a pending group invitation."""
        invite = await ctx.db.get_group_invite(group_id, ctx.author.id)
        if invite is None:
            raise commands.BadArgument(
                f"You have no invitation to group #{group_id}."
            )
        await ctx.db.add_group_member(group_id, ctx.author.id)
        await ctx.db.delete_group_invite(group_id, ctx.author.id)
        group = await ctx.db.get_productivity_group(group_id)
        name = group["name"] if group else f"#{group_id}"
        embed = card(
            "Joined", color=C_SUCCESS,
            description=f"You are now a member of `{name}`.",
        ).build()
        await self._deliver(ctx, [embed], private=True)

    @group.command(name="decline", aliases=["reject"])
    @no_bots
    async def group_decline(
        self, ctx: ArchimedesContext, group_id: int,
    ) -> None:
        """Decline a pending group invitation."""
        removed = await ctx.db.delete_group_invite(group_id, ctx.author.id)
        if not removed:
            raise commands.BadArgument(
                f"You have no invitation to group #{group_id}."
            )
        embed = card(
            "Declined", color=C_SUCCESS,
            description=f"Declined the invite to group #{group_id}.",
        ).build()
        await self._deliver(ctx, [embed], private=True)

    @group.command(name="leave")
    @no_bots
    async def group_leave(self, ctx: ArchimedesContext, group_id: int) -> None:
        """Leave a group you belong to."""
        group = await ctx.db.get_productivity_group(group_id)
        if group is None:
            raise commands.BadArgument(f"There is no group #{group_id}.")
        if not await ctx.db.is_group_member(group_id, ctx.author.id):
            raise commands.BadArgument("You are not in that group.")
        if group["owner_id"] == ctx.author.id:
            raise commands.BadArgument(
                "You own that group. Transfer it with "
                f"`{ctx.prefix}group transfer {group_id} @user` or delete it "
                f"with `{ctx.prefix}group delete {group_id}`."
            )
        await ctx.db.remove_group_member(group_id, ctx.author.id)
        embed = card(
            "Left group", color=C_SUCCESS,
            description=f"You left `{group['name']}`.",
        ).build()
        await self._deliver(ctx, [embed], private=True)

    @group.command(name="kick", aliases=["remove"])
    @no_bots
    async def group_kick(
        self, ctx: ArchimedesContext, group_id: int, *, _rest: str = "",
    ) -> None:
        """Remove a member from a group you own: `kick <id> @user`."""
        group = await self._owned_group(ctx, group_id)
        targets = [m for m in ctx.message.mentions if m.id != self.bot.user.id]
        if not targets:
            raise commands.BadArgument("Mention the member to remove.")
        target = targets[0]
        if target.id == ctx.author.id:
            raise commands.BadArgument(
                "You own the group. Use transfer or delete instead."
            )
        removed = await ctx.db.remove_group_member(group_id, target.id)
        if not removed:
            raise commands.BadArgument(
                f"{target.display_name} is not in that group."
            )
        embed = card(
            "Member removed", color=C_SUCCESS,
            description=f"Removed {target.display_name} from `{group['name']}`.",
        ).build()
        await self._deliver(ctx, [embed], private=True)

    @group.command(name="rename")
    @no_bots
    async def group_rename(
        self, ctx: ArchimedesContext, group_id: int, *, name: str = "",
    ) -> None:
        """Rename a group you own."""
        group = await self._owned_group(ctx, group_id)
        name = (name or "").strip()
        if not name:
            raise commands.BadArgument("Give the new group name.")
        await ctx.db.rename_productivity_group(group_id, name[:100])
        embed = card(
            "Group renamed", color=C_SUCCESS,
            description=f"`{group['name']}` is now `{name}`.",
        ).build()
        await self._deliver(ctx, [embed], private=True)

    @group.command(name="transfer")
    @no_bots
    async def group_transfer(
        self, ctx: ArchimedesContext, group_id: int, *, _rest: str = "",
    ) -> None:
        """Hand group ownership to another member: `transfer <id> @user`."""
        group = await self._owned_group(ctx, group_id)
        targets = [m for m in ctx.message.mentions if m.id != self.bot.user.id]
        if not targets:
            raise commands.BadArgument("Mention the member to hand the group to.")
        target = targets[0]
        if not await ctx.db.is_group_member(group_id, target.id):
            raise commands.BadArgument(
                f"{target.display_name} must be a group member first."
            )
        await ctx.db.set_productivity_group_owner(group_id, target.id)
        embed = card(
            "Ownership transferred", color=C_SUCCESS,
            description=(
                f"{target.display_name} now owns `{group['name']}`."
            ),
        ).build()
        await self._deliver(ctx, [embed], private=True)

    @group.command(name="delete", aliases=["disband"])
    @no_bots
    async def group_delete(
        self, ctx: ArchimedesContext, group_id: int,
    ) -> None:
        """Delete a group you own and everything inside it."""
        group = await self._owned_group(ctx, group_id)
        ok = await ctx.confirm(
            f"Delete group `{group['name']}` and all of its notes, tasks and "
            "events? This cannot be undone."
        )
        if not ok:
            await ctx.reply_error("Group deletion cancelled.")
            return
        await ctx.db.delete_productivity_group(group_id)
        embed = card(
            "Group deleted", color=C_SUCCESS,
            description=f"`{group['name']}` and its items are gone.",
        ).build()
        await self._deliver(ctx, [embed], private=True)

    @group.command(name="duplicate", aliases=["clone", "copy"])
    @no_bots
    async def group_duplicate(
        self, ctx: ArchimedesContext, group_id: int,
    ) -> None:
        """Copy a group's items into a fresh group you own."""
        group = await ctx.db.get_productivity_group(group_id)
        if group is None:
            raise commands.BadArgument(f"There is no group #{group_id}.")
        if not await ctx.db.is_group_member(group_id, ctx.author.id):
            raise commands.BadArgument("Only members can duplicate that group.")
        new_id = await ctx.db.create_productivity_group(
            group["guild_id"], clip(f"{group['name']} (copy)", 100),
            ctx.author.id,
        )
        items = await ctx.db.list_items("group", group_id)
        for it in items:
            await ctx.db.create_item(
                owner_kind="group", owner_id=new_id, kind=it["kind"],
                title=it["title"], body=it["body"],
                list_name=it.get("list_name") or "general",
                done=it["done"], due_at=it.get("due_at"),
                remind_at=it.get("remind_at"), created_by=ctx.author.id,
            )
        embed = card(
            "Group duplicated", color=C_SUCCESS,
            description=(
                f"Copied {len(items)} item(s) into new group "
                f"`#{new_id}` ({group['name']} (copy)). You are the owner."
            ),
        ).build()
        await self._deliver(ctx, [embed], private=True)

    async def _owned_group(
        self, ctx: ArchimedesContext, group_id: int,
    ) -> dict:
        """Return a group the invoker owns, or raise a friendly error."""
        group = await ctx.db.get_productivity_group(group_id)
        if group is None:
            raise commands.BadArgument(f"There is no group #{group_id}.")
        if group["owner_id"] != ctx.author.id:
            raise commands.BadArgument(
                "Only the group owner can do that."
            )
        return group

    # ── reminder loop ──────────────────────────────────────────────────────
    @tasks.loop(seconds=_REMINDER_INTERVAL_S)
    async def _reminder_loop(self) -> None:
        """DM owners and group members when a reminder falls due."""
        try:
            due = await self.bot.db.due_reminders()
        except Exception as exc:  # noqa: BLE001
            log.debug("reminder scan failed: %s", exc)
            return
        for item in due:
            try:
                await self._fire_reminder(item)
            except Exception as exc:  # noqa: BLE001
                log.debug("reminder %s failed: %s", item.get("id"), exc)
            try:
                await self.bot.db.mark_item_reminded(item["id"])
            except Exception as exc:  # noqa: BLE001
                log.debug("mark reminded %s failed: %s", item.get("id"), exc)

    @_reminder_loop.before_loop
    async def _before_reminders(self) -> None:
        await self.bot.wait_until_ready()

    async def _fire_reminder(self, item: dict) -> None:
        if item["owner_kind"] == "group":
            recipients = await self.bot.db.list_group_members(item["owner_id"])
            group = await self.bot.db.get_productivity_group(item["owner_id"])
            where = f"group {group['name']}" if group else "a group"
        else:
            recipients = [item["owner_id"]]
            where = "your items"
        builder = card(
            "Reminder", color=C_GOLD,
            description=f"**{clip(item['title'], 240)}**",
        )
        if item.get("body"):
            builder.field("Details", clip(item["body"], 1024), False)
        builder.field("Type", _KIND_LABEL.get(item["kind"], "Item"), True)
        if item.get("due_at"):
            builder.field("Scheduled", _fmt_dt(item["due_at"]), True)
        builder.footer(f"{where}  -  item #{item['id']}")
        embed = builder.build()
        for uid in recipients:
            user = self.bot.get_user(uid)
            if user is None:
                try:
                    user = await self.bot.fetch_user(uid)
                except discord.HTTPException:
                    continue
            await self._notify_user(user, embed)


async def setup(bot) -> None:
    await bot.add_cog(Productivity(bot))
