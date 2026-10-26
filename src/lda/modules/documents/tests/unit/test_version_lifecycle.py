from __future__ import annotations

import pytest

from lda.modules.documents.domain.document import DocumentVersion, VersionStatus
from lda.modules.documents.tests.contract.test_repository_contract import make
from lda.shared.kernel.errors import ConflictError


def pending() -> DocumentVersion:
    return make()[1]


def test_the_four_statuses_are_exactly_these_strings() -> None:
    assert [s.value for s in VersionStatus] == ["pending", "ready", "failed", "quarantined"]


@pytest.mark.parametrize("outcome", ["ready", "failed", "quarantined"])
def test_a_pending_version_takes_a_terminal_outcome_and_nothing_else_changes(
    outcome: str,
) -> None:
    before = pending()
    after = before.with_outcome(VersionStatus(outcome))
    assert after.status.value == outcome and before.status is VersionStatus.PENDING
    assert (after.id, after.document_id, after.number, after.size_bytes, after.media_type) == (
        before.id,
        before.document_id,
        before.number,
        before.size_bytes,
        before.media_type,
    )
    assert after.created_at == before.created_at and after.created_by == before.created_by


def test_pending_is_not_an_outcome() -> None:
    with pytest.raises(ConflictError, match="document_version_outcome_invalid"):
        pending().with_outcome(VersionStatus.PENDING)


@pytest.mark.parametrize("first", ["ready", "failed", "quarantined"])
@pytest.mark.parametrize("second", ["pending", "ready", "failed", "quarantined"])
def test_a_terminal_version_never_takes_another_outcome(first: str, second: str) -> None:
    done = pending().with_outcome(VersionStatus(first))
    with pytest.raises(ConflictError, match="document_version_outcome_invalid"):
        done.with_outcome(VersionStatus(second))
