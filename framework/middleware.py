"""framework/middleware.py -- command decorators / checks.

Decorator stacking order on a command::

    @commands.command(name="foo")
    @guild_only
    @no_bots
    async def foo(self, ctx: ArchimedesContext) -> None:
"""
from __future__ import annotations

from discord.ext import commands


def guild_only(func):
    """Reject commands run in DMs."""

    async def predicate(ctx) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage("This command only works in a server.")
        return True

    return commands.check(predicate)(func)


def no_bots(func):
    """Reject commands invoked by other bots / webhooks."""

    async def predicate(ctx) -> bool:
        if ctx.author.bot:
            raise commands.CheckFailure("Bots cannot use this command.")
        return True

    return commands.check(predicate)(func)


def require_manage_guild(func):
    """Gate a command behind the Manage Server permission."""

    async def predicate(ctx) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage("This command only works in a server.")
        perms = ctx.author.guild_permissions
        if not (perms.manage_guild or perms.administrator):
            raise commands.CheckFailure(
                "You need the Manage Server permission to use this command."
            )
        return True

    return commands.check(predicate)(func)
