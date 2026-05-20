"""main.py -- entry point for the Archimedes chat bot.

Validates configuration, wires SIGTERM/SIGINT to a graceful shutdown so a
container redeploy drains cleanly, and keeps trying to reconnect to the
Discord gateway when it drops. An operator-issued `.ai restart` re-execs
the interpreter so a wedged process clears without a redeploy.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import discord

from config import Config
from framework.bot import ArchimedesBot
from framework.log import print_banner, setup_logging

log = logging.getLogger("archimedes")

# Reconnect backoff bounds (seconds).
_RECONNECT_MIN_S = 2
_RECONNECT_MAX_S = 60


def _install_signal_handlers(bot: ArchimedesBot) -> None:
    loop = asyncio.get_running_loop()
    triggered = False

    def _handle(sig: signal.Signals) -> None:
        nonlocal triggered
        if triggered:
            return
        triggered = True
        log.warning("Received %s -- shutting down gracefully", sig.name)
        bot.request_shutdown()
        loop.create_task(bot.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle, sig)
        except NotImplementedError:
            pass  # non-Unix event loop


def _reexec() -> None:
    """Replace this process with a fresh interpreter running the same args."""
    log.info("Re-execing for restart: %s %s", sys.executable, sys.argv)
    # Flush logs before handing the file descriptors to the new process.
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    os.execv(sys.executable, [sys.executable, *sys.argv])


async def main() -> None:
    setup_logging()
    print_banner()

    problems = Config.validate()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        raise SystemExit(1)

    attempt = 0
    restart_requested = False
    while True:
        bot = ArchimedesBot()
        try:
            async with bot:
                _install_signal_handlers(bot)
                try:
                    await bot.start(Config.TOKEN)
                except discord.LoginFailure:
                    log.exception("Login failed -- check DISCORD_TOKEN.")
                    return
                except KeyboardInterrupt:
                    log.warning("Interrupted -- exiting.")
                    bot.request_shutdown()
                except (
                    discord.ConnectionClosed,
                    discord.GatewayNotFound,
                    discord.HTTPException,
                    OSError,
                    asyncio.TimeoutError,
                ) as exc:
                    log.warning("Connection error: %s -- will reconnect.", exc)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Bot loop crashed: %s -- will reconnect.", exc)
        finally:
            restart_requested = bot.restart_requested
            shutdown_requested = bot.shutdown_requested
            ever_ready = bot.ever_ready

        if restart_requested:
            _reexec()
            return  # unreachable
        if shutdown_requested:
            log.info("Bot exited cleanly.")
            return

        # Unexpected drop: back off and reconnect. The delay grows with each
        # consecutive failure to avoid hammering Discord. Reset the counter
        # once we have seen a successful login, so a healthy bot that drops
        # hours later still reconnects fast.
        if ever_ready:
            attempt = 0
        attempt += 1
        delay = min(_RECONNECT_MAX_S, _RECONNECT_MIN_S * (2 ** (attempt - 1)))
        log.warning(
            "Reconnecting in %ds (attempt %d).", delay, attempt,
        )
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return


if __name__ == "__main__":
    asyncio.run(main())
