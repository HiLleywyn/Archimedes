"""main.py -- entry point for the Archimedes chat bot.

Validates configuration, wires SIGTERM/SIGINT to a graceful shutdown so a
container redeploy drains cleanly, and retries with backoff when Discord
rate-limits the login.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from config import Config
from framework.bot import ArchimedesBot
from framework.log import print_banner, setup_logging

log = logging.getLogger("archimedes")


def _install_signal_handlers(bot: ArchimedesBot) -> None:
    loop = asyncio.get_running_loop()
    triggered = False

    def _handle(sig: signal.Signals) -> None:
        nonlocal triggered
        if triggered:
            return
        triggered = True
        log.warning("Received %s -- shutting down gracefully", sig.name)
        loop.create_task(bot.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle, sig)
        except NotImplementedError:
            pass  # non-Unix event loop


async def main() -> None:
    setup_logging()
    print_banner()

    problems = Config.validate()
    if problems:
        for p in problems:
            log.error("config: %s", p)
        raise SystemExit(1)

    max_retries = 5
    for attempt in range(max_retries):
        try:
            async with ArchimedesBot() as bot:
                _install_signal_handlers(bot)
                await bot.start(Config.TOKEN)
            log.info("Bot exited cleanly.")
            return
        except KeyboardInterrupt:
            log.warning("Interrupted -- exiting.")
            return
        except Exception as exc:  # noqa: BLE001
            rate_limited = "429" in str(exc) or "rate limit" in str(exc).lower()
            if rate_limited and attempt < max_retries - 1:
                delay = 2 ** (attempt + 2)
                log.warning("Rate limited on login (%d/%d) -- retry in %ds",
                            attempt + 1, max_retries, delay)
                await asyncio.sleep(delay)
                continue
            log.exception("Fatal error: %s", exc)
            raise


if __name__ == "__main__":
    asyncio.run(main())
