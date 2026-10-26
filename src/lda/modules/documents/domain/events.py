"""Identifier-only events (ADR-028). No titles, media types or content."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from lda.modules.documents.domain.ids import DocumentId, DocumentVersionId
from lda.shared.kernel.events import DomainEvent


@dataclass(frozen=True, kw_only=True)
class DocumentVersionAdded(DomainEvent):
    type_name: ClassVar[str] = "documents.DocumentVersionAdded"
    document_id: DocumentId
    version_id: DocumentVersionId
    version_number: int


@dataclass(frozen=True, kw_only=True)
class DocumentDeleted(DomainEvent):
    """The tombstone committed and access was revoked. Downstream modules hook here (ADR-023)."""

    type_name: ClassVar[str] = "documents.DocumentDeleted"
    document_id: DocumentId


@dataclass(frozen=True, kw_only=True)
class DocumentPurged(DomainEvent):
    """Documents' originals and rows are gone. Says nothing about other modules' data."""

    type_name: ClassVar[str] = "documents.DocumentPurged"
    document_id: DocumentId
    versions_purged: int
