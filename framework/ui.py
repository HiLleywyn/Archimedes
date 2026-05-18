"""framework/ui.py -- colour palette, formatting helpers and paginators.

All embed colours come from the constants here; never use raw hex literals
in cogs. Time values are always rendered through ``fmt_ts`` so epoch floats
and datetimes format identically.
"""
from __future__ import annotations

import datetime as _dt

import discord

# ── Colour palette ────────────────────────────────────────────────────────────
C_SUCCESS = 0x2ECC71   # green   -- confirmations
C_ERROR = 0xE74C3C     # red     -- errors
C_WARNING = 0xE67E22   # orange  -- warnings
C_INFO = 0x3498DB      # blue    -- informational
C_GOLD = 0xF1C40F      # yellow  -- highlights
C_PURPLE = 0x9B59B6    # purple  -- profiles / AI
C_TEAL = 0x1ABC9C      # teal
C_NAVY = 0x2C3E50      # dark blue -- panels
C_AMBER = 0xF39C12     # amber
C_PINK = 0xE91E63      # pink
C_NEUTRAL = 0x95A5A6   # gray
C_BLURPLE = 0x5865F2   # discord brand -- admin / dev


# ── Formatting helpers ────────────────────────────────────────────────────────
def fmt_ts(ts, fmt: str = "%m/%d %H:%M") -> str:
    """Format a timestamp. Accepts an epoch float/int or a datetime.

    DB timestamps come back as epoch floats; this handles both so callers
    never have to branch.
    """
    if ts is None:
        return "never"
    try:
        if isinstance(ts, (int, float)):
            dt = _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc)
        elif isinstance(ts, _dt.datetime):
            dt = ts
        else:
            return str(ts)
        return dt.strftime(fmt)
    except (ValueError, OSError, OverflowError):
        return str(ts)


def fmt_pct(pct: float) -> str:
    """Format a percentage with an explicit sign: ``+4.50%`` / ``-3.25%``."""
    try:
        return f"{float(pct):+.2f}%"
    except (TypeError, ValueError):
        return "0.00%"


def time_ago(ts) -> str:
    """Human relative time: ``3m ago``, ``2h ago``, ``5d ago``."""
    if ts is None:
        return "never"
    try:
        if isinstance(ts, _dt.datetime):
            secs = (_dt.datetime.now(_dt.timezone.utc) - ts).total_seconds()
        else:
            secs = _dt.datetime.now(_dt.timezone.utc).timestamp() - float(ts)
    except (TypeError, ValueError):
        return "unknown"
    secs = max(0, int(secs))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def clip(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters with an ellipsis."""
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


# ── Paginators ────────────────────────────────────────────────────────────────
class Paginator(discord.ui.View):
    """Simple prev/next paginator over a list of embeds."""

    def __init__(self, pages: list[discord.Embed], *, author_id: int | None = None,
                 timeout: float = 120.0) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages or [discord.Embed(description="(empty)")]
        self.author_id = author_id
        self.index = 0
        self._sync()

    def _sync(self) -> None:
        single = len(self.pages) <= 1
        self.prev_btn.disabled = single or self.index == 0
        self.next_btn.disabled = single or self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This pager isn't yours.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _b: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _b: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    async def send(self, ctx) -> None:
        view = self if len(self.pages) > 1 else None
        await ctx.reply(embed=self.pages[self.index], view=view, mention_author=False)


class _CategorySelect(discord.ui.Select):
    def __init__(self, parent: "CategoryPaginator") -> None:
        self.parent = parent
        options = [
            discord.SelectOption(label=clip(label, 100), value=str(i))
            for i, label in enumerate(parent.labels[:25])
        ]
        super().__init__(placeholder="Pick a section", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent.cat_index = int(self.values[0])
        self.parent.page_index = 0
        self.parent._sync()
        await interaction.response.edit_message(
            embed=self.parent._current(), view=self.parent,
        )


class CategoryPaginator(discord.ui.View):
    """A dropdown of categories, each with its own list of embed pages."""

    def __init__(self, categories: dict[str, list[discord.Embed]], *,
                 author_id: int | None = None, timeout: float = 180.0) -> None:
        super().__init__(timeout=timeout)
        self.categories = categories or {"(empty)": [discord.Embed(description="(empty)")]}
        self.labels = list(self.categories.keys())
        self.author_id = author_id
        self.cat_index = 0
        self.page_index = 0
        if len(self.labels) > 1:
            self.add_item(_CategorySelect(self))
        self._sync()

    def _pages(self) -> list[discord.Embed]:
        pages = self.categories[self.labels[self.cat_index]]
        return pages or [discord.Embed(description="(empty)")]

    def _current(self) -> discord.Embed:
        return self._pages()[self.page_index]

    def _sync(self) -> None:
        pages = self._pages()
        single = len(pages) <= 1
        self.prev_btn.disabled = single or self.page_index == 0
        self.next_btn.disabled = single or self.page_index >= len(pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.author_id is not None and interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This menu isn't yours.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Prev", style=discord.ButtonStyle.secondary, row=1)
    async def prev_btn(self, interaction: discord.Interaction, _b: discord.ui.Button):
        self.page_index = max(0, self.page_index - 1)
        self._sync()
        await interaction.response.edit_message(embed=self._current(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_btn(self, interaction: discord.Interaction, _b: discord.ui.Button):
        self.page_index = min(len(self._pages()) - 1, self.page_index + 1)
        self._sync()
        await interaction.response.edit_message(embed=self._current(), view=self)

    @classmethod
    async def send(cls, ctx, categories: dict[str, list[discord.Embed]]) -> None:
        author_id = getattr(getattr(ctx, "author", None), "id", None)
        view = cls(categories, author_id=author_id)
        needs_view = len(view.labels) > 1 or len(view._pages()) > 1
        await ctx.reply(
            embed=view._current(),
            view=view if needs_view else None,
            mention_author=False,
        )
