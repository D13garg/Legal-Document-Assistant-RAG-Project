"""D2: the quote must occur in the exact cited chunk (ADR-029). What it does and does not prove.

Refusing tests assert the whole rejection contract (outcome, code, no statements, a record without
content), not just the outcome. The `characterization` tests pin documented limits so nobody can
mistake them for guarantees: they are NOT claims of entailment or legal correctness.
"""

from __future__ import annotations

import pytest

from lda.modules.citations.api import GenerationOutcome
from lda.modules.grounded_generation.domain import codes
from lda.modules.grounded_generation.tests.support import PASSAGES, Rig, ctx, reply

NOTICE = "shall give ninety days written notice"  # in PASSAGES[0] only
FEES = "payable within thirty days of the invoice"  # in PASSAGES[1] only


def run(rig: Rig, raw: str):  # type: ignore[no-untyped-def]
    rig.provider.replies.append(raw)
    return rig.use_case.execute(ctx(), rig.command())


def assert_rejected_as(answer, rig: Rig, code: str) -> None:  # type: ignore[no-untyped-def]
    assert answer.outcome is GenerationOutcome.REJECTED and answer.statements == ()
    assert answer.rejection == code
    (record,) = rig.units.committed_records()
    assert record.outcome is GenerationOutcome.REJECTED and record.citations == ()


def test_a_quote_from_a_different_supplied_chunk_is_rejected() -> None:
    rig = Rig()
    answer = run(rig, reply(("Fees.", "S1", FEES)))  # S1 is the notice chunk; FEES is S2's text
    assert_rejected_as(answer, rig, codes.OUTPUT_QUOTE_UNSUPPORTED)


def test_the_same_quote_under_the_right_marker_is_accepted() -> None:
    rig = Rig()
    assert run(rig, reply(("Fees.", "S2", FEES))).outcome is GenerationOutcome.ANSWERED


def test_one_misattributed_statement_rejects_the_whole_answer() -> None:
    rig = Rig()
    answer = run(rig, reply(("Notice.", "S1", NOTICE), ("Fees.", "S1", FEES)))
    assert_rejected_as(answer, rig, codes.OUTPUT_QUOTE_UNSUPPORTED)


def test_the_comparison_is_case_sensitive() -> None:
    rig = Rig()
    answer = run(rig, reply(("Notice.", "S1", NOTICE.upper())))
    assert_rejected_as(answer, rig, codes.OUTPUT_QUOTE_UNSUPPORTED)


def test_whitespace_may_vary_in_amount_but_not_in_presence() -> None:
    rig = Rig()
    answer = run(rig, reply(("Notice.", "S1", "shall give ninetydays written notice")))
    assert_rejected_as(answer, rig, codes.OUTPUT_QUOTE_UNSUPPORTED)


@pytest.mark.parametrize("quote", ["what is the notice period", "SECRET-QUESTION-4402"])
def test_the_question_is_not_a_source(quote: str) -> None:
    rig = Rig()
    answer = run(rig, reply(("Echo.", "S1", quote + " abc")))
    assert_rejected_as(answer, rig, codes.OUTPUT_QUOTE_UNSUPPORTED)


def test_prompt_scaffolding_is_not_a_source() -> None:
    rig = Rig()
    answer = run(rig, reply(("Echo.", "S1", "[[lda:passage S1")))
    assert answer.outcome is GenerationOutcome.REJECTED  # structural marker or unsupported
    assert answer.rejection in {codes.OUTPUT_UNSAFE_TEXT, codes.OUTPUT_QUOTE_UNSUPPORTED}


def test_unicode_is_not_normalised_so_a_decomposed_quote_does_not_match_a_composed_source() -> None:
    rig = Rig(passages=("Le pr\u00e9avis est de quatre-vingt-dix jours.", PASSAGES[1]))
    answer = run(rig, reply(("Preavis.", "S1", "pre\u0301avis est de quatre-vingt-dix")))
    assert_rejected_as(answer, rig, codes.OUTPUT_QUOTE_UNSUPPORTED)  # fails closed, by design


# ---- characterization: documented limits, not guarantees ------------------------------------


def test_characterization_a_valid_quote_does_not_prove_the_claim() -> None:
    """The claim text is never checked against the quote. This answer is false in substance
    ("one day") yet released, because its quote exists in the cited chunk. Semantic support is
    outside Step 14 (ADR-029 limitations; later evaluation work)."""
    rig = Rig()
    answer = run(rig, reply(("The notice period is one day.", "S1", NOTICE)))
    assert answer.outcome is GenerationOutcome.ANSWERED
    assert answer.statements[0].text.reveal() == "The notice period is one day."


def test_characterization_a_quote_may_start_or_end_mid_word() -> None:
    """The match is a substring of the whitespace-normalised chunk, not a word-aligned span."""
    rig = Rig()
    answer = run(rig, reply(("Notice.", "S1", "hall give ninety days written noti")))
    assert answer.outcome is GenerationOutcome.ANSWERED
