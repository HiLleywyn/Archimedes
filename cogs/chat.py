"""cogs/chat.py -- the chat brain.

Owns every conversational path: an @mention, a reply to one of Archimedes's
answers, the .ask command, and (optionally) ambient chime-ins. All four
share one pipeline: gather context -> build the system prompt -> stream a
tool-calling turn into a placeholder -> persist + learn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time

import discord
from discord.ext import commands

from config import Config
from framework.context import ArchimedesContext
from framework.embed import card
from framework.middleware import guild_only, no_bots
from framework.ui import C_ERROR, C_SUCCESS, C_WARNING, clip
from ai.client import complete_default
from ai.context import ChatMode, build_system_prompt, gather_chat_context
from ai.memory import run_post_message_tasks
from ai.quota import (
    cancel_ai_quota_reservation, quota_limit, quota_window_hours, reserve_ai_quota,
)
from ai.models import resolve_model
from ai.safety import (
    is_injection_attempt, looks_like_acrostic, sanitize_input, sanitize_output,
)
from ai.tools import ToolContext, run_agent_stream
from ai.usage import TurnMeter
from cogs.chat_views import AskReplyView, ApprovalView, AskState, StreamRenderer

log = logging.getLogger(__name__)

_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_MAX_TOKENS = 1000
_DISCORD_LIMIT = 1990


def _image_urls(message: discord.Message) -> list[str]:
    """Collect image URLs from one message's attachments and embeds."""
    urls: list[str] = []
    for att in message.attachments:
        ct = (att.content_type or "").lower()
        if ct.startswith("image") or att.filename.lower().endswith(_IMAGE_EXT):
            urls.append(att.url)
    for emb in message.embeds:
        for media in (emb.image, emb.thumbnail):
            url = media.url if media else None
            # An attachment:// URL is not fetchable on its own -- the
            # attachment it points at is already collected above.
            if url and not url.startswith("attachment://"):
                urls.append(url)
                break
    return urls


async def _gather_images(message: discord.Message) -> list[str]:
    """Image URLs from the message, plus any message it is a reply to.

    Replying to an image -- including one Archimedes itself generated and
    posted -- pulls that image into the turn, so "what is this" and "edit
    this" work on the replied-to image, not only on a fresh attachment.
    """
    urls = list(_image_urls(message))
    ref = message.reference
    if ref is not None and getattr(ref, "message_id", None):
        referenced = ref.resolved
        if not isinstance(referenced, discord.Message):
            try:
                referenced = await message.channel.fetch_message(ref.message_id)
            except (discord.HTTPException, AttributeError):
                referenced = None
        if isinstance(referenced, discord.Message):
            for url in _image_urls(referenced):
                if url not in urls:
                    urls.append(url)
    return urls[:4]


def _history_key(channel) -> str:
    """Conversation history bucket: one per thread, shared for inline chat."""
    return f"thread:{channel.id}" if isinstance(channel, discord.Thread) else "default"


def _stamp_footer(text: str, meter: TurnMeter | None) -> str:
    """Append the per-turn usage footer (model, time, tokens, cost).

    Rendered as Discord subtext so it reads as a quiet footer under the
    reply. A multi-step turn lists each model the turn touched.
    """
    body = text[:_DISCORD_LIMIT]
    if meter is None:
        return body
    footer = meter.footer_text("-# ")
    if not footer:
        return body
    budget = _DISCORD_LIMIT - len(footer) - 1
    return text[:max(0, budget)] + "\n" + footer


def _format_tool_args(args: dict) -> str:
    """Render tool-call arguments as compact, clipped lines for an approval
    prompt, so the person deciding can see what the call would do.

    Long values are truncated; a non-dict or empty argument set reads as
    "(no arguments)".
    """
    if not isinstance(args, dict) or not args:
        return "_(no arguments)_"
    lines = []
    for key, value in list(args.items())[:8]:
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, default=str)
            except (TypeError, ValueError):
                text = str(value)
        lines.append(f"- **{key}**: {clip(text, 280)}")
    return "\n".join(lines)


