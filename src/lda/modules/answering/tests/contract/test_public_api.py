"""The public surface and declarations of answering (ADR-040)."""

from __future__ import annotations

import inspect

from lda.modules.answering import api
from lda.modules.answering.domain import codes
from lda.shared.kernel.declarations import NotAudited, Requires, audit_of, authorization_of


def test_the_surface_is_exactly_the_documented_names() -> None:
    assert sorted(api.__all__) == sorted(
        [
            "INVALID_CANDIDATES",
            "INVALID_COMMAND",
            "INVALID_QUESTION",
            "LIMITS_INVALID",
            "NOT_AUTHORIZED",
            "REQUIRES_PRINCIPAL",
            "TURN_NOT_RECORDED",
            "Answer",
            "AnswerCommand",
            "AnswerGenerator",
            "AnswerLimits",
            "AnswerOutcome",
            "AnsweredTurn",
            "AssistantTurnWriter",
            "ChunkSearch",
            "ScopeResolver",
            "UserMessageWriter",
        ]
    )
    for name in api.__all__:
        assert hasattr(api, name)


def test_answer_declares_its_requirements_and_that_it_emits_no_fact_of_its_own() -> None:
    authorization = authorization_of(api.Answer)
    assert isinstance(authorization, Requires)
    assert authorization.requirements == ("answering.answer_question", "documents.read_document")
    assert isinstance(audit_of(api.Answer), NotAudited)


def test_answer_has_one_public_method() -> None:
    methods = [n for n, _ in inspect.getmembers(api.Answer, inspect.isfunction) if n[0] != "_"]
    assert methods == ["execute"]


def test_codes_are_unique_prefixed_and_content_free() -> None:
    values = [v for k, v in vars(codes).items() if k.isupper() and isinstance(v, str)]
    assert len(values) == len(set(values)) == 7
    assert all(v.startswith("answering_") and v == v.lower() for v in values)
