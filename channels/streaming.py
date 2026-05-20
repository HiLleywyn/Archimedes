"""channels/streaming.py -- preview-message streaming for Discord.

A long Archimedes reply can take seconds. The streaming preview keeps
the channel feeling alive: the bot posts a draft message immediately,
then edits it as tokens arrive, then finalises it once the model
stops. The behaviour matches the ``partial / block / progress / off``
modes documented in the OpenClaw Discord integration.

The actual model streaming still happens in ``ai.client.stream_completion``;
this module only owns the message-editing side. It is intentionally
small so a channel that does not stream can simply not import it.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import discord

log = logging.getLogger(__name__)

# Discord rate-limits edits at roughly one per second per channel; we
# stay well under that so a bursty token stream does not get throttled.
EDIT_INTERVAL_S = 1.2
MAX_MESSAGE_CHARS = 1900   # Discord cap is 2000; we leave headroom.


@dataclass
class StreamingOptions:
    mode: str = "partial"   # "off" | "partial" | "block" | "progress"
    placeholder: str = "..."


class PreviewStreamer:
    """Owns one preview message and applies token deltas to it.

    Usage:
        async with PreviewStreamer(message_target, options) as stream:
            async for chunk in model_stream:
                await stream.append(chunk)
            await stream.finalise(final_text)
    """

    def __init__(self, target, options: StreamingOptions) -> None:
        self.target = target          # discord.abc.Messageable
        self.options = options
        self._buffer: list[str] = []
        self._message: discord.Message | None = None
        self._last_edit = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "PreviewStreamer":
        if self.options.mode == "off":
            return self
        try:
            self._message = await self.target.send(self.options.placeholder)
        except discord.HTTPException as exc:
            log.warning("Preview streamer could not send placeholder: %s", exc)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._message is not None and self._buffer and exc is not None:
            # On crash, leave the partial text so the user sees what we had.
            try:
                await self._message.edit(content=self._current_text())
            except discord.HTTPException:
                pass

    async def append(self, chunk: str) -> None:
        if self.options.mode in ("off", "block"):
            self._buffer.append(chunk)
            return
        self._buffer.append(chunk)
        now = time.monotonic()
        if (now - self._last_edit) < EDIT_INTERVAL_S:
            return
        async with self._lock:
            if self._message is None:
                return
            try:
                await self._message.edit(content=self._current_text())
                self._last_edit = now
            except discord.HTTPException as exc:
                log.debug("Preview edit failed: %s", exc)

    async def finalise(self, final_text: str) -> discord.Message | None:
        """Replace the preview with the final text and return the message.

        Returns None when the streamer was disabled (mode='off'); the
        channel should ``await target.send(...)`` itself in that case.
        """
        if self.options.mode == "off":
            return None
        text = final_text[:MAX_MESSAGE_CHARS] or self.options.placeholder
        if self._message is None:
            try:
                return await self.target.send(text)
            except discord.HTTPException as exc:
                log.warning("Preview finalise send failed: %s", exc)
                return None
        try:
            await self._message.edit(content=text)
            return self._message
        except discord.HTTPException as exc:
            log.warning("Preview finalise edit failed: %s", exc)
            return self._message

    def _current_text(self) -> str:
        joined = "".join(self._buffer).strip()
        if not joined:
            return self.options.placeholder
        return joined[:MAX_MESSAGE_CHARS]
