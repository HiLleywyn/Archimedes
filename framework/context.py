"""framework/context.py -- the custom command context.

``DiscoContext`` adds reply helpers so cogs never build raw error/success
embeds. Always prefer these over ``ctx.reply(embed=discord.Embed(...))``.
"""
from __future__ import annotations

import asyncio

import discord
from discord.ext import commands

from framework.embed import card
from framework.ui import C_ERROR, C_SUCCESS, C_WARNING


class DiscoContext(commands.Context):
    """Command context with embed reply helpers and a ``db`` shortcut."""

    @property
    def db(self):
        """The shared database handle (set on the bot at startup)."""
        return self.bot.db

    @property
    def guild_id(self) -> int:
        return self.guild.id if self.guild else 0

    # ── reply helpers ─────────────────────────────────────────────────────────
    async def reply_error(self, msg: str, *, title: str = "Error") -> discord.Message:
        return await self.reply(
            embed=card(title, description=msg, color=C_ERROR).build(),
            mention_author=False,
        )

    async def reply_success(self, msg: str, *, title: str = "Done") -> discord.Message:
        return await self.reply(
            embed=card(title, description=msg, color=C_SUCCESS).build(),
            mention_author=False,
        )

    async def reply_cooldown(self, seconds: float) -> discord.Message:
        return await self.reply(
            embed=card(
                "Slow down",
                description=f"Try again in {seconds:.0f}s.",
                color=C_WARNING,
            ).build(),
            mention_author=False,
        )

    async def reply_error_hint(
        self, msg: str, *, hint: str = "", command_name: str = "",
    ) -> discord.Message:
        b = card("Error", description=msg, color=C_ERROR)
        if hint:
            b = b.field("Hint", hint, False)
        return await self.reply(embed=b.build(), mention_author=False)

    async def confirm(self, prompt: str, *, timeout: float = 30.0) -> bool:
        """Show a yes/no confirmation dialog. Returns the user's choice."""
        view = _ConfirmView(self.author.id, timeout=timeout)
        msg = await self.reply(
            embed=card("Confirm", description=prompt, color=C_WARNING).build(),
            view=view,
            mention_author=False,
        )
        await view.wait()
        try:
            await msg.edit(view=None)
        except discord.HTTPException:
            pass
        return bool(view.value)

    async def paginate(self, pages: list[discord.Embed], *, timeout: float = 120.0) -> None:
        """Send a list of embeds with prev/next navigation."""
        from framework.ui import Paginator

        await Paginator(pages, author_id=self.author.id, timeout=timeout).send(self)


class _ConfirmView(discord.ui.View):
    def __init__(self, author_id: int, *, timeout: float) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This prompt isn't yours.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, _b: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, _b: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()
