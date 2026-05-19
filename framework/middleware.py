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


def require_owner(func):
    """Gate a command behind the bot owner.

    OWNER_ID is honored first; if it is unset, fall back to the Discord
    application owner discovered by discord.py at login.
    """
    from config import Config

    async def predicate(ctx) -> bool:
        if Config.OWNER_ID and int(ctx.author.id) == int(Config.OWNER_ID):
            return True
        try:
            if await ctx.bot.is_owner(ctx.author):
                return True
        except Exception:  # noqa: BLE001
            pass
        raise commands.CheckFailure("Only the bot owner can use this command.")

    return commands.check(predicate)(func)
