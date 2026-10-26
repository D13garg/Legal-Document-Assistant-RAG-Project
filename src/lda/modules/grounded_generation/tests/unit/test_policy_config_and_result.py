"""Policy, settings and result invariants of grounded generation (ADR-029)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError as SettingsError

from lda.modules.ai_gateway.api import GenerationParameters
from lda.modules.citations.api import (
    Citation,
    CitationSource,
    CitedDocumentId,
    CitedVersionId,
    EvidenceSet,
    GenerationOutcome,
    GenerationRecord,
)
from lda.modules.grounded_generation.application.dto import AnswerStatement, GroundedAnswer
from lda.modules.grounded_generation.application.policy import (
    CONFIG_ID,
    MODEL_LABEL,
    TEMPLATE_LABEL,
    GroundingPolicy,
)
from lda.modules.grounded_generation.config import GroundedGenerationSettings
from lda.modules.grounded_generation.domain.answer_contract import ContractLimits
from lda.modules.grounded_generation.tests.support import CONFIG_ID as FIXTURE_CONFIG_ID
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import PrincipalId, RequestId, TenantId
from lda.shared.kernel.sensitive import Sensitive


def policy(**overrides: object) -> GroundingPolicy:
    base: dict[str, object] = {
        "model": "fake-model:1",
        "template_id": "grounded_answer",
        "template_version": 1,
        "parameters": GenerationParameters(256, 0.0),
        "contract": ContractLimits(),
        "max_sources": 5,
    }
    base.update(overrides)
    return GroundingPolicy(**base)  # type: ignore[arg-type]


def test_a_valid_policy_derives_the_marker_bound_from_the_contract() -> None:
    assert policy().max_markers == ContractLimits().max_statements


@pytest.mark.parametrize(
    "bad",
    [
        {"model": ""},
        {"model": "bad model"},
        {"template_id": "Bad-Id"},
        {"template_version": 0},
        {"template_version": True},
        {"max_sources": 0},
        {"parameters": object()},
        {"contract": object()},
    ],
)
def test_an_invalid_policy_is_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        policy(**bad)


def test_the_model_has_no_default_so_a_missing_value_fails_closed() -> None:
    with pytest.raises(SettingsError):
        GroundedGenerationSettings()  # type: ignore[call-arg]
    settings = GroundedGenerationSettings(model="m-1")
    assert (settings.template_id, settings.template_version, settings.temperature) == (
        "grounded_answer",
        1,
        0.0,
    )


def test_settings_defaults_equal_the_contract_defaults() -> None:
    s, c = GroundedGenerationSettings(model="m-1"), ContractLimits()
    assert (
        s.max_output_characters,
        s.max_statements,
        s.max_statement_characters,
        s.min_quote_characters,
        s.max_quote_characters,
    ) == (
        c.max_output_characters,
        c.max_statements,
        c.max_statement_characters,
        c.min_quote_characters,
        c.max_quote_characters,
    )


def test_unknown_settings_are_refused() -> None:
    with pytest.raises(SettingsError):
        GroundedGenerationSettings(model="m-1", surprise=1)  # type: ignore[call-arg]


def test_this_modules_label_shapes_never_admit_what_the_record_would_refuse() -> None:
    """The use case validates before the provider call; the record validates after it. They must
    agree, or an answer could be generated and then lost."""
    source = CitationSource(
        CitedDocumentId(uuid4()), CitedVersionId(uuid4()), uuid4(), 0, "a" * 64, None, None
    )

    evidence = EvidenceSet(TenantId(uuid4()), PrincipalId(uuid4()), (Citation("S1", source),))
    assert CONFIG_ID.fullmatch(FIXTURE_CONFIG_ID)
    for model in ("fake-model:1", "gpt.4o/mini-2", "m" * 128, "a:b/c.d_e-f"):
        assert MODEL_LABEL.fullmatch(model)
        _record(evidence, model=model, template_id="grounded_answer", config=FIXTURE_CONFIG_ID)
    for model in ("", "-x", "m" * 129, "a b", "a\n"):
        assert MODEL_LABEL.fullmatch(model) is None
    assert TEMPLATE_LABEL.fullmatch("grounded_answer")
    assert CONFIG_ID.fullmatch("rag-" + "G" * 64) is None


def _record(evidence: object, *, model: str, template_id: str, config: str) -> GenerationRecord:
    return GenerationRecord(
        record_id=uuid4(),
        tenant_id=evidence.tenant_id,  # type: ignore[attr-defined]
        principal_id=evidence.principal_id,  # type: ignore[attr-defined]
        request_id=RequestId(uuid4()),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        config_id=config,
        outcome=GenerationOutcome.INSUFFICIENT_EVIDENCE,
        template_id=template_id,
        template_version="v1",
        model_id=model,
        request_sha256="b" * 64,
        evidence_fingerprint=evidence.fingerprint(),  # type: ignore[attr-defined]
        citations=(),
    )


def citation() -> Citation:
    source = CitationSource(
        CitedDocumentId(uuid4()), CitedVersionId(uuid4()), uuid4(), 0, "a" * 64, None, None
    )
    return Citation("S1", source)


def test_result_invariants() -> None:
    statement = AnswerStatement(Sensitive("t"), Sensitive("q"), citation())
    GroundedAnswer(GenerationOutcome.ANSWERED, (statement,), uuid4())
    GroundedAnswer(GenerationOutcome.INSUFFICIENT_EVIDENCE, (), None)
    GroundedAnswer(GenerationOutcome.REJECTED, (), uuid4(), "grounded_output_schema")
    for outcome, statements, record_id, rejection in (
        (GenerationOutcome.ANSWERED, (), uuid4(), None),  # an answer needs statements
        (GenerationOutcome.INSUFFICIENT_EVIDENCE, (statement,), uuid4(), None),
        (GenerationOutcome.REJECTED, (), uuid4(), None),  # a rejection names its reason
        (GenerationOutcome.ANSWERED, (statement,), uuid4(), "x"),
        (GenerationOutcome.ANSWERED, (statement,), None, None),  # only 'no sources' has no record
        (GenerationOutcome.REJECTED, (), None, "grounded_output_schema"),
        ("answered", (statement,), uuid4(), None),
    ):
        with pytest.raises(ValidationError):
            GroundedAnswer(outcome, statements, record_id, rejection)  # type: ignore[arg-type]


def test_a_result_hides_statement_content_in_repr() -> None:
    statement = AnswerStatement(Sensitive("HIDDEN"), Sensitive("HIDDEN-Q"), citation())
    shown = repr(GroundedAnswer(GenerationOutcome.ANSWERED, (statement,), uuid4()))
    assert "HIDDEN" not in shown
