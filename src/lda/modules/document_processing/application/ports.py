"""Ports of document_processing. Adapters: infrastructure (parsers, SQL) and testing (in-memory)."""

from __future__ import annotations

from typing import Protocol

from lda.modules.document_processing.domain.extraction import ExtractedDocument, ExtractionLimits
from lda.modules.document_processing.domain.ids import DocumentId, VersionId
from lda.modules.document_processing.domain.record import ExtractionRecord, PurgeReceipt
from lda.shared.kernel.context import ExecutionContext
from lda.shared.ports.jobs import JobFence
from lda.shared.ports.unit_of_work import UnitOfWork


class ContentParser(Protocol):
    @property
    def version(self) -> str:
        """Identifies the extraction behaviour (own schema plus parser library versions)."""
        ...

    def parse(self, media_type: str, content: bytes, limits: ExtractionLimits) -> ExtractedDocument:
        """Raises `ExtractionRejected` for content that can never be extracted. Any other exception
        is an infrastructure failure and is retried."""
        ...


class ExtractionRepository(Protocol):
    """Bound to one tenant for its whole life."""

    def add(self, record: ExtractionRecord) -> bool:
        """Insert if absent. False when the version already has a record (never replaced)."""
        ...

    def get(self, version_id: VersionId) -> ExtractionRecord | None: ...

    def list_for(self, document_id: DocumentId) -> tuple[ExtractionRecord, ...]: ...

    def remove_all_for(self, document_id: DocumentId) -> int: ...


class ReceiptRepository(Protocol):
    def add(self, receipt: PurgeReceipt) -> bool:
        """Insert if absent. False when a receipt exists (receipts are never replaced)."""
        ...

    def get(self, document_id: DocumentId) -> PurgeReceipt | None: ...


class ProcessingUnitOfWork(UnitOfWork, Protocol):
    @property
    def extractions(self) -> ExtractionRepository: ...

    @property
    def receipts(self) -> ReceiptRepository: ...

    @property
    def fence(self) -> JobFence: ...


class ProcessingUnitOfWorkFactory(Protocol):
    def begin(self, ctx: ExecutionContext) -> ProcessingUnitOfWork: ...
