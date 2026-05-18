"""cogs/meta.py -- help, ping and about for the bot itself."""
from __future__ import annotations

import time

import discord
from discord.ext import commands

from config import Config
from framework.context import DiscoContext
from framework.embed import card
from framework.ui import C_INFO, C_PURPLE


class Meta(commands.Cog):
    """Bot meta commands: help, ping, about."""

    def __init__(self, bot) -> None:
        self.bot = bot

    @commands.command(name="help")
    async def help_cmd(self, ctx: DiscoContext) -> None:
        """Show what Disco can do."""
        p = ctx.prefix
        b = (
            card(
                "Disco AI",
                color=C_PURPLE,
                description=(
                    "A memory-backed AI chat companion. Just `@`mention me, "
                    "reply to one of my messages, or use the commands below."
                ),
            )
            .field(
                "Chatting",
                f"`@Disco <message>` -- talk to me\n"
                f"`{p}ask <question>` -- ask me something\n"
                "Reply to any of my messages to keep the conversation going.",
                False,
            )
            .field(
                "Your settings",
                f"`{p}disco` -- tune how I talk to you\n"
                f"`{p}disco ctx` -- see what I have learned about you\n"
                f"`{p}disco optout` -- stop me learning about you",
                False,
            )
            .field(
                "Staff",
                f"`{p}ai` -- the AI control surface (Manage Server)",
                False,
            )
            .footer("Disco AI -- standalone AI chat bot")
        )
        await ctx.reply(embed=b.build(), mention_author=False)

    @commands.command(name="ping")
    async def ping_cmd(self, ctx: DiscoContext) -> None:
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
    async def about_cmd(self, ctx: DiscoContext) -> None:
        """About this bot."""
        b = (
            card("About Disco AI", color=C_PURPLE, description=(
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
