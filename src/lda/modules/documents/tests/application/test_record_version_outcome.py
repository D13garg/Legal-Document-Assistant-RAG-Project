"""RecordVersionOutcome: Documents stays the authority over its own version status (ADR-015)."""

from __future__ import annotations

from uuid import UUID

import pytest

from lda.modules.documents.application.dto.documents import (
    DocumentReference,
    OutcomeResult,
    RecordOutcomeCommand,
)
from lda.modules.documents.application.use_cases.record_version_outcome import RecordVersionOutcome
from lda.modules.documents.domain.document import VersionStatus
from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.modules.documents.tests.support import (
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    Rig,
    ctx_for,
    uploaded_ids,
)
from lda.platform.testing.doubles import FakeClock
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import ValidationError
from lda.shared.ports.audit import AuditFact, AuditOutcome

R = VersionStatus


@pytest.fixture
def uc(rig: Rig, clock: FakeClock) -> RecordVersionOutcome:
    return RecordVersionOutcome(rig.factory, clock)


def committed_facts(rig: Rig) -> list[AuditFact]:
    return [f for u in rig.factory.begun if u.committed for f in u.audit.facts]


def version_status(rig: Rig, version_id: DocumentVersionId) -> VersionStatus:
    with rig.factory.begin(ctx_for(TENANT_A)) as uow:
        found = uow.versions.get(version_id)
        assert found is not None
        return found.status


def make_pending(rig: Rig) -> tuple[DocumentId, DocumentVersionId]:
    return uploaded_ids(rig, ctx_for(TENANT_A))


def outcome_facts(rig: Rig) -> list[AuditFact]:
    return [f for f in committed_facts(rig) if f.action == "documents.version_outcome_recorded"]


@pytest.mark.parametrize("outcome", [R.READY, R.FAILED, R.QUARANTINED])
def test_a_pending_version_takes_the_reported_outcome_and_it_is_audited_with_identifiers_only(
    rig: Rig, uc: RecordVersionOutcome, outcome: VersionStatus
) -> None:
    document_id, version_id = make_pending(rig)
    assert version_status(rig, version_id) is R.PENDING
    result = uc.execute(SYSTEM_A, RecordOutcomeCommand(document_id, version_id, outcome))
    assert result.result is OutcomeResult.APPLIED
    assert version_status(rig, version_id) is outcome
    (fact,) = outcome_facts(rig)
    assert (fact.tenant_id, fact.resource_type, fact.resource_id) == (
        TENANT_A,
        "document",
        str(document_id),
    )
    assert fact.outcome is AuditOutcome.SUCCESS
    attrs = dict(fact.attributes)
    assert attrs["version_id"] == str(version_id) and attrs["status"] == outcome.value
    assert "purpose" in attrs  # the system actor says why
    assert "Master services agreement" not in repr(fact)  # never the title, never content


def test_the_outcome_is_idempotent_and_audited_once(rig: Rig, uc: RecordVersionOutcome) -> None:
    document_id, version_id = make_pending(rig)
    command = RecordOutcomeCommand(document_id, version_id, R.READY)
    assert uc.execute(SYSTEM_A, command).result is OutcomeResult.APPLIED
    for _ in range(3):
        assert uc.execute(SYSTEM_A, command).result is OutcomeResult.ALREADY
    assert len(outcome_facts(rig)) == 1
    assert version_status(rig, version_id) is R.READY


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (R.READY, R.FAILED),
        (R.READY, R.QUARANTINED),
        (R.FAILED, R.READY),
        (R.QUARANTINED, R.READY),
        (R.QUARANTINED, R.FAILED),
        (R.FAILED, R.QUARANTINED),
    ],
)
def test_a_different_terminal_outcome_is_refused_and_the_first_one_stands(
    rig: Rig, uc: RecordVersionOutcome, first: VersionStatus, second: VersionStatus
) -> None:
    document_id, version_id = make_pending(rig)
    uc.execute(SYSTEM_A, RecordOutcomeCommand(document_id, version_id, first))
    result = uc.execute(SYSTEM_A, RecordOutcomeCommand(document_id, version_id, second))
    assert result.result is OutcomeResult.REFUSED
    assert version_status(rig, version_id) is first
    assert len(outcome_facts(rig)) == 1  # the refusal is not an event


