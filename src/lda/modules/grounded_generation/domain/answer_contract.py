"""The answer contract (ADR-029): what a provider reply must be to be shown at all.

The reply is untrusted text. It is accepted only if it is exactly one strict JSON object::

    {"outcome": "answered",
     "statements": [{"text": "...", "citation": "S1", "quote": "..."}]}
    {"outcome": "insufficient_evidence", "statements": []}

Rules, all of which must hold (any violation rejects the WHOLE reply; nothing is repaired,
trimmed or salvaged):

- at most `max_output_characters`; strict JSON (no duplicate keys, no NaN/Infinity, no trailing
  data, no code fence); the top level has exactly `outcome` and `statements`;
- `answered` has 1..`max_statements` statements; `insufficient_evidence` has none;
- a statement has exactly `text`, `citation` and `quote`, all strings: `text` is one printable line
  of at most `max_statement_characters` characters; `citation` is ONE marker (`S1`, `S2`, ...);
  `quote` is a span of `min_quote_characters`..`max_quote_characters` characters;
- no text carries a control, format, surrogate, private-use or unassigned character, and none
  carries the structural prompt marker.

This module checks shape and character safety only. Whether the marker names supplied evidence and
whether the quote really occurs in that evidence is checked against the evidence by the application
(`verify_statements`). Passing every check proves the cited source exists and contains the quoted
words; it does NOT prove the statement is entailed by them (ADR-029 limitations).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field

from lda.modules.grounded_generation.domain import codes
from lda.shared.kernel.errors import DomainError, ValidationError

OUTCOME_ANSWERED = "answered"
OUTCOME_INSUFFICIENT = "insufficient_evidence"
STRUCTURAL_MARKER = "[[lda:"
"""Lower-case prefix of every structural prompt marker (equal to prompts' `MARKER`, case-folded;
a test keeps them equal). Model output carrying it could forge a trust boundary downstream."""

_MARKER = re.compile(r"S[1-9][0-9]{0,3}")
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})
_TOP_KEYS = frozenset({"outcome", "statements"})
_STATEMENT_KEYS = frozenset({"text", "citation", "quote"})


class ContractViolation(DomainError):
    """The provider reply broke the answer contract. `code` is one of `REJECTION_CODES`."""


@dataclass(frozen=True, slots=True)
class ContractLimits:
    max_output_characters: int = 24_000
    max_statements: int = 20
    max_statement_characters: int = 1_500
    min_quote_characters: int = 12
    max_quote_characters: int = 400

    def __post_init__(self) -> None:
        values = (
            self.max_output_characters,
            self.max_statements,
            self.max_statement_characters,
            self.min_quote_characters,
            self.max_quote_characters,
        )
        if not all(type(v) is int and v >= 1 for v in values):
            raise ValidationError(codes.INVALID_INPUT, detail="contract_limits")
        if self.min_quote_characters > self.max_quote_characters:
            raise ValidationError(codes.INVALID_INPUT, detail="contract_limits")


@dataclass(frozen=True, slots=True)
class ParsedStatement:
    text: str = field(repr=False)
    citation: str
    quote: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ParsedAnswer:
    outcome: str
    statements: tuple[ParsedStatement, ...] = field(repr=False)


def normalize_span(text: str) -> str:
    """Whitespace-insensitive form used to match a quote against a passage: every run of Unicode
    whitespace becomes one space and the ends are trimmed. Case and every other character count."""
    return " ".join(text.split())


def parse_answer(raw: str, limits: ContractLimits) -> ParsedAnswer:
    if type(raw) is not str:
        raise ContractViolation(codes.OUTPUT_MALFORMED)
    if len(raw) > limits.max_output_characters:
        raise ContractViolation(codes.OUTPUT_TOO_LARGE)
    document = _load(raw)
    if type(document) is not dict or set(document) != _TOP_KEYS:
        raise ContractViolation(codes.OUTPUT_SCHEMA)
    outcome, statements = document["outcome"], document["statements"]
    if outcome not in (OUTCOME_ANSWERED, OUTCOME_INSUFFICIENT) or type(statements) is not list:
        raise ContractViolation(codes.OUTPUT_SCHEMA)
    if outcome == OUTCOME_INSUFFICIENT:
        if statements:
            raise ContractViolation(codes.OUTPUT_SCHEMA)
        return ParsedAnswer(OUTCOME_INSUFFICIENT, ())
    if not 1 <= len(statements) <= limits.max_statements:
        raise ContractViolation(codes.OUTPUT_SCHEMA)
    return ParsedAnswer(OUTCOME_ANSWERED, tuple(_statement(s, limits) for s in statements))


def _load(raw: str) -> object:
    def no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        keys = [k for k, _ in pairs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate key")
        return dict(pairs)

    def no_constants(_: str) -> object:
        raise ValueError("non-finite number")

    try:
        return json.loads(raw, object_pairs_hook=no_duplicates, parse_constant=no_constants)
    except (ValueError, RecursionError):
        raise ContractViolation(codes.OUTPUT_MALFORMED) from None


def _statement(value: object, limits: ContractLimits) -> ParsedStatement:
    if type(value) is not dict or set(value) != _STATEMENT_KEYS:
        raise ContractViolation(codes.OUTPUT_SCHEMA)
    text, citation, quote = value["text"], value["citation"], value["quote"]
    if not (type(text) is str and type(citation) is str and type(quote) is str):
        raise ContractViolation(codes.OUTPUT_SCHEMA)
    if _MARKER.fullmatch(citation) is None:
        raise ContractViolation(codes.OUTPUT_CITATION_INVALID)
    if not text.strip() or len(text) > limits.max_statement_characters:
        raise ContractViolation(codes.OUTPUT_SCHEMA)
    span = normalize_span(quote)
    if not limits.min_quote_characters <= len(span) <= limits.max_quote_characters:
        raise ContractViolation(codes.OUTPUT_SCHEMA)
    if _unsafe(text) or _unsafe(quote) or any(c in text for c in "\n\r\t"):
        raise ContractViolation(codes.OUTPUT_UNSAFE_TEXT)
    return ParsedStatement(text, citation, quote)


def _unsafe(text: str) -> bool:
    if STRUCTURAL_MARKER in text.lower():
        return True
    return any(
        unicodedata.category(ch) in _FORBIDDEN_CATEGORIES and ch not in "\n\r\t" for ch in text
    )
