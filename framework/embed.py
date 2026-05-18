"""framework/embed.py -- fluent embed builder.

``card()`` is the canonical entry point for all embed creation. Never
construct ``discord.Embed`` directly; use this so styling stays consistent.

Usage::

    from framework.embed import card
    from framework.ui import C_INFO

    embed = (
        card("Title", description="Body", color=C_INFO)
        .field("Key", "Value", inline=True)
        .footer("a footer")
        .build()
    )
"""
from __future__ import annotations

import datetime

import discord


class CardBuilder:
    """Fluent embed card builder. Every setter returns ``self`` for chaining."""

    __slots__ = ("_embed",)

    def __init__(
        self,
        title: str = "",
        *,
        description: str | None = None,
        color: int | None = None,
    ) -> None:
        self._embed = discord.Embed(
            title=title or None,
            description=description,
            color=color,
        )

    def description(self, text: str) -> "CardBuilder":
        self._embed.description = text
        return self

    def color(self, value: int) -> "CardBuilder":
        self._embed.color = discord.Colour(value)
        return self

    def url(self, value: str) -> "CardBuilder":
        self._embed.url = value
        return self

    def field(self, name: str, value: str, inline: bool = False) -> "CardBuilder":
        self._embed.add_field(name=name, value=value, inline=inline)
        return self

    def field_if(
        self, condition: bool, name: str, value: str, inline: bool = False,
    ) -> "CardBuilder":
        if condition:
            self._embed.add_field(name=name, value=value, inline=inline)
        return self

    def blank(self, inline: bool = False) -> "CardBuilder":
        self._embed.add_field(name="​", value="​", inline=inline)
        return self

    def footer(self, text: str, icon_url: str | None = None) -> "CardBuilder":
        self._embed.set_footer(text=text, icon_url=icon_url)
        return self

    def author(
        self, name: str, icon_url: str | None = None, url: str | None = None,
    ) -> "CardBuilder":
        kwargs: dict = {"name": name}
        if icon_url:
            kwargs["icon_url"] = icon_url
        if url:
            kwargs["url"] = url
        self._embed.set_author(**kwargs)
        return self

    def thumbnail(self, url: str) -> "CardBuilder":
        if url:
            self._embed.set_thumbnail(url=url)
        return self

    def image(self, url: str) -> "CardBuilder":
        if url:
            self._embed.set_image(url=url)
        return self

    def timestamp(self, dt: datetime.datetime | None = None) -> "CardBuilder":
        self._embed.timestamp = dt or datetime.datetime.now(datetime.timezone.utc)
        return self

    def build(self) -> discord.Embed:
        return self._embed


def card(
    title: str = "",
    *,
    description: str | None = None,
    color: int | None = None,
) -> CardBuilder:
    """Create a fluent embed card builder."""
    return CardBuilder(title, description=description, color=color)
