"""GenerationRecorder: insert-only record, evidence ownership, forged citations."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from lda.modules.citations.api import (
    CITATION_NOT_IN_EVIDENCE,
    EVIDENCE_FOREIGN,
    RECORD_EXISTS,
    Citation,
    GenerationOutcome,
    GenerationRecorder,
    RecordGenerationCommand,
)
from lda.modules.citations.testing import FixedClock, FreshIds, InMemoryGenerationRecords
from lda.modules.citations.tests.support import Scenario, digest, new_ctx
from lda.shared.kernel.errors import AuthorizationError, ConflictError, ValidationError
from lda.shared.kernel.ids import RequestId


def command(s: Scenario, **over: object) -> RecordGenerationCommand:
    evidence = s.evidence()
    base: dict[str, object] = {
        "request_id": RequestId(uuid4()),
        "evidence": evidence,
        "citations": evidence.resolve(["S2", "S1"], max_markers=5),
        "outcome": GenerationOutcome.ANSWERED,
        "config_id": "rag-" + digest(5),
        "template_id": "grounded_answer",
        "template_version": "v1",
        "model_id": "fake-model:1",
        "request_sha256": digest(6),
    }
    return RecordGenerationCommand(**{**base, **over})  # type: ignore[arg-type]


def recorder(records: InMemoryGenerationRecords | None = None) -> GenerationRecorder:
    return GenerationRecorder(records or InMemoryGenerationRecords(), FixedClock(), FreshIds())


def test_a_record_is_written_with_the_verified_citations_and_evidence_fingerprint() -> None:
    s, records = Scenario(), InMemoryGenerationRecords()
    cmd = command(s)
    done = recorder(records).record(s.ctx, cmd)
    stored = records.get(s.ctx.tenant_id, done.record_id)
    assert stored is not None
    assert stored.citations == cmd.citations
    assert stored.evidence_fingerprint == cmd.evidence.fingerprint()
    assert (stored.tenant_id, stored.principal_id) == (s.ctx.tenant_id, s.ctx.principal_id)
    assert stored.created_at == FixedClock().now()


def test_another_tenant_cannot_read_the_record() -> None:
    s, records = Scenario(), InMemoryGenerationRecords()
    done = recorder(records).record(s.ctx, command(s))
    assert records.get(new_ctx().tenant_id, done.record_id) is None


def test_the_repository_is_insert_only() -> None:
    s, records = Scenario(), InMemoryGenerationRecords()
    done = recorder(records).record(s.ctx, command(s))
    stored = records.get(s.ctx.tenant_id, done.record_id)
    assert stored is not None
    with pytest.raises(ConflictError) as caught:
        records.add(stored)
    assert caught.value.code == RECORD_EXISTS
    assert {n for n in dir(records) if not n.startswith("_")} == {"add", "get"}


def test_evidence_of_another_tenant_or_principal_is_refused() -> None:
    s = Scenario()
    cmd = command(s)
    for other in (new_ctx(), replace(s.ctx, principal_id=new_ctx().principal_id)):
        with pytest.raises(AuthorizationError) as caught:
            recorder().record(other, cmd)
        assert caught.value.code == EVIDENCE_FOREIGN


def test_a_citation_the_evidence_did_not_issue_is_refused() -> None:
    s = Scenario()
    cmd = command(s)
    hand_made = Citation("S1", replace(cmd.citations[0].source, ordinal=42))
    other_generation = Scenario(s.ctx).evidence().citations[:1]
    for bad in ((hand_made,), other_generation, (*cmd.citations, hand_made)):
        records = InMemoryGenerationRecords()
        with pytest.raises(ValidationError) as caught:
            recorder(records).record(s.ctx, replace(cmd, citations=bad))
        assert caught.value.code == CITATION_NOT_IN_EVIDENCE
        assert records.get(s.ctx.tenant_id, uuid4()) is None


def test_an_answer_without_citations_and_a_non_answer_with_them_are_refused() -> None:
    s = Scenario()
    with pytest.raises(ValidationError):
        recorder().record(s.ctx, command(s, citations=()))
    with pytest.raises(ValidationError):
        recorder().record(s.ctx, command(s, outcome=GenerationOutcome.INSUFFICIENT_EVIDENCE))
    done = recorder().record(
        s.ctx, command(s, outcome=GenerationOutcome.INSUFFICIENT_EVIDENCE, citations=())
    )
    assert done.record_id


def test_the_request_hash_stays_out_of_reprs() -> None:
    s = Scenario()
    assert digest(6) not in repr(command(s))
