"""Identifier-only outcome events (ADR-010 §7, ADR-028). Published in the transaction that moves
the run to its outcome; Documents is the consumer. No content, title or object key."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from lda.modules.ingestion.domain.ids import DocumentRef, IngestionRunId, VersionRef
from lda.shared.kernel.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class IngestionCompleted(DomainEvent):
    """Scan clean and every stage of the plan succeeded: the only source of `ready`."""

    type_name: ClassVar[str] = "ingestion.IngestionCompleted"
    document: DocumentRef
    version: VersionRef
    run_id: IngestionRunId
    stage_count: int


@dataclass(frozen=True, kw_only=True)
class IngestionFailed(DomainEvent):
    type_name: ClassVar[str] = "ingestion.IngestionFailed"
    document: DocumentRef
    version: VersionRef
    run_id: IngestionRunId
    error_code: str


@dataclass(frozen=True, kw_only=True)
class IngestionQuarantined(DomainEvent):
    type_name: ClassVar[str] = "ingestion.IngestionQuarantined"
    document: DocumentRef
    version: VersionRef
    run_id: IngestionRunId
    error_code: str
