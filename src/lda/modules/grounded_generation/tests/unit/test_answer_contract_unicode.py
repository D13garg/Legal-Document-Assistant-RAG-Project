"""D3 character rules and the contract's numeric boundaries, case by case (ADR-029).

The rule under test: statement text and quotes are refused if they contain a control (Cc), format
(Cf), surrogate (Cs), private-use (Co), unassigned (Cn) or line/paragraph-separator (Zl, Zp)
character; text is additionally one line, while a quote may contain \\n, \\r and \\t. Each case is
checked in the field it applies to, because the two fields have different rules, and the accepting
cases are as important as the refusing ones: over-rejection silently turns valid answers into
`REJECTED` ones.
"""

from __future__ import annotations

import json
import unicodedata

import pytest

from lda.modules.grounded_generation.domain import codes
from lda.modules.grounded_generation.domain.answer_contract import (
    ContractLimits,
    ContractViolation,
    parse_answer,
)

LIMITS = ContractLimits()
QUOTE = "a quote long enough"


def answered(text: str = "A claim.", citation: str = "S1", quote: str = QUOTE) -> str:
    statement = {"text": text, "citation": citation, "quote": quote}
    return json.dumps({"outcome": "answered", "statements": [statement]})


def refused_with(raw: str) -> str:
    with pytest.raises(ContractViolation) as caught:
        parse_answer(raw, LIMITS)
    return caught.value.code


FORBIDDEN = {
    "NUL (Cc)": "\x00",
    "unit separator (Cc)": "\x1f",
    "DEL (Cc)": "\x7f",
    "NEL (Cc)": "\x85",
    "vertical tab (Cc)": "\x0b",
    "form feed (Cc)": "\x0c",
    "zero width space (Cf)": "\u200b",
    "right-to-left override (Cf)": "\u202e",
    "BOM / zero width no-break space (Cf)": "\ufeff",
    "soft hyphen (Cf)": "\xad",
    "lone high surrogate (Cs)": "\ud800",
    "lone low surrogate (Cs)": "\udfff",
    "private use (Co)": "\ue000",
    "supplementary private use (Co)": "\U000f0000",
    "unassigned (Cn)": "\u0378",
    "noncharacter (Cn)": "\uffff",
    "line separator (Zl)": "\u2028",
    "paragraph separator (Zp)": "\u2029",
}


def test_the_table_really_holds_the_categories_it_claims() -> None:
    expected = {"Cc": 6, "Cf": 4, "Cs": 2, "Co": 2, "Cn": 2, "Zl": 1, "Zp": 1}
    seen: dict[str, int] = {}
    for char in FORBIDDEN.values():
        category = unicodedata.category(char)
        seen[category] = seen.get(category, 0) + 1
    assert seen == expected


@pytest.mark.parametrize("char", FORBIDDEN.values(), ids=FORBIDDEN.keys())
def test_a_forbidden_character_is_refused_in_statement_text(char: str) -> None:
    assert refused_with(answered(text=f"before{char}after")) == codes.OUTPUT_UNSAFE_TEXT


@pytest.mark.parametrize("char", FORBIDDEN.values(), ids=FORBIDDEN.keys())
def test_a_forbidden_character_is_refused_in_a_quote(char: str) -> None:
    assert refused_with(answered(quote=f"{QUOTE}{char}tail")) == codes.OUTPUT_UNSAFE_TEXT


@pytest.mark.parametrize("char", ["\n", "\r", "\t"])
def test_statement_text_is_one_line_whatever_the_whitespace(char: str) -> None:
    assert refused_with(answered(text=f"a{char}b")) == codes.OUTPUT_UNSAFE_TEXT


@pytest.mark.parametrize("char", ["\n", "\r", "\t", " ", "\u00a0", "\u3000"])
def test_a_quote_may_contain_ordinary_and_unicode_spaces(char: str) -> None:
    (statement,) = parse_answer(answered(quote=f"a quote{char}long enough"), LIMITS).statements
    assert statement.citation == "S1"


def test_escaped_forbidden_characters_are_caught_after_json_decoding() -> None:
    raw = '{"outcome":"answered","statements":[{"text":"a\\u200bb","citation":"S1","quote":"%s"}]}'
    assert refused_with(raw % QUOTE) == codes.OUTPUT_UNSAFE_TEXT


@pytest.mark.parametrize(
    "text",
    [
        "Die Kündigungsfrist beträgt neunzig Tage (§ 12).",  # Latin, umlauts, section sign
        "契約は九十日前の書面による通知で終了する。",  # CJK
        "Résumé: e\u0301 combining acute (Mn) is allowed",
        "Price: \u20ac1,000 \u2013 \u201cquoted\u201d \u2014 ok",
        "Emoji are symbols (So): \U0001f600",
        "Mixed\u00a0nbsp and\u3000ideographic space (Zs)",
    ],
)
def test_ordinary_international_text_is_not_over_rejected(text: str) -> None:
    (statement,) = parse_answer(answered(text=text, quote=text + " quote"), LIMITS).statements
    assert statement.text == text


