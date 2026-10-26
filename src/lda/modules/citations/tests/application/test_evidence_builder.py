"""EvidenceBuilder: provenance, forgery, isolation, purge, proof ordering, content-free errors."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from lda.modules.citations.api import (
    INVALID_INPUT,
    LIMIT_EXCEEDED,
    PROVENANCE_MISMATCH,
    SOURCE_UNAVAILABLE,
    BuildEvidenceCommand,
    ChunkProvenance,
)
from lda.modules.citations.api import (
    CitedDocumentId as DocumentId,
)
from lda.modules.citations.api import (
    CitedVersionId as VersionId,
)
from lda.modules.citations.testing import FakeProvenance, RecordingVerifier
from lda.modules.citations.tests.support import TOKEN, Scenario, digest, new_ctx
from lda.shared.kernel.errors import AuthorizationError, LdaError, NotFoundError, ValidationError
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.authorization import ProofExpectation


def test_citations_come_from_the_chunk_store_with_markers_in_claim_order() -> None:
    s = Scenario()
    evidence = s.evidence()
    assert [c.marker for c in evidence.citations] == ["S1", "S2", "S3"]
    first, second, third = (c.source for c in evidence.citations)
    assert (first.chunk_id, first.page_start, first.page_end) == (s.chunks_a[0].chunk_id, 1, 1)
    assert (second.page_start, second.page_end) == (1, 2)
    assert (third.document_id, third.version_id) == (s.doc_b, s.ver_b)
    assert (third.page_start, third.page_end) == (7, 9)


def test_missing_pages_stay_missing_and_are_not_invented() -> None:
    s = Scenario()
    source = s.evidence(s.claim("a", 2)).citations[0].source
    assert (source.page_start, source.page_end) == (None, None)


class _OrderedVerifier(RecordingVerifier):
    def __init__(self, inner: RecordingVerifier, events: list[str]) -> None:
        super().__init__(inner.token, inner.accepted)
        self._events = events

    def verify(self, token: str, expected: ProofExpectation) -> None:
        self._events.append("verify")
        super().verify(token, expected)


class _OrderedProvenance(FakeProvenance):
    def __init__(self, inner: FakeProvenance, events: list[str]) -> None:
        super().__init__()
        self.versions = inner.versions
        self._events = events

    def read(
        self, tenant_id: TenantId, document_id: DocumentId, version_id: VersionId
    ) -> tuple[ChunkProvenance, ...]:
        self._events.append("read")
        return super().read(tenant_id, document_id, version_id)


def test_the_proof_is_verified_before_any_chunk_read_and_again_after() -> None:
    s = Scenario()
    events: list[str] = []
    verifier = _OrderedVerifier(s.verifier(s.doc_a, s.doc_b), events)
    s.provenance = _OrderedProvenance(s.provenance, events)
    s.builder(verifier).build(s.ctx, BuildEvidenceCommand((s.claim("a"), s.claim("b")), TOKEN))
    assert events == ["verify", "read", "read", "verify"]


def test_a_bad_proof_means_no_chunk_store_is_touched() -> None:
    s = Scenario()
    with pytest.raises(AuthorizationError):
        s.builder().build(s.ctx, BuildEvidenceCommand((s.claim(),), "forged"))
    assert s.provenance.reads == 0


def test_the_proof_must_cover_exactly_the_cited_documents() -> None:
    s = Scenario()
    wider = s.verifier(s.doc_a, s.doc_b)  # proof for both, but only document a is cited
    with pytest.raises(AuthorizationError):
        s.builder(wider).build(s.ctx, BuildEvidenceCommand((s.claim("a"),), TOKEN))
    assert s.provenance.reads == 0


def test_revocation_during_the_read_yields_no_evidence() -> None:
    s = Scenario()
    verifier = s.verifier(s.doc_a)
    verifier.revoke_after = 1
    with pytest.raises(AuthorizationError):
        s.builder(verifier).build(s.ctx, BuildEvidenceCommand((s.claim(),), TOKEN))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda c: replace(c, chunk_id=uuid4()),  # a chunk id the store never minted
        lambda c: replace(c, ordinal=c.ordinal + 1),
        lambda c: replace(c, text_hash=digest(99)),
    ],
)
def test_a_forged_or_mismatched_claim_is_refused_without_naming_the_field(tamper) -> None:  # type: ignore[no-untyped-def]
    s = Scenario()
    with pytest.raises(ValidationError) as caught:
        s.builder(s.verifier(s.doc_a)).build(
            s.ctx, BuildEvidenceCommand((tamper(s.claim()),), TOKEN)
        )
    assert caught.value.code == PROVENANCE_MISMATCH


def test_a_chunk_of_another_version_of_the_same_document_is_a_mismatch() -> None:
    s = Scenario()
    other = replace(s.claim("b"), document_id=s.doc_a, version_id=s.ver_a)
    with pytest.raises(ValidationError) as caught:
        s.builder(s.verifier(s.doc_a)).build(s.ctx, BuildEvidenceCommand((other,), TOKEN))
    assert caught.value.code == PROVENANCE_MISMATCH


def test_another_tenants_source_is_indistinguishable_from_a_missing_one() -> None:
    s = Scenario()
    foreign = new_ctx()  # same documents, different tenant: the store has nothing under it
    verifier = RecordingVerifier(TOKEN)
    with pytest.raises(NotFoundError) as caught:
        s.builder(verifier).build(foreign, BuildEvidenceCommand((s.claim(),), TOKEN))
    assert caught.value.code == SOURCE_UNAVAILABLE


def test_a_purged_or_ineligible_version_refuses_the_whole_set() -> None:
    s = Scenario()
    s.provenance.drop(s.ctx.tenant_id, s.doc_b, s.ver_b)
    with pytest.raises(NotFoundError) as caught:
        s.builder().build(s.ctx, BuildEvidenceCommand((s.claim("a"), s.claim("b")), TOKEN))
    assert caught.value.code == SOURCE_UNAVAILABLE


@pytest.mark.parametrize(
    ("claims", "token", "code"),
    [
        ((), TOKEN, INVALID_INPUT),
        (None, TOKEN, INVALID_INPUT),
        ("claims", TOKEN, INVALID_INPUT),
        ((object(),), TOKEN, INVALID_INPUT),
        (None, "", INVALID_INPUT),
    ],
)
def test_malformed_input_is_refused_before_authorization(claims, token, code) -> None:  # type: ignore[no-untyped-def]
    s = Scenario()
    verifier = s.verifier(s.doc_a)
    with pytest.raises(ValidationError) as caught:
        s.builder(verifier).build(s.ctx, BuildEvidenceCommand(claims, token))
    assert caught.value.code == code
    assert verifier.calls == []


def test_duplicate_chunks_malformed_hashes_and_oversize_sets_are_refused() -> None:
    s = Scenario()
    claim = s.claim()
    for bad in ((claim, claim), (replace(claim, text_hash="XYZ"),), (replace(claim, ordinal=-1),)):
        with pytest.raises(ValidationError) as caught:
            s.builder().build(s.ctx, BuildEvidenceCommand(bad, TOKEN))
        assert caught.value.code == INVALID_INPUT
    with pytest.raises(ValidationError) as caught:
        s.builder(max_sources=1).build(s.ctx, BuildEvidenceCommand((claim, s.claim("a", 1)), TOKEN))
    assert caught.value.code == LIMIT_EXCEEDED


def test_errors_and_reprs_carry_no_identifiers_hashes_or_tokens() -> None:
    s = Scenario()
    claim = replace(s.claim(), text_hash=digest(99))
    command = BuildEvidenceCommand((claim,), TOKEN)
    assert TOKEN not in repr(command)
    with pytest.raises(LdaError) as caught:
        s.builder().build(s.ctx, command)
    text = f"{caught.value!r} {caught.value} {caught.value.code}"
    for secret in (TOKEN, digest(99), str(claim.chunk_id), str(claim.document_id.value)):
        assert secret not in text


def test_an_unrelated_document_id_type_is_refused() -> None:
    s = Scenario()
    bad = replace(s.claim(), document_id=uuid4())  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        s.builder().build(s.ctx, BuildEvidenceCommand((bad,), TOKEN))
