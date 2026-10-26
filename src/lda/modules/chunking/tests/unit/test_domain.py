"""Chunk set bytes and ids, the record, reconciliation, the receipt."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from lda.modules.chunking.domain.chunk import Chunk, ChunkSet, Segment, content_hash
from lda.modules.chunking.domain.failures import PERMANENT_CODES, ChunkingRejected
from lda.modules.chunking.domain.ids import DocumentId, VersionId
from lda.modules.chunking.domain.record import (
    PURGE_SCOPE,
    ChunkSetRecord,
    PurgeReceipt,
    Reconciliation,
    reconcile,
)
from lda.modules.chunking.domain.resource import chunks_path
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import TenantId

T1, T2 = TenantId(UUID(int=1)), TenantId(UUID(int=2))
DOC, VER = DocumentId(UUID(int=10)), VersionId(UUID(int=11))
H = "a" * 64
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def chunk(ordinal: int, text: str = "Clause é") -> Chunk:
    return Chunk(ordinal, text, (Segment(0, 0, len(text)),), ("Art 1",), None, None)


def make(tenant: TenantId = T1, h: str = H, version: str = "chunk-1; max=1; min=1") -> ChunkSet:
    return ChunkSet(tenant, DOC, VER, h, version, (chunk(0), chunk(1, "Second")))


def test_bytes_are_canonical_ascii_json_with_explicit_boundaries() -> None:
    data = make().to_bytes()
    assert data.isascii()
    assert data == make().to_bytes()
    payload = json.loads(data)
    assert payload["extraction_hash"] == H
    first = payload["chunks"][0]
    assert first["text"] == "Clause é"
    assert first["segments"] == [[0, 0, 8]]
    assert first["section_path"] == ["Art 1"]
    assert first["char_count"] == 8
    assert first["text_hash"] == chunk(0).text_hash
    assert [c["ordinal"] for c in payload["chunks"]] == [0, 1]


def test_ids_are_stable_and_depend_on_every_provenance_part() -> None:
    base = make().chunk_id(0)
    assert base == make().chunk_id(0)
    assert len({base, make().chunk_id(1), make(T2).chunk_id(0), make(h="b" * 64).chunk_id(0)}) == 4
    assert make(version="chunk-2").chunk_id(0) != base


def test_a_set_must_be_non_empty_and_numbered_from_zero() -> None:
    with pytest.raises(ValidationError):
        ChunkSet(T1, DOC, VER, H, "v", ())
    with pytest.raises(ValidationError):
        ChunkSet(T1, DOC, VER, H, "v", (chunk(1),))
    with pytest.raises(ValidationError):
        ChunkSet(T1, DOC, VER, "nothex", "v", (chunk(0),))


@pytest.mark.parametrize(
    "build",
    [
        lambda: Segment(-1, 0, 1),
        lambda: Segment(0, 3, 3),
        lambda: Chunk(0, "", (Segment(0, 0, 1),), (), None, None),
        lambda: Chunk(0, "x", (), (), None, None),
        lambda: Chunk(0, "x", (Segment(0, 0, 1),), (), 1, None),
        lambda: Chunk(0, "x", (Segment(0, 0, 1),), (), 3, 2),
    ],
)
def test_invalid_pieces_are_refused(build: object) -> None:
    with pytest.raises(ValidationError):
        build()  # type: ignore[operator]


def test_the_record_describes_the_artifact_and_holds_no_text() -> None:
    record, artifact = ChunkSetRecord.describe(make(), NOW)
    assert record.content_hash == content_hash(artifact)
    assert (record.artifact_bytes, record.chunk_count, record.character_count) == (
        len(artifact),
        2,
        14,
    )
    assert record.extraction_hash == H
    assert "Clause" not in repr(record)


@pytest.mark.parametrize(
    "change",
    [
        {"content_hash": "x"},
        {"extraction_hash": "x"},
        {"chunker_version": "bad\nversion"},
        {"chunk_count": 0},
        {"created_at": datetime(2026, 1, 1)},
    ],
)
def test_an_invalid_record_is_refused(change: dict[str, Any]) -> None:
    record, _ = ChunkSetRecord.describe(make(), NOW)
    with pytest.raises(ValidationError):
        dataclasses.replace(record, **change)


def test_reconciliation_decisions() -> None:
    recorded, _ = ChunkSetRecord.describe(make(), NOW)
    same, _ = ChunkSetRecord.describe(make(), NOW)
    other_version, _ = ChunkSetRecord.describe(make(version="chunk-2"), NOW)
    other_hash, _ = ChunkSetRecord.describe(make(h="b" * 64), NOW)
    assert reconcile(recorded, same) is Reconciliation.ADOPT
    assert reconcile(recorded, other_version) is Reconciliation.KEEP_RECORDED
    assert reconcile(recorded, other_hash) is Reconciliation.CONFLICT


def test_receipt_scope_and_failure_codes() -> None:
    receipt = PurgeReceipt(T1, DOC, NOW, (VER,))
    assert (receipt.scope, receipt.versions_purged) == (PURGE_SCOPE, 1)
    assert PURGE_SCOPE == "chunking.chunk_artifacts_and_rows"
    with pytest.raises(ValueError):
        ChunkingRejected("invented")
    assert all(ChunkingRejected(code).code == code for code in PERMANENT_CODES)


def test_the_artifact_path_is_per_document_and_version() -> None:
    assert chunks_path(DOC, VER) == f"chunking/{DOC}/{VER}/chunks"
