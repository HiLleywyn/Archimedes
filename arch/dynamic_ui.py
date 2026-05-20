"""arch/dynamic_ui.py -- channel-agnostic UI primitives.

Archimedes's signature trick is that a reply is more than text -- it is a
small, structured card the channel renders natively. A Discord channel
turns a ``Card`` into an embed with view-attached buttons; a future web
channel turns the same card into HTML; a CLI channel falls back to plain
prose. Plugins compose cards through ``arch.card`` (see
``framework/plugins/api.py``).

These types are intentionally serialisable and free of any Discord import,
so a renderer outside ``channels/`` can consume them too.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Atoms ─────────────────────────────────────────────────────────────────────
@dataclass
class Button:
    label: str
    action: str                 # opaque id the channel routes back on click
    style: str = "primary"      # "primary" | "secondary" | "danger" | "link"
    url: str = ""               # set for style == "link"
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Suggestion:
    """A one-click follow-up prompt the user can send back. Rendered on
    Discord as a row of secondary buttons under the card."""

    label: str
    prompt: str                 # the text submitted on click


@dataclass
class StatTile:
    """A big-number tile: ``$1.3B / Contract Backlog / As of May 2026``."""

    value: str
    label: str
    sublabel: str = ""


@dataclass
class Section:
    """A heading plus a body. ``items`` is the body's structured payload --
    a list of strings for a bullet list, a list of ``(name, value)`` pairs
    for a field grid, or empty for prose-only sections."""

    heading: str = ""
    body: str = ""
    items: list[Any] = field(default_factory=list)
    style: str = "prose"        # "prose" | "bullets" | "fields"


# ── Cards ─────────────────────────────────────────────────────────────────────
@dataclass
class Card:
    """A rich, structured reply. The chat-bubble equivalent is ``Card(body=...)``."""

    title: str = ""
    body: str = ""
    sections: list[Section] = field(default_factory=list)
    tiles: list[StatTile] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)
    suggestions: list[Suggestion] = field(default_factory=list)
    accent: str = ""            # "info" | "ok" | "warn" | "error" | "" (default)
    footer: str = ""

    # ── Builder helpers (chaining keeps plugin call-sites tight) ──────────────
    def with_section(self, heading: str, body: str = "", *,
                     items: list | None = None, style: str = "prose") -> "Card":
        self.sections.append(Section(
            heading=heading, body=body, items=list(items or []), style=style,
        ))
        return self

    def with_tile(self, value: str, label: str, sublabel: str = "") -> "Card":
        self.tiles.append(StatTile(value=value, label=label, sublabel=sublabel))
        return self

    def with_button(self, label: str, action: str, *,
                    style: str = "primary", url: str = "",
                    payload: dict | None = None) -> "Card":
        self.buttons.append(Button(
            label=label, action=action, style=style, url=url,
            payload=dict(payload or {}),
        ))
        return self

    def with_suggestion(self, label: str, prompt: str) -> "Card":
        self.suggestions.append(Suggestion(label=label, prompt=prompt))
        return self


# ── Responses ─────────────────────────────────────────────────────────────────
@dataclass
class ArchResponse:
    """What ``ArchAgent.handle`` hands back to a channel.

    ``text`` is always populated -- a channel that does not understand cards
    falls back to it. ``card`` is the structured form a Discord embed or a
    web pane renders natively. ``followups`` are messages the channel
    delivers *after* the primary reply (a long answer split across two
    bubbles, or a separate "by the way" note).
    """

    text: str = ""
    card: Card | None = None
    followups: list["ArchResponse"] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)


# ── Plain-text rendering for CLI / fallback channels ──────────────────────────
def render_card_plain(card: Card) -> str:
    """Best-effort plaintext for a channel that cannot show structure."""
    lines: list[str] = []
    if card.title:
        lines.append(card.title)
        lines.append("=" * len(card.title))
    if card.body:
        lines.append(card.body)
    for tile in card.tiles:
        suffix = f"  ({tile.sublabel})" if tile.sublabel else ""
        lines.append(f"  {tile.value}  {tile.label}{suffix}")
    for sec in card.sections:
        if sec.heading:
            lines.append("")
            lines.append(sec.heading)
            lines.append("-" * len(sec.heading))
        if sec.body:
            lines.append(sec.body)
        if sec.style == "bullets":
            for it in sec.items:
                lines.append(f"  - {it}")
        elif sec.style == "fields":
            for pair in sec.items:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    lines.append(f"  {pair[0]}: {pair[1]}")
    if card.footer:
        lines.append("")
        lines.append(card.footer)
    return "\n".join(lines).strip()