def test_the_structural_marker_is_refused_in_any_case_in_text_and_quote() -> None:
    for variant in ("[[lda:", "[[LDA:", "[[LdA:"):
        assert refused_with(answered(text=f"x {variant}y")) == codes.OUTPUT_UNSAFE_TEXT
        assert refused_with(answered(quote=f"{QUOTE} {variant}y")) == codes.OUTPUT_UNSAFE_TEXT


# ---- numeric boundaries: exactly at the limit passes, one over fails ---------------------------


def test_statement_text_length_boundary() -> None:
    parse_answer(answered(text="x" * LIMITS.max_statement_characters), LIMITS)
    assert (
        refused_with(answered(text="x" * (LIMITS.max_statement_characters + 1)))
        == codes.OUTPUT_SCHEMA
    )


def test_quote_length_boundaries_count_after_whitespace_normalisation() -> None:
    parse_answer(answered(quote="q" * LIMITS.min_quote_characters), LIMITS)
    assert (
        refused_with(answered(quote="q" * (LIMITS.min_quote_characters - 1))) == codes.OUTPUT_SCHEMA
    )
    parse_answer(answered(quote="q" * LIMITS.max_quote_characters), LIMITS)
    assert (
        refused_with(answered(quote="q" * (LIMITS.max_quote_characters + 1))) == codes.OUTPUT_SCHEMA
    )
    padded = "   short   " + "\n\t" * 50  # long raw, 5 characters once normalised
    assert refused_with(answered(quote=padded)) == codes.OUTPUT_SCHEMA


def test_statement_count_boundary() -> None:
    def many(n: int) -> str:
        item = {"text": "t", "citation": "S1", "quote": QUOTE}
        return json.dumps({"outcome": "answered", "statements": [item] * n})

    assert (
        len(parse_answer(many(LIMITS.max_statements), LIMITS).statements) == LIMITS.max_statements
    )
    assert refused_with(many(LIMITS.max_statements + 1)) == codes.OUTPUT_SCHEMA


def test_output_size_boundary_is_checked_before_parsing() -> None:
    assert refused_with(" " * (LIMITS.max_output_characters + 1)) == codes.OUTPUT_TOO_LARGE
    # exactly at the limit it is parsed (and fails as malformed JSON, not as too large)
    assert refused_with(" " * LIMITS.max_output_characters) == codes.OUTPUT_MALFORMED


@pytest.mark.parametrize("marker", ["S1", "S9", "S10", "S99", "S1000", "S9999"])
def test_markers_in_range_parse(marker: str) -> None:
    (statement,) = parse_answer(answered(citation=marker), LIMITS).statements
    assert statement.citation == marker


@pytest.mark.parametrize(
    "marker", ["S0", "S01", "S00", "s1", "S", "S1.0", "S1,S2", "S10000", "\uff33\uff11", "S\u0661"]
)
def test_markers_out_of_form_are_refused(marker: str) -> None:
    assert refused_with(answered(citation=marker)) == codes.OUTPUT_CITATION_INVALID


# ---- structure: duplicate keys, unexpected shapes -----------------------------------------------


ITEM = '"text":"t","citation":"S1","quote":"a quote long enough"'


@pytest.mark.parametrize(
    "raw",
    [
        '{"outcome":"answered","outcome":"answered","statements":[]}',
        '{"outcome":"answered","\\u006futcome":"answered","statements":[]}',  # same key, escaped
        '{"outcome":"answered","statements":[{"text":"u",' + ITEM + "}]}",
        '{"outcome":"answered","statements":[{' + ITEM + ',"citation":"S2"}]}',
    ],
)
def test_duplicate_keys_are_refused_at_any_depth_even_when_escaped(raw: str) -> None:
    assert refused_with(raw) == codes.OUTPUT_MALFORMED


@pytest.mark.parametrize(
    "raw",
    [
        "```json\n" + answered() + "\n```",  # fenced
        "Here you go: " + answered(),  # preamble
        answered() + " thanks",  # trailing prose
        "\ufeff" + answered(),  # BOM
        "[" + answered() + "]",  # wrapped in an array
    ],
)
def test_anything_but_one_bare_json_object_is_refused(raw: str) -> None:
    assert refused_with(raw) in {codes.OUTPUT_MALFORMED, codes.OUTPUT_SCHEMA}


def test_the_control_case_parses() -> None:
    assert parse_answer(answered(), LIMITS).statements


def test_huge_numbers_and_constants_are_refused_not_crashing() -> None:
    assert refused_with('{"outcome":"answered","statements":' + "9" * 5_000 + "}") in {
        codes.OUTPUT_MALFORMED,
        codes.OUTPUT_SCHEMA,
    }
    assert refused_with('{"outcome":"answered","statements":NaN}') == codes.OUTPUT_MALFORMED
