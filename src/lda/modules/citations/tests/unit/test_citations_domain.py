"""Domain invariants: sources, evidence markers, GenerationRecord."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from lda.modules.citations.api import (
    CITATION_MISSING,
    INVALID_INPUT,
    LIMIT_EXCEEDED,
    RECORD_INVALID,
    UNKNOWN_MARKER,
    Citation,
    CitationSource,
    EvidenceSet,
    GenerationOutcome,
    GenerationRecord,
    source_resource,
)
from lda.modules.citations.api import (
    CitedDocumentId as DocumentId,
)
from lda.modules.citations.api import (
    CitedVersionId as VersionId,
)
from lda.modules.citations.tests.support import Scenario, digest, new_ctx
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import RequestId


def source(**over: object) -> CitationSource:
    base: dict[str, object] = {
        "document_id": DocumentId(uuid4()),
        "version_id": VersionId(uuid4()),
        "chunk_id": uuid4(),
        "ordinal": 0,
        "text_hash": digest(1),
        "page_start": 2,
        "page_end": 3,
    }
    return CitationSource(**{**base, **over})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        {"ordinal": -1},
        {"ordinal": True},
        {"text_hash": "abc"},
        {"text_hash": "A" * 64},
        {"chunk_id": "not-a-uuid"},
        {"page_start": 0, "page_end": 1},
        {"page_start": 5, "page_end": 3},
        {"page_start": 2, "page_end": None},
        {"page_start": None, "page_end": 2},
        {"page_start": True, "page_end": True},
    ],
)
def test_invalid_sources_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError) as caught:
        source(**bad)
    assert caught.value.code == INVALID_INPUT


def test_a_source_is_frozen_and_carries_no_text_fields() -> None:
    s = source()
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.ordinal = 9  # type: ignore[misc]
    names = {f.name for f in dataclasses.fields(CitationSource)}
    assert names == {
        "document_id", "version_id", "chunk_id", "ordinal", "text_hash", "page_start", "page_end"
    }  # fmt: skip
    ref = source_resource(s)
    assert (ref.kind, ref.id) == ("document", str(s.document_id.value))


def test_markers_resolve_dedupe_and_keep_first_use_order() -> None:
    evidence = Scenario().evidence()
    got = evidence.resolve(["S3", "S1", "S3"], max_markers=10)
    assert [c.marker for c in got] == ["S3", "S1"]


@pytest.mark.parametrize(
    "marker", ["S0", "S4", "s1", "S01", " S1", "S1 ", "1", "", "S1\n", None, 7]
)
def test_unknown_or_malformed_markers_are_refused(marker: object) -> None:
    evidence = Scenario().evidence()
    with pytest.raises(ValidationError) as caught:
        evidence.resolve(["S1", marker], max_markers=10)  # type: ignore[list-item]
    assert caught.value.code == UNKNOWN_MARKER


def test_no_marker_is_refused_unless_the_caller_allows_a_non_answer() -> None:
    evidence = Scenario().evidence()
    with pytest.raises(ValidationError) as caught:
        evidence.resolve([], max_markers=10)
    assert caught.value.code == CITATION_MISSING
    assert evidence.resolve([], max_markers=10, allow_empty=True) == ()


def test_too_many_markers_are_refused() -> None:
    evidence = Scenario().evidence()
    with pytest.raises(ValidationError) as caught:
        evidence.resolve(["S1"] * 4, max_markers=3)
    assert caught.value.code == LIMIT_EXCEEDED


def test_an_evidence_set_must_have_sequential_markers_and_unique_chunks() -> None:
    ctx = new_ctx()
    a, b = Citation("S1", source()), Citation("S2", source())
    for bad in ((), (b, a), (Citation("S2", a.source),), (a, Citation("S2", a.source))):
        with pytest.raises(ValidationError):
            EvidenceSet(ctx.tenant_id, ctx.principal_id, bad)
    assert EvidenceSet(ctx.tenant_id, ctx.principal_id, (a, b)).contains(a)


def test_the_fingerprint_is_deterministic_and_changes_with_any_source_detail() -> None:
    s = Scenario()
    one, two = s.evidence(), s.evidence()
    assert one.fingerprint() == two.fingerprint()
    other = s.evidence(s.claim("a", 0))
    assert other.fingerprint() != one.fingerprint()
    assert len(one.fingerprint()) == 64


def record(**over: object) -> GenerationRecord:
    ctx = new_ctx()
    s = Scenario(ctx)
    evidence = s.evidence()
    base: dict[str, object] = {
        "record_id": uuid4(),
        "tenant_id": ctx.tenant_id,
        "principal_id": ctx.principal_id,
        "request_id": RequestId(uuid4()),
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "config_id": "rag-" + digest(5),
        "outcome": GenerationOutcome.ANSWERED,
        "template_id": "grounded_answer",
        "template_version": "v1",
        "model_id": "fake-model:1",
        "request_sha256": digest(6),
        "evidence_fingerprint": evidence.fingerprint(),
        "citations": evidence.citations[:1],
    }
    return GenerationRecord(**{**base, **over})  # type: ignore[arg-type]


def test_a_generation_record_is_immutable() -> None:
    r = record()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.outcome = GenerationOutcome.REJECTED  # type: ignore[misc]
    assert isinstance(r.citations, tuple)


@pytest.mark.parametrize(
    "bad",
    [
        {"config_id": digest(5)},
        {"config_id": "rag-" + "G" * 64},
        {"request_sha256": "short"},
        {"evidence_fingerprint": ""},
        {"created_at": datetime(2026, 1, 1)},
        {"template_id": "has space"},
        {"model_id": ""},
        {"outcome": "answered"},
        {"record_id": "x"},
    ],
)
def test_invalid_records_are_refused(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        record(**bad)


def test_outcome_and_citations_must_agree() -> None:
    for outcome in (GenerationOutcome.INSUFFICIENT_EVIDENCE, GenerationOutcome.REJECTED):
        assert record(outcome=outcome, citations=()).citations == ()
        with pytest.raises(ValidationError) as caught:
            record(outcome=outcome)
        assert caught.value.code == RECORD_INVALID
    with pytest.raises(ValidationError) as caught:
        record(citations=())  # answered without a citation
    assert caught.value.code == RECORD_INVALID


def test_a_record_repr_and_fields_hold_no_content_or_proof() -> None:
    names = {f.name for f in dataclasses.fields(GenerationRecord)}
    assert not names & {"text", "answer", "prompt", "question", "excerpt", "proof", "token"}
