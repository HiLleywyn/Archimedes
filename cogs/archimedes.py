"""cogs/archimedes.py -- the player-facing .arch command group.

Lets any member tune how Archimedes talks to them and inspect what it has
learned. Prefix-only (no slash command). There is no premium gate and no
unlock requirement -- every command is open to everyone.

    .arch                       -- help page
    .arch chat / threads        -- inline replies vs thread replies
    .arch ctx [@user|#channel|server|clear]
    .arch save / unsave / saved -- bookmark Archimedes answers
    .arch optin / optout        -- AI context tracking
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

from framework.context import ArchimedesContext
from framework.embed import card
from framework.middleware import guild_only, no_bots
from framework.ui import C_INFO, C_PURPLE, clip, fmt_ts
from ai import traits as trait_engine
from ai.memory import guild_scope, user_scope

log = logging.getLogger(__name__)


class Archimedes(commands.Cog):
    """The .arch command group: player-facing Archimedes controls."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.group(name="arch", aliases=["a", "archimedes"], invoke_without_command=True)
    @guild_only
    @no_bots
    async def archimedes(self, ctx: ArchimedesContext, *, _rest: str = "") -> None:
        """Archimedes controls. Run .arch for the full help page."""
        await self._send_help(ctx)

    async def _send_help(self, ctx: ArchimedesContext) -> None:
        p = ctx.prefix
        mode = await ctx.db.get_archimedes_reply_mode(ctx.author.id, ctx.guild_id)
        b = (
            card(
                "Archimedes",
                color=C_INFO,
                description=(
                    "Archimedes answers `@`mentions and replies with a small, "
                    "memory-backed AI. These commands tune how it talks to you."
                ),
            )
            .field(
                "Talk style",
                f"`{p}arch chat` -- Archimedes replies inline in-channel\n"
                f"`{p}arch threads` -- Archimedes replies inside its own thread",
                False,
            )
            .field(
                "Context",
                f"`{p}arch ctx` -- what Archimedes knows about you\n"
                f"`{p}arch ctx @user` -- look up another member\n"
                f"`{p}arch ctx server` -- server-wide context\n"
                f"`{p}arch ctx clear` -- wipe what Archimedes learned about you",
                False,
            )
            .field(
                "Saved answers",
                f"`{p}arch save` -- reply to an Archimedes message to bookmark it\n"
                f"`{p}arch saved [num]` -- browse your bookmarks\n"
                f"`{p}arch unsave <num>` -- drop a bookmark",
                False,
            )
            .field(
                "Privacy",
                f"`{p}arch optout` -- stop Archimedes learning about you\n"
                f"`{p}arch optin` -- opt back in (everyone starts opted in)",
                False,
            )
            .field("Your reply mode", "inline chat" if mode == "chat" else "threads", True)
            .footer(f"Prefix-only. Use {p}arch, {p}a, or {p}archimedes.")
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── reply mode ────────────────────────────────────────────────────────────
    @archimedes.command(name="chat")
    @guild_only
    @no_bots
    async def archimedes_chat(self, ctx: ArchimedesContext) -> None:
        """Switch Archimedes to inline in-channel replies instead of threads."""
        await ctx.db.set_archimedes_reply_mode(ctx.author.id, ctx.guild_id, "chat")
        await ctx.reply_success(
            "Archimedes will now answer you with a normal in-channel reply. "
            "Switch back any time with `.arch threads`.",
            title="Reply mode: inline chat",
        )

    @archimedes.command(name="threads", aliases=["thread"])
    @guild_only
    @no_bots
    async def archimedes_threads(self, ctx: ArchimedesContext) -> None:
        """Switch Archimedes back to replying inside its own thread."""
        await ctx.db.set_archimedes_reply_mode(ctx.author.id, ctx.guild_id, "thread")
        await ctx.reply_success(
            "Archimedes will now answer you inside its own thread to keep channels "
            "tidy. Switch to inline replies any time with `.arch chat`.",
            title="Reply mode: threads",
        )

    # ── context inspector ─────────────────────────────────────────────────────
    @archimedes.command(name="ctx", aliases=["context"])
    @guild_only
    @no_bots
    async def archimedes_ctx(self, ctx: ArchimedesContext, *, target: str = "") -> None:
        """Inspect AI context: yours, a member's, or the server's."""
        low = target.strip().lower()

        if low == "clear":
            deleted = await ctx.db.wipe_ai_user_state(ctx.author.id, ctx.guild_id)
            n = sum(deleted.values())
            await ctx.reply_success(
                f"Wiped what Archimedes had learned about you here ({n} row(s)) -- "
                "memory, traits, conversation history and your personal facts.",
                title="Your Archimedes context cleared",
            )
            return

        if low in ("server", "guild"):
            await self._show_server_ctx(ctx)
            return

        mentioned = [m for m in ctx.message.mentions if m.id != self.bot.user.id]
        member = mentioned[0] if mentioned else ctx.author
        await self._show_user_ctx(ctx, member)

    async def _show_user_ctx(self, ctx: ArchimedesContext, member: discord.abc.User) -> None:
        db = ctx.db
        memory = await db.get_ai_user_memory(member.id, ctx.guild_id)
        traits = await trait_engine.get_traits(db, member.id, ctx.guild_id)
        opted_out = await db.is_ai_opted_out(member.id, ctx.guild_id)
        history_n = await db.fetch_val(
            "SELECT COUNT(*) FROM ai_conversations WHERE user_id=$1 AND guild_id=$2",
            member.id, ctx.guild_id,
        )
        facts = await db.fetch_all(
            "SELECT key, value FROM archimedes_facts WHERE scope=$1 ORDER BY updated_at DESC LIMIT 10",
            user_scope(member.id, ctx.guild_id),
        )

        b = card(f"Archimedes context -- {member.display_name}", color=C_PURPLE)
        b = b.thumbnail(member.display_avatar.url)
        b = b.field("Memory", clip(memory or "(nothing learned yet)", 1024), False)
        if traits:
            trait_lines = "\n".join(
                f"- {t['label']} ({t['confidence'] * 100:.0f}% sure)"
                for t in traits[:6]
            )
        else:
            trait_lines = "(no traits inferred yet)"
        b = b.field("Inferred style", trait_lines, False)
        if facts:
            fact_lines = "\n".join(
                f"`{f['key']}`: {clip(f['value'], 90)}" for f in facts
            )
            b = b.field("Remembered facts", clip(fact_lines, 1024), False)
        b = b.field("Stored messages", str(int(history_n or 0)), True)
        b = b.field("AI tracking", "opted OUT" if opted_out else "active", True)
        b = b.footer("Also: .arch ctx server / clear")
        await ctx.reply(embed=b.build(), mention_author=False)

    async def _show_server_ctx(self, ctx: ArchimedesContext) -> None:
        db = ctx.db
        gid = ctx.guild_id
        facts = await db.fetch_all(
            "SELECT key, value FROM archimedes_facts WHERE scope=$1 "
            "ORDER BY updated_at DESC LIMIT 12",
            guild_scope(gid),
        )
        episodes = await db.fetch_all(
            "SELECT summary, EXTRACT(EPOCH FROM created_at) AS created_at "
            "FROM archimedes_episodes WHERE scope=$1 ORDER BY created_at DESC LIMIT 8",
            guild_scope(gid),
        )
        optouts = await db.fetch_val(
            "SELECT COUNT(*) FROM ai_opt_outs WHERE guild_id=$1", gid,
        )
        tracked = await db.fetch_val(
            "SELECT COUNT(DISTINCT user_id) FROM ai_user_memory WHERE guild_id=$1", gid,
        )

        b = card(f"Archimedes server context -- {ctx.guild.name}", color=C_INFO)
        if facts:
            fact_lines = "\n".join(
                f"`{f['key']}`: {clip(f['value'], 90)}" for f in facts
            )
        else:
            fact_lines = "(no server facts learned yet)"
        b = b.field("Server facts", clip(fact_lines, 1024), False)
        if episodes:
            ep_lines = "\n".join(
                f"- {clip(e['summary'], 110)} ({fmt_ts(e['created_at'])})"
                for e in episodes
            )
        else:
            ep_lines = "(no episodes recorded yet)"
        b = b.field("Recent episodes", clip(ep_lines, 1024), False)
        b = b.field("Members tracked", str(int(tracked or 0)), True)
        b = b.field("Members opted out", str(int(optouts or 0)), True)
        await ctx.reply(embed=b.build(), mention_author=False)

    # ── saved answers ─────────────────────────────────────────────────────────
    def _is_archimedes_message(self, msg: discord.Message | None) -> bool:
        if msg is None or self.bot.user is None:
            return False
        return msg.author.id == self.bot.user.id and bool((msg.content or "").strip())

    async def _resolve_referenced(self, ctx: ArchimedesContext) -> discord.Message | None:
        ref = ctx.message.reference
        if ref is None:
            return None
        if isinstance(ref.resolved, discord.Message):
            return ref.resolved
        if ref.message_id:
            try:
                return await ctx.channel.fetch_message(ref.message_id)
            except discord.HTTPException:
                return None
        return None

    @archimedes.command(name="save")
    @guild_only
    @no_bots
    async def archimedes_save(self, ctx: ArchimedesContext) -> None:
        """Bookmark an Archimedes answer. Run this as a reply to one of its messages."""
        archimedes_msg = await self._resolve_referenced(ctx)
        if not self._is_archimedes_message(archimedes_msg):
            await ctx.reply_error(
                "Reply to one of Archimedes's messages with `.arch save` to bookmark it."
            )
            return
        trigger = None
        try:
            async for m in archimedes_msg.channel.history(limit=12, before=archimedes_msg):
                if not m.author.bot and (m.content or "").strip():
                    trigger = m
                    break
        except discord.HTTPException:
            pass
        prompt_text = (trigger.content.strip() if trigger else "") or "(original not found)"
        saved = await ctx.db.add_archimedes_saved_message(
            ctx.author.id, ctx.guild_id, archimedes_msg.channel.id, archimedes_msg.id,
            trigger.id if trigger else None,
            clip(prompt_text, 1500), clip(archimedes_msg.content or "", 3000),
            archimedes_msg.jump_url,
        )
        if not saved:
            await ctx.reply_error("You've already saved that answer. See `.arch saved`.")
            return
        await ctx.reply_success(
            "Bookmarked that exchange. View it with `.arch saved`.",
            title="Archimedes answer saved",
        )

    @archimedes.command(name="unsave")
    @guild_only
    @no_bots
    async def archimedes_unsave(self, ctx: ArchimedesContext, index: int | None = None) -> None:
        """Drop a bookmarked Archimedes answer by its number."""
        rows = await ctx.db.list_archimedes_saved_messages(ctx.author.id, ctx.guild_id)
        if not rows:
            await ctx.reply_error("You have no saved Archimedes answers.")
            return
        if index is None or index < 0 or index >= len(rows):
            await ctx.reply_error(
                f"Give a number 0-{len(rows) - 1}: `.arch unsave <num>`."
            )
            return
        ok = await ctx.db.delete_archimedes_saved_message(
            ctx.author.id, ctx.guild_id, int(rows[index]["id"]),
        )
        if not ok:
            await ctx.reply_error("Couldn't drop that bookmark -- try again.")
            return
        await ctx.reply_success("Removed that answer.", title="Bookmark dropped")

    @archimedes.command(name="saved")
    @guild_only
    @no_bots
    async def archimedes_saved(self, ctx: ArchimedesContext, index: int | None = None) -> None:
        """Browse your bookmarked Archimedes answers, or open one by number."""
        rows = await ctx.db.list_archimedes_saved_messages(ctx.author.id, ctx.guild_id)
        if not rows:
            await ctx.reply_error(
                "You haven't saved any answers. Reply to an Archimedes message "
                "with `.arch save`."
            )
            return
        if index is not None:
            if index < 0 or index >= len(rows):
                await ctx.reply_error(f"No saved answer {index}. You have {len(rows)}.")
                return
            await ctx.reply(
                embed=self._saved_embed(rows[index], index, len(rows)),
                mention_author=False,
            )
            return
        pages = [self._saved_embed(r, i, len(rows)) for i, r in enumerate(rows)]
        await ctx.paginate(pages)

    def _saved_embed(self, row: dict, index: int, total: int) -> discord.Embed:
        b = card(f"Saved Archimedes answer #{index}", color=C_PURPLE)
        b = b.field("Your question", clip(row.get("prompt_text") or "(unavailable)", 1024), False)
        b = b.field("Archimedes's answer", clip(row.get("response_text") or "(empty)", 1024), False)
        b = b.field("Saved", fmt_ts(row.get("saved_at")), True)
        if row.get("jump_url"):
            b = b.field("Jump", f"[Open in chat]({row['jump_url']})", True)
        b = b.footer(f"{index + 1} of {total}  -  .arch unsave {index} to remove")
        return b.build()

    # ── privacy ───────────────────────────────────────────────────────────────
    @archimedes.command(name="optout")
    @guild_only
    @no_bots
    async def archimedes_optout(self, ctx: ArchimedesContext) -> None:
        """Opt out of AI context tracking. Archimedes forgets what it knows about you."""
        if await ctx.db.is_ai_opted_out(ctx.author.id, ctx.guild_id):
            await ctx.reply_error_hint(
                "You're already opted out.",
                hint=f"Use {ctx.prefix}arch optin to re-enable memory.",
            )
            return
        await ctx.db.set_ai_opt_out(ctx.author.id, ctx.guild_id)
        await ctx.reply_success(
            "Wiped your AI memory, history and learned traits. Archimedes no longer "
            f"remembers anything about you here. Reverse with `{ctx.prefix}arch optin`.",
            title="Opted out of AI context",
        )

    @archimedes.command(name="optin")
    @guild_only
    @no_bots
    async def archimedes_optin(self, ctx: ArchimedesContext) -> None:
        """Opt back in to AI context tracking."""
        if not await ctx.db.is_ai_opted_out(ctx.author.id, ctx.guild_id):
            await ctx.reply_error("You weren't opted out.")
            return
        await ctx.db.clear_ai_opt_out(ctx.author.id, ctx.guild_id)
        await ctx.reply_success(
            "Welcome back. Archimedes will start learning about you again from here on.",
            title="Opted in to AI context",
        )


async def setup(bot) -> None:
    await bot.add_cog(Archimedes(bot))
