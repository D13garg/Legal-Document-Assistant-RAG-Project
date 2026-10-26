"""Deterministic text normalization. The same input always yields the same output."""

from __future__ import annotations

import re
import unicodedata

_HORIZONTAL = re.compile(r"[^\S\n]+")  # any whitespace except the newline
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_SEPARATOR_CATEGORIES = frozenset({"Zs", "Zl", "Zp"})


def normalize_text(raw: str) -> str:
    """NFC; one `\\n` for every line break; control, format (zero-width, BOM, bidi), surrogate and
    unassigned code points removed; every other space, tab or line/paragraph separator becomes one
    space; lines trimmed and runs of blank lines collapsed to one. A paragraph break is the single
    blank line that remains."""
    text = unicodedata.normalize("NFC", raw).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_HORIZONTAL.sub(" ", _clean_line(line)).strip() for line in text.split("\n")]
    collapsed: list[str] = []
    for line in lines:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    while collapsed and not collapsed[-1]:
        collapsed.pop()
    return "\n".join(collapsed)


def _clean_line(line: str) -> str:
    if line.isascii() and line.isprintable():
        return line  # fast path: no control, format or separator code point can be present
    kept: list[str] = []
    for char in line:
        category = unicodedata.category(char)
        if category in _SEPARATOR_CATEGORIES or char in "\t\x0b\x0c\x85":
            kept.append(" ")
        elif category not in _DROPPED_CATEGORIES:
            kept.append(char)
    return "".join(kept)


def paragraphs(raw: str) -> tuple[str, ...]:
    """The normalized paragraphs of `raw`: blocks separated by a blank line, lines joined by one
    space (a layout line break is not a paragraph break)."""
    normalized = normalize_text(raw)
    if not normalized:
        return ()
    return tuple(" ".join(block.split("\n")) for block in normalized.split("\n\n"))


def single_line(raw: str) -> str:
    """The normalized text of `raw` as one line (every line break becomes one space)."""
    return " ".join(line for line in normalize_text(raw).split("\n") if line)