def test_pending_is_not_a_reportable_outcome(rig: Rig, uc: RecordVersionOutcome) -> None:
    document_id, version_id = make_pending(rig)
    with pytest.raises(ValidationError, match="document_outcome_invalid"):
        uc.execute(SYSTEM_A, RecordOutcomeCommand(document_id, version_id, R.PENDING))
    assert version_status(rig, version_id) is R.PENDING
    assert outcome_facts(rig) == []


def test_a_deleted_document_is_never_touched(rig: Rig, uc: RecordVersionOutcome) -> None:
    document_id, version_id = make_pending(rig)
    rig.delete.execute(ctx_for(TENANT_A), DocumentReference(document_id))
    for outcome in (R.READY, R.FAILED, R.QUARANTINED):
        result = uc.execute(SYSTEM_A, RecordOutcomeCommand(document_id, version_id, outcome))
        assert result.result is OutcomeResult.DELETED
    assert version_status(rig, version_id) is R.PENDING
    assert outcome_facts(rig) == []


def test_unknown_versions_mismatched_documents_and_other_tenants_are_missing(
    rig: Rig, uc: RecordVersionOutcome
) -> None:
    document_id, version_id = make_pending(rig)
    other_document, other_version = make_pending(rig)
    stranger_version = DocumentVersionId(UUID(int=0xBAD))
    stranger_document = DocumentId(UUID(int=0xBAD))
    other_tenant = SystemContext(TENANT_B, "x")
    cases = [
        (SYSTEM_A, RecordOutcomeCommand(document_id, stranger_version, R.READY)),
        (SYSTEM_A, RecordOutcomeCommand(other_document, version_id, R.READY)),  # not a pair
        (SYSTEM_A, RecordOutcomeCommand(stranger_document, version_id, R.READY)),
        (other_tenant, RecordOutcomeCommand(document_id, version_id, R.READY)),
    ]
    for ctx, command in cases:
        assert uc.execute(ctx, command).result is OutcomeResult.MISSING
    assert version_status(rig, version_id) is R.PENDING
    assert version_status(rig, other_version) is R.PENDING
    assert outcome_facts(rig) == []


def test_a_failed_commit_leaves_the_version_pending_and_unaudited(
    rig: Rig, uc: RecordVersionOutcome
) -> None:
    from lda.modules.documents.tests.support import FaultyFactory
    from lda.shared.kernel.errors import InfrastructureError

    document_id, version_id = make_pending(rig)
    faulty = RecordVersionOutcome(FaultyFactory(rig.factory, len(rig.factory.begun)), rig.clock)
    with pytest.raises(InfrastructureError):
        faulty.execute(SYSTEM_A, RecordOutcomeCommand(document_id, version_id, R.READY))
    assert version_status(rig, version_id) is R.PENDING
    assert outcome_facts(rig) == []
    assert (
        uc.execute(SYSTEM_A, RecordOutcomeCommand(document_id, version_id, R.READY)).result
        is OutcomeResult.APPLIED
    )


def test_losing_the_compare_and_set_is_refused_and_leaves_no_trace(
    rig: Rig, uc: RecordVersionOutcome
) -> None:
    """Another transaction recorded a different outcome between our read and our write."""
    document_id, version_id = make_pending(rig)
    original = rig.factory.begin

    def racing(ctx):  # type: ignore[no-untyped-def]
        uow = original(ctx)
        uow.versions.set_outcome = lambda *_: False  # type: ignore[method-assign]
        return uow

    rig.factory.begin = racing  # type: ignore[method-assign]
    result = uc.execute(SYSTEM_A, RecordOutcomeCommand(document_id, version_id, R.READY))
    assert result.result is OutcomeResult.REFUSED
    assert outcome_facts(rig) == []  # nothing audited, nothing committed
    rig.factory.begin = original  # type: ignore[method-assign]
    assert version_status(rig, version_id) is R.PENDING
