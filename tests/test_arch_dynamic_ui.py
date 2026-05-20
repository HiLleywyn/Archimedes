"""tests/test_arch_dynamic_ui.py -- the channel-agnostic card primitives."""
from __future__ import annotations

from arch.dynamic_ui import (
    ArchResponse, Button, Card, Section, StatTile, render_card_plain,
)


def test_card_builder_chain_appends_in_order() -> None:
    c = (
        Card(title="Firefly")
        .with_section("Key Projects", "Alpha, MLV, Blue Ghost, Elytra",
                      style="prose")
        .with_tile("$1.3B", "Contract Backlog", "As of May 2026")
        .with_button("Launch Schedule", "open_launch_schedule")
        .with_suggestion("Stock Performance", "Show FLY stock performance")
    )
    assert c.title == "Firefly"
    assert len(c.sections) == 1
    assert c.sections[0].heading == "Key Projects"
    assert len(c.tiles) == 1
    assert c.tiles[0].value == "$1.3B"
    assert len(c.buttons) == 1
    assert c.buttons[0].action == "open_launch_schedule"
    assert len(c.suggestions) == 1


def test_section_style_defaults_to_prose() -> None:
    sec = Section(heading="X", body="body")
    assert sec.style == "prose"
    assert sec.items == []


def test_button_link_carries_url_only_for_link_style() -> None:
    plain = Button(label="Click", action="x")
    link = Button(label="Visit", action="x", style="link",
                  url="https://example.com")
    assert plain.url == ""
    assert link.url.startswith("https://")


def test_render_card_plain_includes_title_body_tiles_sections_footer() -> None:
    c = (
        Card(title="Firefly", body="A space company.", footer="Cached 5m ago")
        .with_tile("$1.3B", "Backlog", "May 2026")
        .with_section("Projects", "Alpha rocket", style="bullets",
                      items=["Alpha", "MLV", "Blue Ghost"])
    )
    out = render_card_plain(c)
    assert "Firefly" in out
    assert "A space company." in out
    assert "$1.3B" in out
    assert "Backlog" in out
    assert "Projects" in out
    assert "- Alpha" in out
    assert "Cached 5m ago" in out


def test_render_card_plain_field_style_renders_key_value_pairs() -> None:
    c = Card().with_section("Stats", style="fields",
                            items=[("Revenue", "$81M"),
                                   ("Outlook", "$420M-$450M")])
    out = render_card_plain(c)
    assert "Revenue: $81M" in out
    assert "Outlook: $420M" in out


def test_response_text_is_the_fallback_channel_render() -> None:
    r = ArchResponse(text="ok")
    assert r.text == "ok"
    assert r.card is None
    # A response can carry follow-ups without setting text on the parent.
    r.followups.append(ArchResponse(text="later"))
    assert r.followups[0].text == "later"


def test_card_accent_drives_renderer_choice() -> None:
    # Accent strings are channel hints, not enforced enums -- the renderer
    # maps known values and falls back for unknowns. We just spot-check the
    # default is blank so the colour-table lookup returns the neutral
    # accent.
    assert Card().accent == ""
    assert Card(accent="ok").accent == "ok"
