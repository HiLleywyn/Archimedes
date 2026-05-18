"""framework/log.py -- logging setup for the Disco AI bot."""
from __future__ import annotations

import logging
import sys

from config import Config

_CONFIGURED = False


def setup_logging() -> None:
    """Configure root logging once. Uses rich when available, else plain."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level = getattr(logging, Config.LOG_LEVEL, logging.INFO)
    handler: logging.Handler
    try:
        from rich.logging import RichHandler

        handler = RichHandler(rich_tracebacks=True, show_path=False)
        fmt = "%(message)s"
    except Exception:  # noqa: BLE001
        handler = logging.StreamHandler(sys.stdout)
        fmt = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

    logging.basicConfig(level=level, format=fmt, handlers=[handler], datefmt="%H:%M:%S")
    # discord.py is chatty at INFO -- keep it to warnings.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("asyncpg").setLevel(logging.WARNING)


def print_banner() -> None:
    """Log a short startup banner."""
    log = logging.getLogger("discoai")
    log.info("=" * 56)
    log.info("  Disco AI -- standalone AI chat bot")
    log.info("  prefix=%s  backend=%s", Config.PREFIX, Config.CHAT_BACKEND)
    log.info("=" * 56)
