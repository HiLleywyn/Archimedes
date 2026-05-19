"""ai/safety.py -- prompt-injection detection and input/output sanitization.

Three layers protect the chat pipeline:

  * ``is_injection_attempt`` -- rejects a user message before it ever
    reaches the model (classic "ignore previous instructions" plus
    formatting smuggles like acrostics).
  * ``sanitize_input`` / ``sanitize_context_snippet`` -- scrub anything
    untrusted that goes INTO a prompt (mentions, links, slurs).
  * ``sanitize_output`` / ``looks_like_acrostic`` -- scrub what comes OUT
    of the model before it is posted to a channel.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ai.emoji_safety import repair_custom_emojis

if TYPE_CHECKING:
    import discord


# ── Injection detection ───────────────────────────────────────────────────────
_INJECTION_PATTERNS = re.compile(
    r"ignore\s+(previous|all|prior|above|your)\s+instructions?"
    r"|you\s+are\s+now\s+\w"
    r"|pretend\s+(you\s+are|to\s+be)"
    r"|act\s+as\s+(?!a\s+game|an?\s+advisor)"
    r"|new\s+instructions?\s*:"
    r"|system\s+prompt"
    r"|override\s+(?:your|all|previous)"
    r"|jailbreak"
    r"|DAN\s*mode"
    r"|do\s+anything\s+now"
    r"|\[INST\]"
    r"|<\|im_start\|>"
    r"|<system>",
    re.IGNORECASE,
)

# Acrostic / formatting-exploit attempts: smuggle a payload through a
# harmless-looking transformation (first letter of each word, one letter per
# line, vertical output). The output-side guard catches it again if the
# model follows the instruction anyway.
_FORMATTING_EXPLOIT_PATTERNS = re.compile(
    r"first\s+letter\s+of\s+(each|every)\s+(word|line|item)"
    r"|only\s+keep\s+the\s+first\s+letter"
    r"|(write|output|print|reply|respond|type)\s+(this|it|the\s+\w+)?\s*vertical(ly)?"
    r"|one\s+letter\s+per\s+line"
    r"|acrostic"
    r"|spell(ing)?\s+(out\s+)?(with|using)\s+(the\s+)?first\s+letters?"
    r"|take\s+(the\s+)?first\s+(letter|char(acter)?s?)"
    r"|(append|write|output|type)\s+(a\s+)?(new\s+)?line\s+(and\s+)?(write\s+)?<@",
    re.IGNORECASE,
)


def is_injection_attempt(text: str) -> bool:
    """Return True if the user message looks like a prompt-injection attempt."""
    if not text:
        return False
    return bool(
        _INJECTION_PATTERNS.search(text)
        or _FORMATTING_EXPLOIT_PATTERNS.search(text)
    )


# ── Acrostic output guard ─────────────────────────────────────────────────────
_ACROSTIC_RUN_RE = re.compile(r"(?:^\s*[A-Za-z]\s*$\n+){4,}", re.MULTILINE)


def looks_like_acrostic(text: str) -> bool:
    """Return True if the text contains a run of 4+ single-letter lines."""
    return bool(_ACROSTIC_RUN_RE.search(text or ""))


# ── URL / mention / slur scrubbing ────────────────────────────────────────────
_URL_TLDS = (
    r"com|net|org|io|gg|xyz|app|dev|co|me|ai|link|site|tv|info|tech|cloud"
    r"|store|shop|blog|cc|so|onion"
)
_URL_RE = re.compile(
    r"https?://\S+"
    r"|ftp://\S+"
    r"|discord\.gg/\S+"
    r"|discord(?:app)?\.com/invite/\S+"
    r"|www\.\S+\.\S+"
    rf"|\b(?:[a-z0-9][a-z0-9-]*\.)+(?:{_URL_TLDS})(?:/\S*)?",
    re.IGNORECASE,
)
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_OUTPUT_MENTION_RE = re.compile(r"@(everyone|here)|<@!?\d+>|<@&\d+>", re.IGNORECASE)
_HSPACE_RE = re.compile(r"[^\S\n]+")
_MULTI_NL_RE = re.compile(r"\n{3,}")
_PARTIAL_EMOJI_TAIL_RE = re.compile(r"\s*<a?:[A-Za-z0-9_]{1,32}:\d{0,20}\s*$")

# Racial slur filter: the n-word plus common leetspeak obfuscations, while
# avoiding false positives on "Niger", "niggard", "snigger".
_SLUR_PATTERNS = [re.compile(r"\bn[i1l!|][gq96]{2,}[aerh]+[sz]?\b", re.IGNORECASE)]


def strip_links(text: str) -> str:
    """Remove every URL / invite link from text."""
    return _URL_RE.sub("", text or "").strip()


def _strip_partial_emoji_tail(text: str) -> str:
    prev = None
    while prev != text:
        prev = text
        text = _PARTIAL_EMOJI_TAIL_RE.sub("", text)
    return text


def _apply_slur_filter(text: str) -> str:
    for pattern in _SLUR_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text


def sanitize_output(text: str, guild: "discord.Guild | None" = None) -> str:
    """Scrub AI output before it is posted.

    Strips links, image/link markdown, role/user/everyone pings and slurs.
    Channel mentions (``<#id>``) are kept so the model can link a channel.
    Markdown structure (lists, code blocks, line breaks) is preserved.
    """
    if not text:
        return ""
    text = _MD_IMAGE_RE.sub("", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("", text)
    text = _OUTPUT_MENTION_RE.sub("[redacted]", text)
    text = _apply_slur_filter(text)
    text = _HSPACE_RE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    text = _strip_partial_emoji_tail(text)
    text = repair_custom_emojis(text, guild)
    return text.strip()


def sanitize_input(text: str, *, keep_urls: bool = False) -> str:
    """Scrub user input: neutralise pings, links and slurs, then truncate.

    With ``keep_urls`` the URLs the user typed are preserved. The chat
    pipeline needs this for the question itself: a user may ask the bot to
    act on a link with a tool (web fetch, GitHub lookups), and the model
    cannot do that if the URL has been stripped before it arrives. The
    model's *output* is still scrubbed of links by :func:`sanitize_output`,
    so a preserved input URL can never be echoed back into a channel.
    """
    if not text:
        return ""
    if not keep_urls:
        text = _MD_IMAGE_RE.sub("", text)
        text = _MD_LINK_RE.sub(r"\1", text)
        text = _URL_RE.sub("", text)
    text = re.sub(r"<@!?\d+>", "@user", text)
    text = re.sub(r"<@&\d+>", "@role", text)
    text = re.sub(r"<#\d+>", "#channel", text)
    text = re.sub(r"@everyone\b", "@channel", text, flags=re.IGNORECASE)
    text = re.sub(r"@here\b", "@channel", text, flags=re.IGNORECASE)
    text = _apply_slur_filter(text)
    text = _HSPACE_RE.sub(" ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = _MULTI_NL_RE.sub("\n\n", text)
    return text.strip()[:800]


def sanitize_context_snippet(text: str, limit: int = 240) -> str:
    """Scrub untrusted context text before it goes into a prompt.

    Stricter than ``sanitize_input``: also strips markdown noise and
    collapses all whitespace so ambient chatter cannot dominate the prompt.
    """
    text = sanitize_input(text or "")
    text = re.sub(r"[`*_>#\[\]{}|]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()[:limit]
