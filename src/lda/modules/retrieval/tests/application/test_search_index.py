"""`SearchIndex`: proof first, scope-bounded, live eligibility, current version only (ADR-008)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest

from lda.modules.access_control.api import ResourceRef
from lda.modules.access_control.testing import an_access_scope
from lda.modules.documents.api import VersionStatus
from lda.modules.retrieval.api import (
    SEARCH_LIMIT_INVALID,
    SEARCH_QUERY_INVALID,
    SEARCH_REQUIRES_PRINCIPAL,
    SEARCH_SCOPE_EMPTY,
    SEARCH_SCOPE_INVALID,
    SEARCH_SCOPE_MISMATCH,
    SearchIndex,
    SearchLimits,
    SearchQuery,
)
from lda.modules.retrieval.domain.embedding import Vector
from lda.modules.retrieval.domain.ids import DocumentId, VersionId
from lda.modules.retrieval.domain.selection import Candidate
from lda.modules.retrieval.testing import (
    InMemoryRetrievalStore,
    InMemoryRetrievalUnitOfWork,
    InMemoryRetrievalUnitOfWorkFactory,
)
from lda.modules.retrieval.testing.in_memory import InMemoryVectorIndexRepository
from lda.modules.retrieval.tests.support import (
    ALICE,
    BOB,
    LIMITS,
    SECRET,
    SYSTEM_A,
    TENANT_A,
    TENANT_B,
    Rig,
    build_rig,
    ctx,
)
from lda.shared.kernel.context import ExecutionContext
from lda.shared.kernel.errors import AuthorizationError, InfrastructureError, ValidationError

PAYMENT = ["Payment is due monthly in advance", "Late payment accrues interest"]
LAW = ["Governing law is the law of New York", "Disputes go to arbitration"]


def _indexed(rig: Rig, texts: list[str], *, ready: bool = True) -> tuple[DocumentId, VersionId]:
    document, version = rig.new_document(texts)
    rig.index(document, version)
    if ready:
        rig.ready(document, version)
    return document, version


def test_results_are_ranked_and_carry_provenance_but_no_text(rig: Rig) -> None:
    document, version = _indexed(rig, PAYMENT + LAW)
    result = rig.search("payment due monthly", [document], limit=3)
    assert [h.rank for h in result.hits] == [1, 2, 3]
    assert [h.score for h in result.hits] == sorted((h.score for h in result.hits), reverse=True)
    top = result.hits[0]
    assert (top.document_id, top.version_id, top.version_number, top.ordinal) == (
        document,
        version,
        1,
        0,
    )
    assert len(top.text_hash) == 64 and result.index_id == rig.embedder.identity.index_id
    assert not hasattr(top, "text")


def test_search_is_deterministic(rig: Rig) -> None:
    document, _ = _indexed(rig, PAYMENT + LAW)
    assert rig.search("payment", [document]) == rig.search("payment", [document])


def test_a_document_outside_the_scope_is_never_returned(rig: Rig) -> None:
    mine, _ = _indexed(rig, PAYMENT)
    theirs, _ = _indexed(rig, ["Payment secrets of another client"])
    hits = rig.search("payment", [mine]).hits
    assert hits and {h.document_id for h in hits} == {mine}
    assert theirs not in {h.document_id for h in rig.search("payment", [mine]).hits}


def test_another_tenants_vectors_are_never_returned(rig: Rig) -> None:
    mine, _ = _indexed(rig, PAYMENT)
    foreign, foreign_version = rig.new_document(PAYMENT, TENANT_B)
    rig.index(foreign, foreign_version, TENANT_B)
    result = rig.search("payment", [mine, foreign])
    assert {h.document_id for h in result.hits} == {mine}


def test_the_proof_is_verified_before_the_query_is_embedded(rig: Rig) -> None:
    document, _ = _indexed(rig, PAYMENT)
    query = rig.query("payment", [document], grant=False)  # a proof nobody granted
    with pytest.raises(AuthorizationError):
        rig.search_index.execute(ctx(), query)
    assert rig.embedder.query_calls == 0


def test_a_proof_for_another_scope_is_refused(rig: Rig) -> None:
    a, _ = _indexed(rig, PAYMENT)
    b, _ = _indexed(rig, LAW)
    rig.query("x", [a], proof_id="proof-a")  # grants proof-a for {a} only
    widened = rig.query("x", [a, b], proof_id="proof-a", grant=False)
    with pytest.raises(AuthorizationError):
        rig.search_index.execute(ctx(), widened)


def test_a_stale_or_expired_proof_fails_closed(rig: Rig) -> None:
    document, _ = _indexed(rig, PAYMENT)
    query = rig.query("payment", [document])
    rig.verifier.revoke("proof-1")  # expired, or the policy epoch moved on
    with pytest.raises(AuthorizationError):
        rig.search_index.execute(ctx(), query)


def test_a_proof_that_goes_stale_during_the_search_returns_nothing(rig: Rig) -> None:
    document, _ = _indexed(rig, PAYMENT)
    query = rig.query("payment", [document])
    rig.verifier.fail_after = 1  # the first check passes, the re-check after the search fails
    with pytest.raises(AuthorizationError):
        rig.search_index.execute(ctx(), query)
    assert rig.verifier.verified == 2


def test_an_unavailable_verifier_fails_closed(rig: Rig) -> None:
    document, _ = _indexed(rig, PAYMENT)
    rig.verifier.unavailable = True
    with pytest.raises(InfrastructureError):
        rig.search("payment", [document])


def test_the_scope_must_belong_to_the_caller(rig: Rig) -> None:
    document, _ = _indexed(rig, PAYMENT)
    wrong_tenant = rig.query("payment", [document], tenant=TENANT_B)
    with pytest.raises(AuthorizationError) as excinfo:
        rig.search_index.execute(ctx(), wrong_tenant)
    assert excinfo.value.code == SEARCH_SCOPE_MISMATCH
    wrong_principal = rig.query("payment", [document], principal=BOB)
    with pytest.raises(AuthorizationError):
        rig.search_index.execute(ctx(principal=ALICE), wrong_principal)


def test_a_system_context_cannot_search(rig: Rig) -> None:
    document, _ = _indexed(rig, PAYMENT)
    with pytest.raises(AuthorizationError) as excinfo:
        rig.search_index.execute(SYSTEM_A, rig.query("payment", [document]))
    assert excinfo.value.code == SEARCH_REQUIRES_PRINCIPAL


def test_an_empty_scope_is_refused(rig: Rig) -> None:
    with pytest.raises(ValidationError) as excinfo:
        rig.search_index.execute(ctx(), rig.query("payment", [], grant=False))
    assert excinfo.value.code == SEARCH_SCOPE_EMPTY


def test_a_scope_with_a_foreign_kind_or_bad_id_is_refused(rig: Rig) -> None:
    for resources in (
        frozenset({ResourceRef("conversation", "c1")}),
        frozenset({ResourceRef("document", "not-a-uuid")}),
    ):
        scope = an_access_scope(tenant_id=TENANT_A, principal_id=ALICE, resources=resources)
        with pytest.raises(ValidationError) as excinfo:
            rig.search_index.execute(ctx(), SearchQuery(scope, "t", "payment", 3))
        assert excinfo.value.code == SEARCH_SCOPE_INVALID


@pytest.mark.parametrize("text", ["", "   ", "x" * 501, "bad\x00text"])
def test_invalid_query_text_is_refused_before_anything_runs(rig: Rig, text: str) -> None:
    document, _ = _indexed(rig, PAYMENT)
    with pytest.raises(ValidationError) as excinfo:
        rig.search(text, [document])
    assert excinfo.value.code == SEARCH_QUERY_INVALID
    assert rig.verifier.verified == 0 and rig.embedder.query_calls == 0


@pytest.mark.parametrize("limit", [0, -1, 11, True])
def test_invalid_limits_are_refused(rig: Rig, limit: int) -> None:
    document, _ = _indexed(rig, PAYMENT)
    with pytest.raises(ValidationError) as excinfo:
        rig.search("payment", [document], limit=limit)
    assert excinfo.value.code == SEARCH_LIMIT_INVALID


def test_a_malformed_query_embedding_is_refused(rig: Rig) -> None:
    document, _ = _indexed(rig, PAYMENT)
    rig.embedder.query_override = [0.0] * 3
    with pytest.raises(ValidationError):
        rig.search("payment", [document])


# --- eligibility and current version -----------------------------------------------------------


@pytest.mark.parametrize(
    "status", [VersionStatus.PENDING, VersionStatus.FAILED, VersionStatus.QUARANTINED]
)
def test_only_ready_versions_are_searchable(rig: Rig, status: VersionStatus) -> None:
    document, version = rig.new_document(PAYMENT)
    rig.index(document, version)
    if status is not VersionStatus.PENDING:
        rig.mark(document, version, status)
    assert rig.search("payment", [document]).hits == ()


def test_a_tombstoned_document_is_unreachable_immediately(rig: Rig) -> None:
    document, _ = _indexed(rig, PAYMENT)
    assert rig.search("payment", [document]).hits
    rig.tombstone(document)  # no purge yet: the vectors still exist
    assert rig.retrieval.store.rows
    assert rig.search("payment", [document]).hits == ()


def test_the_highest_ready_version_wins_and_older_ones_are_not_searched(rig: Rig) -> None:
    document, v1 = _indexed(rig, ["Payment terms version one"])
    v2 = rig.new_version(document, ["Payment terms version two"])
    rig.index(document, v2)
    assert {h.version_id for h in rig.search("payment terms", [document]).hits} == {v1}
    rig.ready(document, v2)
    hits = rig.search("payment terms", [document]).hits
    assert {h.version_id for h in hits} == {v2} and hits[0].version_number == 2


@pytest.mark.parametrize("status", [VersionStatus.FAILED, VersionStatus.QUARANTINED])
def test_a_newer_failed_version_leaves_the_older_ready_one_current(
    rig: Rig, status: VersionStatus
) -> None:
    document, v1 = _indexed(rig, ["Payment terms version one"])
    v2 = rig.new_version(document, ["Payment terms version two"])
    rig.index(document, v2)
    rig.mark(document, v2, status)
    assert {h.version_id for h in rig.search("payment terms", [document]).hits} == {v1}


def test_a_newer_ready_version_indexed_only_under_an_old_identity_hides_the_older_one(
    rig: Rig,
) -> None:
    """Fail closed: v2 is ready but not searchable for this identity (it needs a rebuild), so v1's
    older text must not be served in its place."""
    document, _ = _indexed(rig, ["Payment terms version one"])
    v2 = rig.new_version(document, ["Payment terms version two"])
    old_identity = replace(rig.embedder.identity, version="0")
    record = next(iter(rig.retrieval.store.versions.values()))
    rig.retrieval.store.versions[(TENANT_A, v2, old_identity.index_id)] = replace(
        record, version_id=v2, embedding=old_identity
    )
    rig.ready(document, v2)
    assert rig.search("payment terms", [document]).hits == ()


def test_known_limit_a_ready_version_never_indexed_anywhere_is_invisible(rig: Rig) -> None:
    """Characterization (ADR-008): retrieval can only ask Documents about versions it has an index
    record for. A version that became ready WITHOUT passing the `index` stage cannot be seen, so
    the older ready version stays searchable. The ingestion plan must therefore place `index`
    before a version can become ready; composition tests pin that plan."""
    document, v1 = _indexed(rig, ["Payment terms version one"])
    v2 = rig.new_version(document, ["Payment terms version two"])
    rig.ready(document, v2)  # ready, never indexed: only possible with a plan lacking `index`
    assert {h.version_id for h in rig.search("payment terms", [document]).hits} == {v1}


def test_vectors_of_another_embedding_identity_are_never_compared(rig: Rig) -> None:
    document, _ = _indexed(rig, PAYMENT)
    rig.embedder.switch_identity(replace(rig.embedder.identity, version="2"))
    assert rig.search("payment", [document]).hits == ()  # needs a rebuild, never a mixed compare


def test_search_looks_past_superseded_rows_to_fill_the_limit(rig: Rig) -> None:
    """Page size 4: superseded v1 rows fill the first pages; current rows are still found."""
    document, _ = _indexed(rig, [f"payment clause number {i}" for i in range(8)])
    v2 = rig.new_version(document, ["unrelated governing law text", "arbitration venue"])
    rig.index(document, v2)
    rig.ready(document, v2)
    hits = rig.search("payment clause", [document], limit=2).hits
    assert {h.version_id for h in hits} == {v2} and len(hits) == 2


def test_the_inspection_cap_ends_a_search_with_fewer_results(rig: Rig) -> None:
    limits = SearchLimits(
        max_results=10, max_query_characters=500, candidate_pool=2, max_candidates=4
    )
    small = build_rig(limits)
    document, v1 = small.new_document([f"payment clause {i}" for i in range(6)])
    small.index(document, v1)
    small.ready(document, v1)
    v2 = small.new_version(document, ["other"])
    small.index(document, v2)
    small.ready(document, v2)
    assert small.search("payment clause", [document], limit=3).hits == ()


def test_documents_without_an_index_contribute_nothing(rig: Rig) -> None:
    document, _ = rig.new_document(PAYMENT)  # never indexed
    rig.ready(document, rig.chunks.known_versions()[0][1])
    assert rig.search("payment", [document]).hits == ()


class _LeakyIndex(InMemoryVectorIndexRepository):
    """A repository that ignores the document filter it is given (a bug, or an attack)."""

    def __init__(self, base: InMemoryRetrievalUnitOfWork, documents: list[DocumentId]) -> None:
        super().__init__(base.tenant_id, base.version_rows, base.vector_rows, base.locked)
        self._documents = documents

    def nearest(
        self,
        index_id: str,
        documents: Sequence[DocumentId],
        query: Vector,
        limit: int,
        offset: int,
    ) -> tuple[Candidate, ...]:
        return super().nearest(index_id, self._documents, query, limit, offset)


class _LeakyUnit(InMemoryRetrievalUnitOfWork):
    def __init__(self, base: InMemoryRetrievalUnitOfWork, documents: list[DocumentId]) -> None:
        super().__init__(base.tenant_id, base.store, 0)
        self.leaky = _LeakyIndex(self, documents)

    @property
    def index(self) -> InMemoryVectorIndexRepository:
        return self.leaky


class _LeakyFactory(InMemoryRetrievalUnitOfWorkFactory):
    def __init__(self, store: InMemoryRetrievalStore, documents: list[DocumentId]) -> None:
        super().__init__(store)
        self._documents = documents

    def begin(self, ctx: ExecutionContext) -> InMemoryRetrievalUnitOfWork:
        return _LeakyUnit(super().begin(ctx), self._documents)


def test_a_repository_returning_out_of_scope_rows_is_still_filtered(rig: Rig) -> None:
    mine, _ = _indexed(rig, PAYMENT)
    theirs, _ = _indexed(rig, ["Payment secrets"])
    widened = SearchIndex(
        _LeakyFactory(rig.retrieval.store, [mine, theirs]),
        rig.embedder,
        rig.verifier,
        rig.states,
        LIMITS,
    )
    result = widened.execute(ctx(), rig.query("payment", [mine]))
    assert result.hits and {h.document_id for h in result.hits} == {mine}


def test_the_query_text_and_scope_never_appear_in_reprs_or_errors(rig: Rig) -> None:
    document, _ = _indexed(rig, PAYMENT)
    query = rig.query(f"{SECRET} please", [document], proof_id="proof-secret-token")
    assert SECRET not in repr(query) and "proof-secret-token" not in repr(query)
    rig.verifier.revoke("proof-secret-token")
    with pytest.raises(AuthorizationError) as excinfo:
        rig.search_index.execute(ctx(), query)
    for text in (str(excinfo.value), repr(excinfo.value)):
        assert SECRET not in text and "proof-secret-token" not in text