class ChatBrain(commands.Cog):
    """Handles mentions, replies, .ask, and ambient chat."""

    def __init__(self, bot) -> None:
        self.bot = bot
        self._cooldowns: dict[int, float] = {}
        self._bg_tasks: set[asyncio.Task] = set()

    def cog_unload(self) -> None:
        for task in list(self._bg_tasks):
            task.cancel()

    # ── cooldown ──────────────────────────────────────────────────────────────
    def _cooldown_remaining(self, user_id: int) -> float:
        last = self._cooldowns.get(user_id, 0.0)
        elapsed = time.monotonic() - last
        return max(0.0, Config.AI_COOLDOWN_S - elapsed)

    def _set_cooldown(self, user_id: int) -> None:
        self._cooldowns[user_id] = time.monotonic()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ── message routing ───────────────────────────────────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if message.content.startswith(Config.PREFIX):
            return  # a command -- handled by the command processor

        # A direct message is a one-to-one conversation with Archimedes:
        # every message is addressed to it, no mention or reply needed.
        if message.guild is None:
            await self._handle(message, ChatMode.MENTION)
            return

        bot_user = self.bot.user
        ref = message.reference
        is_reply_to_ai = bool(
            ref and ref.message_id and self.bot.is_ai_message(ref.message_id)
        )
        is_mention = bool(
            bot_user and bot_user.mentioned_in(message) and not message.mention_everyone
        )
        if is_mention:
            await self._handle(message, ChatMode.MENTION)
        elif is_reply_to_ai:
            await self._handle(message, ChatMode.REPLY)
        elif self.bot.is_ai_thread(message.channel):
            # Any message inside a thread Archimedes spawned continues the chat.
            await self._handle(message, ChatMode.REPLY)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """Thumbs up / down on an AI reply scores the training corpus."""
        emoji = str(payload.emoji)
        if emoji not in ("👍", "👎"):
            return
        if not self.bot.is_ai_message(payload.message_id):
            return
        training = getattr(self.bot, "training", None)
        if training is not None:
            await training.set_feedback(payload.channel_id, 1 if emoji == "👍" else -1)

    # ── .ask command ──────────────────────────────────────────────────────────
    @commands.command(name="ask")
    @guild_only
    @no_bots
    async def ask_cmd(self, ctx: ArchimedesContext, *, question: str = "") -> None:
        """Ask Archimedes a question. Reads your conversation history for context."""
        if not question.strip():
            await ctx.reply_error(f"Usage: `{ctx.prefix}ask <your question>`")
            return
        await self._handle(ctx.message, ChatMode.ASK, override_text=question)

    # ── shared pipeline ───────────────────────────────────────────────────────
    async def _handle(
        self, message: discord.Message, mode: ChatMode,
        *, override_text: str | None = None,
    ) -> None:
        guild = message.guild
        guild_id = guild.id if guild else 0
        author = message.author
        flags = await self.bot.db.get_ai_flags(guild_id)
        if not flags["chat"]:
            return

        # Resolve the user's question text.
        if override_text is not None:
            raw = override_text
        else:
            raw = message.content
            if self.bot.user is not None:
                for tag in (f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"):
                    raw = raw.replace(tag, "")
        question = sanitize_input(raw, keep_urls=True).strip()
        images = await _gather_images(message)
        if not question and images:
            question = "What's in this image?"
        if not question:
            question = "hey"

        if is_injection_attempt(question):
            await self._safe_reply(message, "nice try. not playing that game.")
            return

        if self._cooldown_remaining(author.id) > 0:
            if mode is ChatMode.ASK:
                await self._safe_reply(
                    message, f"Slow down, give it {self._cooldown_remaining(author.id):.0f}s.",
                )
            return

        allowed, _remaining, quota_ts = await reserve_ai_quota(author.id, guild_id)
        if not allowed:
            if mode is ChatMode.ASK:
                hrs = quota_window_hours()
                await self._safe_reply(
                    message,
                    f"You've used your {quota_limit()} AI messages for the last "
                    f"{'hour' if hrs == 1 else f'{hrs}h'}. Try again later.",
                )
            return
        self._set_cooldown(author.id)

        try:
            await self._respond(message, mode, question, images, flags, quota_ts)
        except Exception:  # noqa: BLE001
            log.exception("chat pipeline failed")
            cancel_ai_quota_reservation(author.id, guild_id, quota_ts)

    async def _respond(
        self, message: discord.Message, mode: ChatMode, question: str,
        images: list[str], flags: dict, quota_ts: float,
    ) -> None:
        guild = message.guild
        guild_id = guild.id if guild else 0
        author = message.author
        db = self.bot.db
        opted_out = await db.is_ai_opted_out(author.id, guild_id)

        # Decide where the reply lands: a fresh thread, the current thread,
        # or inline. Threading honours the member's .arch chat/threads pick.
        threaded = await self._threaded(message, flags)
        thread = None
        if threaded and not isinstance(message.channel, discord.Thread):
            try:
                thread = await message.create_thread(
                    name=(question[:90] or "Archimedes chat"),
                )
                self.bot.remember_ai_thread(thread.id)
            except (discord.HTTPException, AttributeError):
                thread = None
        target_channel = thread or message.channel
        history_key = _history_key(target_channel)

        # Build context + system prompt.
        ctx_obj = await gather_chat_context(
            self.bot,
            mode=mode,
            user_id=author.id,
            guild_id=guild_id,
            channel=message.channel,
            member=author if isinstance(author, discord.Member) else None,
            display_name=author.display_name,
            user_message=question,
            history_key=history_key,
        )
        system_prompt = build_system_prompt(ctx_obj)

        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(ctx_obj.history)
        if images:
            # The markers give the model the image URLs as text, so it can
            # hand one to a tool (describe, edit, animate); the image_url
            # blocks let a vision-capable model see them directly.
            markers = "  ".join(f"[ATTACHMENT: {url}]" for url in images)
            blocks: list[dict] = [
                {"type": "text", "text": f"{question}\n{markers}"},
            ]
            for url in images:
                blocks.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": "user", "content": blocks})
        else:
            messages.append({"role": "user", "content": question})

        # Placeholder.
        try:
            if thread is not None:
                placeholder = await thread.send("_thinking..._")
            else:
                placeholder = await message.reply(
                    "_thinking..._", mention_author=False,
                )
        except discord.HTTPException as exc:
            log.warning("placeholder send failed: %s", exc)
            cancel_ai_quota_reservation(author.id, guild_id, quota_ts)
            return

        timeout = float(Config.AI_REPLY_TIMEOUT_S + (30 if images else 0))
        out: dict = {}
        try:
            answer = await asyncio.wait_for(
                self._stream_turn(
                    placeholder, messages,
                    user_id=author.id, guild_id=guild_id,
                    channel_id=target_channel.id, out=out,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            answer = None

        if not answer:
            cancel_ai_quota_reservation(author.id, guild_id, quota_ts)
            try:
                await placeholder.edit(content="AI didn't respond. Try again in a sec.")
            except discord.HTTPException:
                pass
            return

        answer = sanitize_output(answer, guild)
        if not answer or looks_like_acrostic(answer):
            cancel_ai_quota_reservation(author.id, guild_id, quota_ts)
            try:
                await placeholder.edit(content="nice try. not playing that game.")
            except discord.HTTPException:
                pass
            return

        # Attach the action view (Regenerate / Continue / Sources).
        truncated = out.get("finish_reason") == "length" or len(answer) > 1900
        view = self._build_view(
            placeholder, author.id, target_channel.id, messages,
            out.get("tool_schemas"), accumulated=answer, truncated=truncated,
            sources=out.get("sources") or [],
        )
        try:
            await placeholder.edit(
                content=_stamp_footer(answer, out.get("meter")), view=view)
        except discord.HTTPException:
            pass

        self.bot.remember_ai_message(placeholder.id)

        # Persist conversation + learn.
        if not opted_out or history_key != "default":
            await db.save_ai_message(author.id, guild_id, "user", question, history_key)
            await db.save_ai_message(author.id, guild_id, "assistant", answer, history_key)
        if not opted_out:
            self._spawn(run_post_message_tasks(
                db, user_id=author.id, guild_id=guild_id,
                display_name=author.display_name, content=question,
                ai_complete_fn=complete_default, assistant_reply=answer,
            ))

        training = getattr(self.bot, "training", None)
        if training is not None:
            self._spawn(training.log_turn(
                user_id=author.id, guild_id=guild_id, channel_id=target_channel.id,
                user_message=question, assistant_reply=answer,
                messages=[*messages, {"role": "assistant", "content": answer}],
                model=Config.OPENROUTER_MODEL,
            ))

    async def _stream_turn(
        self, placeholder: discord.Message, messages: list[dict],
        *, user_id: int, guild_id: int, channel_id: int, out: dict,
    ) -> str | None:
        """Stream a tool-calling turn into ``placeholder``. Returns final text."""
        pick = await resolve_model(self.bot.db, guild_id, "chat")
        model = pick.model if pick.provider == Config.CHAT_BACKEND else None
        tool_schemas = self.bot.tools.as_openai_tools() if self.bot.tools else []
        out["tool_schemas"] = tool_schemas

        meter = TurnMeter()
        out["meter"] = meter
        tool_ctx = ToolContext(
            bot=self.bot, db=self.bot.db,
            user_id=user_id, guild_id=guild_id, channel_id=channel_id,
            memory=self.bot.memory, registry=self.bot.tools,
            meter=meter,
        )

        async def _approver(name: str, args: dict) -> bool:
            return await self._collect_tool_approval(
                placeholder.channel, user_id, name, args)

        tool_ctx.approver = _approver
        renderer = StreamRenderer(placeholder)
        animator = asyncio.create_task(renderer.run())
        final_text = ""
        try:
            async for ev in run_agent_stream(
                messages, tool_ctx, model=model,
                max_tokens=_MAX_TOKENS, temperature=0.85,
                tools_override=tool_schemas or None,
            ):
                kind = ev.get("type")
                if kind == "sources":
                    out.setdefault("sources", []).extend(ev.get("results") or [])
                    await renderer.feed(ev)
                elif kind == "done":
                    final_text = ev.get("text") or ""
                    out["finish_reason"] = ev.get("finish_reason", "")
                    out["tool_names"] = ev.get("tool_names", [])
                    break
                elif kind == "error":
                    out["error"] = ev.get("error", "")
                    break
                else:
                    await renderer.feed(ev)
        except Exception as exc:  # noqa: BLE001
            log.warning("stream crashed: %s", exc)
        finally:
            renderer.stop()
            animator.cancel()
            try:
                await animator
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        return final_text or None

    async def _collect_tool_approval(
        self, channel, user_id: int, name: str, args: dict,
    ) -> bool:
        """Post an Approve / Reject prompt for one gated tool call.

        ``channel`` is the channel the turn is replying in -- a guild channel,
        a thread or a DM alike -- taken straight from the placeholder message,
        so the prompt always lands where the user is looking and never depends
        on a channel-cache lookup that misses for DMs. Returns the human
        decision; a send failure or an unanswered prompt counts as a refusal,
        so a gated tool is never run without an explicit yes. The prompt is
        edited in place with the outcome, so the channel keeps a record of who
        cleared what.
        """
        timeout = float(max(5, Config.AGENT_APPROVAL_TIMEOUT_S))
        decision: asyncio.Future[bool] = (
            asyncio.get_running_loop().create_future()
        )
        view = ApprovalView(user_id, decision, timeout=timeout)
        builder = card(
            "Tool approval needed",
            description=f"Archimedes wants to run `{name}`.\n"
                        f"{_format_tool_args(args)}",
            color=C_WARNING,
        )
        builder.footer(f"Decide within {int(timeout)}s -- no answer is a "
                       f"refusal.")
        try:
            prompt = await channel.send(
                content=f"<@{user_id}>", embed=builder.build(), view=view,
            )
        except discord.HTTPException:
            return False
        try:
            approved = await asyncio.wait_for(decision, timeout=timeout + 5)
        except asyncio.TimeoutError:
            approved = False
        view.stop()
        verdict = ("approved and is running" if approved
                   else "not approved and will not run")
        outcome = card(
            "Tool approval needed",
            description=f"`{name}` was {verdict}.",
            color=C_SUCCESS if approved else C_ERROR,
        )
        try:
            await prompt.edit(content=None, embed=outcome.build(), view=None)
        except discord.HTTPException:
            pass
        return approved

    def _build_view(
        self, placeholder: discord.Message, user_id: int, channel_id: int,
        messages: list[dict], tool_schemas, *, accumulated: str,
        truncated: bool, sources: list[dict],
    ) -> discord.ui.View:
        state = AskState(
            user_id=user_id, channel_id=channel_id, placeholder_id=placeholder.id,
            messages=messages, tool_schemas=tool_schemas,
            temperature=0.85, max_tokens=_MAX_TOKENS,
            timeout_s=float(Config.AI_REPLY_TIMEOUT_S),
            accumulated_reply=accumulated, was_truncated=truncated,
        )
        return AskReplyView(state, self, sources=sources)

    # ── regenerate / continue ─────────────────────────────────────────────────
    async def regenerate_turn(self, state: AskState) -> None:
        channel = self.bot.get_channel(state.channel_id)
        if channel is None:
            return
        try:
            placeholder = await channel.fetch_message(state.placeholder_id)
        except discord.HTTPException:
            return
        try:
            await placeholder.edit(content="_regenerating..._", view=None)
        except discord.HTTPException:
            return
        out: dict = {}
        guild_id = getattr(channel.guild, "id", 0)
        try:
            answer = await asyncio.wait_for(
                self._stream_turn(
                    placeholder, list(state.messages),
                    user_id=state.user_id, guild_id=guild_id,
                    channel_id=state.channel_id, out=out,
                ),
                timeout=state.timeout_s,
            )
        except asyncio.TimeoutError:
            answer = None
        if not answer:
            try:
                await placeholder.edit(content="AI didn't respond. Try again.")
            except discord.HTTPException:
                pass
            return
        answer = sanitize_output(answer, getattr(channel, "guild", None))
        truncated = out.get("finish_reason") == "length" or len(answer) > 1900
        view = self._build_view(
            placeholder, state.user_id, state.channel_id, state.messages,
            state.tool_schemas, accumulated=answer, truncated=truncated,
            sources=out.get("sources") or [],
        )
        try:
            await placeholder.edit(
                content=_stamp_footer(answer, out.get("meter")), view=view)
        except discord.HTTPException:
            pass
        self.bot.remember_ai_message(placeholder.id)

    async def continue_turn(self, state: AskState) -> None:
        channel = self.bot.get_channel(state.channel_id)
        if channel is None:
            return
        try:
            placeholder = await channel.send("_continuing..._")
        except discord.HTTPException:
            return
        convo = list(state.messages) + [
            {"role": "assistant", "content": state.accumulated_reply[-3000:]},
            {"role": "user", "content":
                "Continue your previous response from where you left off. "
                "Do not repeat anything you already said."},
        ]
        out: dict = {}
        guild_id = getattr(channel.guild, "id", 0)
        try:
            answer = await asyncio.wait_for(
                self._stream_turn(
                    placeholder, convo, user_id=state.user_id,
                    guild_id=guild_id, channel_id=state.channel_id, out=out,
                ),
                timeout=state.timeout_s,
            )
        except asyncio.TimeoutError:
            answer = None
        if not answer:
            try:
                await placeholder.edit(content="AI didn't continue. Try again.")
            except discord.HTTPException:
                pass
            return
        answer = sanitize_output(answer, getattr(channel, "guild", None))
        combined = state.accumulated_reply + answer
        truncated = out.get("finish_reason") == "length" or len(combined) > 1900
        view = self._build_view(
            placeholder, state.user_id, state.channel_id, state.messages,
            state.tool_schemas, accumulated=combined, truncated=truncated,
            sources=out.get("sources") or [],
        )
        try:
            await placeholder.edit(
                content=_stamp_footer(answer, out.get("meter")), view=view)
        except discord.HTTPException:
            pass
        self.bot.remember_ai_message(placeholder.id)

    # ── ambient (called by the sidecar cog) ───────────────────────────────────
    async def maybe_ambient(self, message: discord.Message) -> None:
        """Optionally chime in on ambient chatter. Used by the sidecar cog."""
        guild = message.guild
        flags = await self.bot.db.get_ai_flags(guild.id)
        if not flags["chat"]:
            return
        if await self.bot.db.is_ai_opted_out(message.author.id, guild.id):
            return
        content = sanitize_input(message.content).strip()
        if not content or is_injection_attempt(content):
            return
        allowed, _r, quota_ts = await reserve_ai_quota(message.author.id, guild.id)
        if not allowed:
            return
        ctx_obj = await gather_chat_context(
            self.bot, mode=ChatMode.AMBIENT, user_id=message.author.id,
            guild_id=guild.id, channel=message.channel,
            member=message.author if isinstance(message.author, discord.Member) else None,
            display_name=message.author.display_name, user_message=content,
        )
        payload = [
            {"role": "system", "content": build_system_prompt(ctx_obj)},
            {"role": "user", "content": f"{message.author.display_name}: {content}"},
        ]
        meter = TurnMeter()
        try:
            answer = await asyncio.wait_for(
                complete_default(payload, max_tokens=120, temperature=0.9,
                                 meter=meter),
                timeout=20.0,
            )
        except asyncio.TimeoutError:
            answer = None
        if not answer:
            cancel_ai_quota_reservation(message.author.id, guild.id, quota_ts)
            return
        cleaned = sanitize_output(answer, guild).strip()
        if not cleaned or cleaned.upper().strip(" .!?") == "SKIP":
            cancel_ai_quota_reservation(message.author.id, guild.id, quota_ts)
            return
        try:
            async with message.channel.typing():
                await asyncio.sleep(random.uniform(1.0, 2.5))
                sent = await message.channel.send(_stamp_footer(cleaned, meter))
            self.bot.remember_ai_message(sent.id)
        except discord.HTTPException:
            cancel_ai_quota_reservation(message.author.id, guild.id, quota_ts)

    # ── helpers ───────────────────────────────────────────────────────────────
    async def _threaded(self, message: discord.Message, flags: dict) -> bool:
        if message.guild is None:
            return False  # threads do not exist in a direct message
        if not flags.get("threaded", True):
            return False
        if isinstance(message.channel, discord.Thread):
            return False
        mode = await self.bot.db.get_archimedes_reply_mode(
            message.author.id, message.guild.id,
        )
        return mode != "chat"

    async def _safe_reply(self, message: discord.Message, text: str) -> None:
        try:
            await message.reply(text, mention_author=False)
        except discord.HTTPException:
            pass


async def setup(bot) -> None:
    await bot.add_cog(ChatBrain(bot))
