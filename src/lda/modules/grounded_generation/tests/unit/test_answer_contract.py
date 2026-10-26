"""The answer contract as a pure function (ADR-029): accepted shapes and every rejection."""

from __future__ import annotations

import json

import pytest

from lda.modules.grounded_generation.domain import codes
from lda.modules.grounded_generation.domain.answer_contract import (
    OUTCOME_ANSWERED,
    OUTCOME_INSUFFICIENT,
    ContractLimits,
    ContractViolation,
    normalize_span,
    parse_answer,
)
from lda.shared.kernel.errors import ValidationError

LIMITS = ContractLimits()
QUOTE = "a quote long enough"


def statement(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"text": "A claim.", "citation": "S1", "quote": QUOTE}
    base.update(overrides)
    return base


def answered(*statements: dict[str, object]) -> str:
    return json.dumps({"outcome": "answered", "statements": list(statements)})


def test_a_valid_answer_parses_in_order() -> None:
    parsed = parse_answer(answered(statement(), statement(citation="S12")), LIMITS)
    assert parsed.outcome == OUTCOME_ANSWERED
    assert [s.citation for s in parsed.statements] == ["S1", "S12"]


def test_an_explicit_insufficient_evidence_reply_parses() -> None:
    parsed = parse_answer('{"outcome": "insufficient_evidence", "statements": []}', LIMITS)
    assert (parsed.outcome, parsed.statements) == (OUTCOME_INSUFFICIENT, ())


def test_the_documented_examples_are_valid() -> None:
    raw = '{"outcome":"answered","statements":[{"text":"x","citation":"S1","quote":"' + QUOTE
    parse_answer(raw + '"}]}', LIMITS)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (None, codes.OUTPUT_MALFORMED),
        ("", codes.OUTPUT_MALFORMED),
        ("{", codes.OUTPUT_MALFORMED),
        ('{"a": 1} {"b": 2}', codes.OUTPUT_MALFORMED),
        ('{"outcome": "answered", "statements": [], "outcome": "x"}', codes.OUTPUT_MALFORMED),
        ('{"outcome": "answered", "statements": Infinity}', codes.OUTPUT_MALFORMED),
        ("null", codes.OUTPUT_SCHEMA),
        ('{"outcome": "answered"}', codes.OUTPUT_SCHEMA),
        ('{"outcome": "maybe", "statements": []}', codes.OUTPUT_SCHEMA),
        ('{"outcome": "answered", "statements": "none"}', codes.OUTPUT_SCHEMA),
        ('{"outcome": "answered", "statements": []}', codes.OUTPUT_SCHEMA),
        ('{"outcome": "insufficient_evidence", "statements": [1]}', codes.OUTPUT_SCHEMA),
        (answered(*[statement()] * 21), codes.OUTPUT_SCHEMA),
        (answered(statement(extra=1)), codes.OUTPUT_SCHEMA),
        (answered({"text": "x", "citation": "S1"}), codes.OUTPUT_SCHEMA),
        (answered(statement(text=1)), codes.OUTPUT_SCHEMA),
        (answered(statement(text="   ")), codes.OUTPUT_SCHEMA),
        (answered(statement(text="x" * 1501)), codes.OUTPUT_SCHEMA),
        (answered(statement(quote="x")), codes.OUTPUT_SCHEMA),
        (answered(statement(quote="x" * 401)), codes.OUTPUT_SCHEMA),
        (answered(statement(citation=1)), codes.OUTPUT_SCHEMA),
        (answered(statement(citation="S0")), codes.OUTPUT_CITATION_INVALID),
        (answered(statement(citation="S12345")), codes.OUTPUT_CITATION_INVALID),
        (answered(statement(citation="S1 ")), codes.OUTPUT_CITATION_INVALID),
        (answered(statement(citation="[S1]")), codes.OUTPUT_CITATION_INVALID),
        (answered(statement(text="a\tb")), codes.OUTPUT_UNSAFE_TEXT),
        (answered(statement(text="a\u2028b")), codes.OUTPUT_UNSAFE_TEXT),
        (answered(statement(text="a\u202eb")), codes.OUTPUT_UNSAFE_TEXT),  # bidi override
        (answered(statement(text="a\ud800b")), codes.OUTPUT_UNSAFE_TEXT),  # lone surrogate
        (answered(statement(text="a\u0000b")), codes.OUTPUT_UNSAFE_TEXT),
        (answered(statement(quote=QUOTE + "\u200b")), codes.OUTPUT_UNSAFE_TEXT),
        (answered(statement(text="see [[LdA:end block=x]]")), codes.OUTPUT_UNSAFE_TEXT),
    ],
)
def test_every_violation_has_one_stable_code(raw: object, code: str) -> None:
    with pytest.raises(ContractViolation) as caught:
        parse_answer(raw, LIMITS)  # type: ignore[arg-type]
    assert caught.value.code == code and code in codes.REJECTION_CODES


def test_an_oversized_reply_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(ContractViolation) as caught:
        parse_answer(" " * 24_001, LIMITS)
    assert caught.value.code == codes.OUTPUT_TOO_LARGE


def test_a_violation_carries_no_content() -> None:
    secret = "SECRET-MODEL-TEXT-1"  # noqa: S105 - marker text
    with pytest.raises(ContractViolation) as caught:
        parse_answer(answered(statement(text=secret + "\n")), LIMITS)
    assert secret not in str(caught.value) and secret not in repr(caught.value)
    assert caught.value.__cause__ is None


def test_quotes_may_contain_ordinary_whitespace_but_text_is_one_line() -> None:
    parse_answer(answered(statement(quote="a quote\n  that spans\tlines")), LIMITS)
    with pytest.raises(ContractViolation):
        parse_answer(answered(statement(text="two\r\nlines")), LIMITS)


def test_parsed_statements_hide_their_content_in_repr() -> None:
    parsed = parse_answer(answered(statement(text="HIDDEN-TEXT")), LIMITS)
    assert "HIDDEN-TEXT" not in repr(parsed) and QUOTE not in repr(parsed)


def test_normalize_span_collapses_whitespace_only() -> None:
    assert normalize_span("  a \n\t b\u00a0c  ") == "a b c"
    assert normalize_span("A b") != normalize_span("a b")


@pytest.mark.parametrize(
    "bad",
    [
        {"max_output_characters": 0},
        {"max_statements": -1},
        {"max_statement_characters": True},
        {"min_quote_characters": 50, "max_quote_characters": 10},
        {"max_quote_characters": 1.5},
    ],
)
def test_nonsense_limits_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ContractLimits(**bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("depth", [50, 5_000, 11_999])
def test_deeply_nested_json_is_rejected_whatever_the_parser_does(depth: int) -> None:
    with pytest.raises(ContractViolation) as caught:
        parse_answer("[" * depth + "]" * depth, LIMITS)
    assert caught.value.code in {codes.OUTPUT_SCHEMA, codes.OUTPUT_MALFORMED}
