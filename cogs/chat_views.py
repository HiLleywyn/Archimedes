"""cogs/chat_views.py -- streaming renderer and the reply action view.

``StreamRenderer`` edits a placeholder message on a throttle as model
tokens arrive, with a spinner and a tool-call status line so the user has
live feedback. ``AskReplyView`` adds Regenerate / Continue buttons to a
finished reply.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import discord

log = logging.getLogger(__name__)

_SPINNER = ("|", "/", "-", "\\")
_EDIT_THROTTLE = 1.3
_DISCORD_LIMIT = 1990


class StreamRenderer:
    """Renders a streaming chat turn into one placeholder message."""

    def __init__(self, placeholder: discord.Message) -> None:
        self.placeholder = placeholder
        self.buffer = ""
        self.phase = "thinking"
        self.tool = ""
        self.tool_names: list[str] = []
        self.frame = 0
        self._last_edit = 0.0
        self._stop = asyncio.Event()

    async def run(self) -> None:
        """Background animator: keeps the spinner alive between deltas."""
        try:
            while not self._stop.is_set():
                await asyncio.sleep(1.4)
                if self._stop.is_set():
                    return
                await self._render(force=True)
        except asyncio.CancelledError:
            pass

    def stop(self) -> None:
        self._stop.set()

    async def feed(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "delta":
            self.buffer += event.get("text", "")
            self.phase = "writing"
            await self._render()
        elif kind == "reset":
            self.buffer = ""
            self.phase = "thinking"
        elif kind == "tool_start":
            self.tool = event.get("tool", "")
            self.phase = "tool"
            await self._render(force=True)
        elif kind == "tool_done":
            name = event.get("tool", "")
            if name:
                self.tool_names.append(name)
            self.tool = ""
        elif kind == "approval_pending":
            self.tool = event.get("tool", "")
            self.phase = "approval"
            await self._render(force=True)
        elif kind == "approval_resolved":
            self.tool = ""
            self.phase = "thinking"
            await self._render(force=True)

    def _status_line(self) -> str:
        spin = _SPINNER[self.frame % len(_SPINNER)]
        if self.phase == "approval" and self.tool:
            return f"_{spin} waiting for approval to run `{self.tool}`..._"
        if self.phase == "tool" and self.tool:
            return f"_{spin} running `{self.tool}`..._"
        if self.phase == "writing":
            return ""
        return f"_{spin} thinking..._"

    def _body(self) -> str:
        status = self._status_line()
        text = self.buffer.strip()
        if text and status:
            return (text[:_DISCORD_LIMIT] + "\n" + status)[:_DISCORD_LIMIT + 40]
        if text:
            return text[:_DISCORD_LIMIT]
        return status or "_thinking..._"

    async def _render(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_edit) < _EDIT_THROTTLE:
            return
        self.frame += 1
        try:
            await self.placeholder.edit(content=self._body())
            self._last_edit = now
        except discord.HTTPException:
            pass


@dataclass
class AskState:
    """Replayable state for the Regenerate / Continue buttons."""

    user_id: int
    channel_id: int
    placeholder_id: int
    messages: list[dict]
    tool_schemas: list[dict] | None
    temperature: float
    max_tokens: int
    timeout_s: float
    accumulated_reply: str = ""
    was_truncated: bool = False
    created_at: float = field(default_factory=time.monotonic)


class AskReplyView(discord.ui.View):
    """Regenerate / Continue / Sources buttons attached to a finished reply."""

    def __init__(self, state: AskState, cog, *, sources: list[dict] | None = None,
                 timeout: float = 600.0) -> None:
        super().__init__(timeout=timeout)
        self.state = state
        self.cog = cog
        self.sources = [r for r in (sources or []) if r.get("url")][:8]
        if not state.was_truncated:
            self.remove_item(self.continue_btn)
        if not self.sources:
            self.remove_item(self.sources_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.user_id:
            await interaction.response.send_message(
                "Only the person who asked can use these.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Regenerate", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def regenerate_btn(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer()
        await self.cog.regenerate_turn(self.state)

    @discord.ui.button(label="Continue", style=discord.ButtonStyle.secondary, emoji="➡️")
    async def continue_btn(self, interaction: discord.Interaction, _b: discord.ui.Button):
        await interaction.response.defer()
        await self.cog.continue_turn(self.state)

    @discord.ui.button(label="Sources", style=discord.ButtonStyle.secondary, emoji="🔗")
    async def sources_btn(self, interaction: discord.Interaction, _b: discord.ui.Button):
        lines = []
        for i, r in enumerate(self.sources, 1):
            title = (r.get("title") or "result")[:80]
            lines.append(f"{i}. [{title}]({r.get('url')})")
        await interaction.response.send_message(
            "\n".join(lines) or "(no sources)", ephemeral=True,
        )


class ApprovalView(discord.ui.View):
    """Approve / Reject buttons gating one tool call on a human decision.

    The view resolves ``decision`` -- a future the agent loop awaits -- to
    True on approval and False on rejection or timeout. Only the member who
    started the turn may decide; an unanswered prompt is treated as a refusal,
    so a gated tool is never run without an explicit yes.
    """

    def __init__(self, user_id: int, decision: "asyncio.Future[bool]", *,
                 timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self._decision = decision

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the person who asked can decide this.", ephemeral=True,
            )
            return False
        return True

    def _resolve(self, approved: bool) -> None:
        if not self._decision.done():
            self._decision.set_result(approved)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success,
                       emoji="✅")
    async def approve_btn(self, interaction: discord.Interaction,
                          _b: discord.ui.Button):
        await interaction.response.defer()
        self._resolve(True)
        self.stop()

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger,
                       emoji="🛑")
    async def reject_btn(self, interaction: discord.Interaction,
                         _b: discord.ui.Button):
        await interaction.response.defer()
        self._resolve(False)
        self.stop()

    async def on_timeout(self) -> None:
        self._resolve(False)
