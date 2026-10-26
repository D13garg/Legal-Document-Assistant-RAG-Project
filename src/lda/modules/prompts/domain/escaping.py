"""Deterministic trust-boundary markers and the escaping of untrusted text (ADR-017).

Every structural marker the renderer emits starts with `MARKER`. Untrusted text (the user question
and document context) is escaped so that no occurrence of `MARKER` inside it can be mistaken for a
structural one: each occurrence, matched case-insensitively and preserved as written, is preceded
by an ODD number of backslashes. A run of `k` backslashes that already precedes an occurrence
becomes `2k + 1`. Nothing else is touched: no normalization, trimming, truncation or reordering.

The encoding is reversible (`unescape_untrusted`). It is a defense in depth for the text layout, not
a prompt-injection solution: a model may still follow instructions inside escaped text, and only
the exact marker (any letter case) is neutralized, not look-alikes such as other brackets.
"""

from __future__ import annotations

import re

MARKER = "[[LDA:"
USER_QUESTION_BLOCK = "user_question"
DOCUMENT_CONTEXT_BLOCK = "document_context"

_OCCURRENCE = re.compile(r"(\\*)(" + re.escape(MARKER) + ")", re.IGNORECASE)


def contains_marker(text: str) -> bool:
    return MARKER.lower() in text.lower()


def escape_untrusted(text: str) -> str:
    """Escape every marker occurrence. `escape_untrusted(t) == t` when `t` has none."""
    return _OCCURRENCE.sub(lambda m: "\\" * (2 * len(m.group(1)) + 1) + m.group(2), text)


def unescape_untrusted(escaped: str) -> str:
    """Exact inverse of `escape_untrusted`. Raises `ValueError` for text it could not have made."""

    def restore(match: re.Match[str]) -> str:
        run = len(match.group(1))
        if run % 2 == 0:
            raise ValueError("not an escaped text")
        return "\\" * ((run - 1) // 2) + match.group(2)

    return _OCCURRENCE.sub(restore, escaped)


def begin_marker(block: str, attributes: str = "") -> str:
    return f"{MARKER}BEGIN block={block}{attributes} trust=untrusted]]"


def end_marker(block: str, attributes: str = "") -> str:
    return f"{MARKER}END block={block}{attributes}]]"


def untrusted_block(block: str, text: str, attributes: str = "") -> str:
    """`BEGIN` line, the escaped text byte for byte, `END` line; lines are joined with `\\n`."""
    return "\n".join(
        (begin_marker(block, attributes), escape_untrusted(text), end_marker(block, attributes))
    )


def question_block(question: str) -> str:
    return untrusted_block(USER_QUESTION_BLOCK, question)


def context_block(index: int, count: int, text: str) -> str:
    """`index` is 1-based. Position and count are part of the block, so a part cannot be moved or
    dropped without the rendered text changing."""
    return untrusted_block(DOCUMENT_CONTEXT_BLOCK, text, f" part={index}/{count}")
