"""Candidates, limits, settings and the shape of an answered turn."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError

from lda.modules.answering.api import (
    INVALID_CANDIDATES,
    INVALID_COMMAND,
    LIMITS_INVALID,
    AnsweredTurn,
    AnswerLimits,
    AnswerOutcome,
)
from lda.modules.answering.config import AnsweringSettings
from lda.modules.answering.domain.candidates import CandidateDocuments
from lda.modules.answering.tests.support import CONFIG_ID, assistant_view, user_view
from lda.modules.conversations.api import ClientMessageId, ConversationId
from lda.shared.kernel.errors import ValidationError


def test_a_valid_candidate_set_is_kept_in_order() -> None:
    ids = tuple(uuid4() for _ in range(3))
    assert CandidateDocuments.within(ids, 3).ids == ids


@pytest.mark.parametrize(
    "make",
    [
        lambda: (),
        lambda: [uuid4()],
        lambda: frozenset({uuid4()}),
        lambda: None,
        lambda: (str(uuid4()),),
        lambda: (uuid4(), 5),
        lambda: tuple(uuid4() for _ in range(4)),  # more than the maximum of 3
    ],
)
def test_an_invalid_candidate_set_is_refused(make: object) -> None:
    with pytest.raises(ValidationError) as caught:
        CandidateDocuments.within(make(), 3)  # type: ignore[operator]
    assert caught.value.code == INVALID_CANDIDATES


def test_duplicates_are_refused_not_silently_merged() -> None:
    same = uuid4()
    with pytest.raises(ValidationError) as caught:
        CandidateDocuments.within((same, uuid4(), same), 3)
    assert caught.value.code == INVALID_CANDIDATES


def test_the_error_names_no_identifier() -> None:
    same = uuid4()
    with pytest.raises(ValidationError) as caught:
        CandidateDocuments.within((same, same), 3)
    assert str(same) not in str(caught.value) + caught.value.detail


@pytest.mark.parametrize(
    "kwargs",
    [
        {"config_id": "rag-" + "a" * 63},
        {"config_id": "RAG-" + "a" * 64},
        {"config_id": "rag-" + "A" * 64},
        {"config_id": ""},
        {"max_candidates": 0},
        {"max_candidates": 10_001},
        {"max_question_characters": 0},
        {"max_question_characters": 20_001},
        {"max_sources": 0},
        {"max_sources": 101},
    ],
)
def test_invalid_limits_are_refused(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {
        "config_id": CONFIG_ID,
        "max_candidates": 1,
        "max_question_characters": 1,
        "max_sources": 1,
    }
    with pytest.raises(ValidationError) as caught:
        AnswerLimits(**{**base, **kwargs})  # type: ignore[arg-type]
    assert caught.value.code == LIMITS_INVALID


def test_the_limit_ceilings_are_accepted() -> None:
    assert AnswerLimits(CONFIG_ID, 10_000, 20_000, 100).max_sources == 100


def test_settings_config_id_is_optional_and_its_shape_is_validated() -> None:
    """Step 17 (ADR-041 D2): optional assertion, validated for shape; the computed identity is
    what is recorded (bootstrap tests prove the binding)."""
    assert AnsweringSettings().config_id is None
    with pytest.raises(PydanticValidationError):
        AnsweringSettings(config_id="rag-nothex")
    settings = AnsweringSettings(config_id=CONFIG_ID)
    assert (settings.max_candidates, settings.max_question_characters, settings.max_sources) == (
        100,
        2_000,
        8,
    )


def test_settings_reject_unknown_fields_and_out_of_range_values() -> None:
    with pytest.raises(PydanticValidationError):
        AnsweringSettings(config_id=CONFIG_ID, surprise=1)  # type: ignore[call-arg]
    for field, bad in (("max_candidates", 10_001), ("max_sources", 101), ("max_sources", 0)):
        with pytest.raises(PydanticValidationError):
            AnsweringSettings(config_id=CONFIG_ID, **{field: bad})


def test_a_turn_without_evidence_has_no_record_and_no_assistant_message() -> None:
    conversation, client = ConversationId(uuid4()), ClientMessageId(uuid4())
    user = user_view(conversation, client)
    turn = AnsweredTurn(AnswerOutcome.INSUFFICIENT_EVIDENCE, (), user)
    assert turn.record_id is None and turn.assistant_message is None
    record = uuid4()
    linked = AnsweredTurn(
        AnswerOutcome.INSUFFICIENT_EVIDENCE, (), user, record, assistant_view(conversation, record)
    )
    assert linked.assistant_message is not None


@pytest.mark.parametrize("outcome", [AnswerOutcome.ANSWERED, AnswerOutcome.REJECTED])
def test_an_answer_or_rejection_without_a_record_is_not_a_valid_turn(
    outcome: AnswerOutcome,
) -> None:
    conversation, client = ConversationId(uuid4()), ClientMessageId(uuid4())
    with pytest.raises(ValidationError) as caught:
        AnsweredTurn(outcome, (), user_view(conversation, client), None, None, "x")
    assert caught.value.code == INVALID_COMMAND


def test_a_turn_whose_message_names_another_record_is_refused() -> None:
    conversation, client = ConversationId(uuid4()), ClientMessageId(uuid4())
    with pytest.raises(ValidationError):
        AnsweredTurn(
            AnswerOutcome.INSUFFICIENT_EVIDENCE,
            (),
            user_view(conversation, client),
            uuid4(),
            assistant_view(conversation, uuid4()),
        )


def test_a_record_without_a_linked_message_is_refused() -> None:
    conversation, client = ConversationId(uuid4()), ClientMessageId(uuid4())
    with pytest.raises(ValidationError):
        AnsweredTurn(
            AnswerOutcome.INSUFFICIENT_EVIDENCE, (), user_view(conversation, client), uuid4()
        )


def test_a_rejection_code_requires_the_rejected_outcome() -> None:
    conversation, client = ConversationId(uuid4()), ClientMessageId(uuid4())
    record = uuid4()
    with pytest.raises(ValidationError):
        AnsweredTurn(
            AnswerOutcome.INSUFFICIENT_EVIDENCE,
            (),
            user_view(conversation, client),
            record,
            assistant_view(conversation, record),
            "grounded_output_schema",
        )
