"""cogs/chat.py -- the chat brain.

Owns every conversational path: an @mention, a reply to one of Archimedes's
answers, the .ask command, and (optionally) ambient chime-ins. All four
share one pipeline: gather context -> build the system prompt -> stream a
tool-calling turn into a placeholder -> persist + learn.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

import discord
from discord.ext import commands

from config import Config
from framework.context import ArchimedesContext
from framework.middleware import guild_only, no_bots
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
from cogs.chat_views import AskReplyView, AskState, StreamRenderer

log = logging.getLogger(__name__)

_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_MAX_TOKENS = 1000


def _image_urls(message: discord.Message) -> list[str]:
    """Collect image URLs from a message's attachments and embeds."""
    urls: list[str] = []
    for att in message.attachments:
        ct = (att.content_type or "").lower()
        if ct.startswith("image") or att.filename.lower().endswith(_IMAGE_EXT):
            urls.append(att.url)
    for emb in message.embeds:
        if emb.image and emb.image.url:
            urls.append(emb.image.url)
        elif emb.thumbnail and emb.thumbnail.url:
            urls.append(emb.thumbnail.url)
    return urls[:4]


def _history_key(channel) -> str:
    """Conversation history bucket: one per thread, shared for inline chat."""
    return f"thread:{channel.id}" if isinstance(channel, discord.Thread) else "default"


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
        if message.author.bot or not message.guild:
            return
        if message.content.startswith(Config.PREFIX):
            return  # a command -- handled by the command processor

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
        author = message.author
        flags = await self.bot.db.get_ai_flags(guild.id)
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
        question = sanitize_input(raw).strip()
        images = _image_urls(message)
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

        allowed, _remaining, quota_ts = await reserve_ai_quota(author.id, guild.id)
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
            cancel_ai_quota_reservation(author.id, guild.id, quota_ts)

    async def _respond(
        self, message: discord.Message, mode: ChatMode, question: str,
        images: list[str], flags: dict, quota_ts: float,
    ) -> None:
        guild = message.guild
        author = message.author
        db = self.bot.db
        opted_out = await db.is_ai_opted_out(author.id, guild.id)

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
            guild_id=guild.id,
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
            blocks: list[dict] = [{"type": "text", "text": question}]
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
            cancel_ai_quota_reservation(author.id, guild.id, quota_ts)
            return

        timeout = float(Config.AI_REPLY_TIMEOUT_S + (30 if images else 0))
        out: dict = {}
        try:
            answer = await asyncio.wait_for(
                self._stream_turn(
                    placeholder, messages,
                    user_id=author.id, guild_id=guild.id,
                    channel_id=target_channel.id, out=out,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            answer = None

        if not answer:
            cancel_ai_quota_reservation(author.id, guild.id, quota_ts)
            try:
                await placeholder.edit(content="AI didn't respond. Try again in a sec.")
            except discord.HTTPException:
                pass
            return

        answer = sanitize_output(answer, guild)
        if not answer or looks_like_acrostic(answer):
            cancel_ai_quota_reservation(author.id, guild.id, quota_ts)
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
            await placeholder.edit(content=answer[:1990], view=view)
        except discord.HTTPException:
            pass

        self.bot.remember_ai_message(placeholder.id)

        # Persist conversation + learn.
        if not opted_out or history_key != "default":
            await db.save_ai_message(author.id, guild.id, "user", question, history_key)
            await db.save_ai_message(author.id, guild.id, "assistant", answer, history_key)
        if not opted_out:
            self._spawn(run_post_message_tasks(
                db, user_id=author.id, guild_id=guild.id,
                display_name=author.display_name, content=question,
                ai_complete_fn=complete_default, assistant_reply=answer,
            ))

        training = getattr(self.bot, "training", None)
        if training is not None:
            self._spawn(training.log_turn(
                user_id=author.id, guild_id=guild.id, channel_id=target_channel.id,
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

        tool_ctx = ToolContext(
            bot=self.bot, db=self.bot.db,
            user_id=user_id, guild_id=guild_id, channel_id=channel_id,
            memory=self.bot.memory, registry=self.bot.tools,
        )
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
            await placeholder.edit(content=answer[:1990], view=view)
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
            await placeholder.edit(content=answer[:1990], view=view)
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
        try:
            answer = await asyncio.wait_for(
                complete_default(payload, max_tokens=120, temperature=0.9),
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
                sent = await message.channel.send(cleaned[:1990])
            self.bot.remember_ai_message(sent.id)
        except discord.HTTPException:
            cancel_ai_quota_reservation(message.author.id, guild.id, quota_ts)

    # ── helpers ───────────────────────────────────────────────────────────────
    async def _threaded(self, message: discord.Message, flags: dict) -> bool:
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
