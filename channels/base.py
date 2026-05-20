"""channels/base.py -- the abstract Channel contract.

Every transport implements two things: ``start`` to begin listening, and
``dispatch`` to hand one inbound message to the agent and ship the
response back. The agent is the same object across every channel -- the
channel only knows how to render. ``NullChannel`` is the loop-free
implementation used by the test suite; production channels live next to
it (``DiscordChannel`` etc.).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from arch.core import ArchAgent, ChannelContext
from arch.dynamic_ui import ArchResponse

log = logging.getLogger(__name__)


class ChannelDispatchError(RuntimeError):
    """Raised when a channel could not dispatch a turn (policy denial,
    transport failure, agent crash). The caller decides whether to retry
    or surface the error to the user."""


class Channel(ABC):
    """Abstract message transport. Subclasses own their gateway loop."""

    #: A short, stable identifier for this transport ("discord", "web").
    name: str = ""

    def __init__(self, agent: ArchAgent) -> None:
        self.agent = agent

    @abstractmethod
    async def start(self) -> None:
        """Begin listening for messages."""

    @abstractmethod
    async def stop(self) -> None:
        """Shut the transport down cleanly."""

    async def dispatch(
        self, message_text: str, ctx: ChannelContext,
    ) -> ArchResponse:
        """Default dispatch path -- pure ``ArchAgent.handle``.

        A transport that needs to layer extras (rate limiting, typing
        indicators, history) overrides this and still calls
        ``self.agent.handle`` inside.
        """
        try:
            return await self.agent.handle(message_text, ctx)
        except Exception as exc:  # noqa: BLE001
            log.exception("Channel %s dispatch failed", self.name)
            raise ChannelDispatchError(str(exc)) from exc


# ── A no-op channel for tests ────────────────────────────────────────────────
class NullChannel(Channel):
    """A channel with no gateway. Tests instantiate it, call
    ``dispatch`` directly, and assert on the returned response."""

    name = "null"

    async def start(self) -> None:  # noqa: D401 -- intentional no-op
        return None

    async def stop(self) -> None:
        return None
