"""channels/renderers.py -- Discord renderer for arch dynamic UI.

Translates an ``arch.dynamic_ui.Card`` into a discord.py ``Embed`` plus a
``View`` carrying any buttons or suggestions the card declared. Channels
that do not understand cards (CLI, test harness) fall back to
``arch.dynamic_ui.render_card_plain``.

The mapping is intentionally restrained -- one embed per card, fields
for sections, value-and-label fields for tiles -- so the rendered reply
still feels native, not like an over-formatted dashboard.
"""
from __future__ import annotations

from typing import Any

import discord

from arch.dynamic_ui import Card, Section, StatTile
from framework.ui import C_ERROR, C_INFO, C_PURPLE, C_SUCCESS, C_WARNING


_ACCENT_COLOURS = {
    "info": C_INFO,
    "ok": C_SUCCESS,
    "warn": C_WARNING,
    "error": C_ERROR,
}


def card_to_embed(card: Card) -> discord.Embed:
    """One ``Embed`` for the prose layer of a card.

    Buttons and suggestions are carried by the view, not the embed; this
    function only handles title, body, tiles, sections and footer.
    """
    colour = _ACCENT_COLOURS.get(card.accent, C_PURPLE)
    embed = discord.Embed(
        title=card.title or None,
        description=card.body or None,
        colour=colour,
    )
    for tile in card.tiles:
        embed.add_field(
            name=tile.label or "​",
            value=_format_tile(tile),
            inline=True,
        )
    for sec in card.sections:
        embed.add_field(
            name=sec.heading or "​",
            value=_format_section(sec),
            inline=False,
        )
    if card.footer:
        embed.set_footer(text=card.footer)
    return embed


def _format_tile(tile: StatTile) -> str:
    parts = [f"**{tile.value}**"]
    if tile.sublabel:
        parts.append(f"_{tile.sublabel}_")
    return "\n".join(parts)


def _format_section(sec: Section) -> str:
    chunks: list[str] = []
    if sec.body:
        chunks.append(sec.body)
    if sec.style == "bullets":
        for item in sec.items:
            chunks.append(f"- {item}")
    elif sec.style == "fields":
        for pair in sec.items:
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                chunks.append(f"**{pair[0]}**: {pair[1]}")
    return "\n".join(chunks) or "​"


# ── View construction ────────────────────────────────────────────────────────
def card_to_view(
    card: Card,
    *,
    on_button=None,
    on_suggestion=None,
) -> discord.ui.View | None:
    """Build a discord.py ``View`` from a card's buttons and suggestions.

    ``on_button`` and ``on_suggestion`` are async callables the channel
    binds at construction time: ``async def on_button(interaction, action,
    payload)``, ``async def on_suggestion(interaction, prompt)``. Either
    may be left None; the corresponding buttons just become inert. Returns
    None when the card declares nothing interactive, so the channel skips
    attaching an empty view.
    """
    if not card.buttons and not card.suggestions:
        return None
    view = discord.ui.View(timeout=180)
    for button in card.buttons:
        view.add_item(_ButtonItem(button, on_button))
    for suggestion in card.suggestions:
        view.add_item(_SuggestionItem(suggestion, on_suggestion))
    return view


_STYLE_MAP = {
    "primary": discord.ButtonStyle.primary,
    "secondary": discord.ButtonStyle.secondary,
    "danger": discord.ButtonStyle.danger,
    "link": discord.ButtonStyle.link,
}


class _ButtonItem(discord.ui.Button):
    def __init__(self, button, on_button) -> None:
        style = _STYLE_MAP.get(button.style, discord.ButtonStyle.primary)
        if button.style == "link":
            super().__init__(label=button.label[:80], style=style,
                             url=button.url or None)
        else:
            super().__init__(label=button.label[:80], style=style)
        self._action = button.action
        self._payload = button.payload
        self._on_button = on_button

    async def callback(self, interaction: discord.Interaction) -> None:
        if self._on_button is None:
            await interaction.response.defer()
            return
        await self._on_button(interaction, self._action, self._payload)


class _SuggestionItem(discord.ui.Button):
    def __init__(self, suggestion, on_suggestion) -> None:
        super().__init__(
            label=suggestion.label[:80],
            style=discord.ButtonStyle.secondary,
        )
        self._prompt = suggestion.prompt
        self._on_suggestion = on_suggestion

    async def callback(self, interaction: discord.Interaction) -> None:
        if self._on_suggestion is None:
            await interaction.response.defer()
            return
        await self._on_suggestion(interaction, self._prompt)
