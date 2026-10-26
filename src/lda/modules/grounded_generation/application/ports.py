"""Ports of grounded generation (ADR-029): a fresh decision in, one transaction out."""

from __future__ import annotations

from typing import Protocol

from lda.modules.citations.api import CitedDocumentId, GenerationRecordRepository
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.unit_of_work import UnitOfWork


class DocumentAccess(Protocol):
    def authorize_read(
        self, ctx: TenantContext, documents: frozenset[CitedDocumentId]
    ) -> Sensitive[str]:
        """A fresh decision: the proof token for exactly `documents.read_document` over exactly
        `documents`, for `ctx`'s tenant and principal.

        Raises `AuthorizationError` unless the caller holds the action on EVERY document (there is
        no partial grant and no reason is revealed). The token is a bearer credential: it never
        leaves the call that asked for it.
        """
        ...


class GroundedUnitOfWork(UnitOfWork, Protocol):
    """One tenant-bound transaction in which the generation record and its audit fact commit
    together."""

    @property
    def records(self) -> GenerationRecordRepository: ...


class GroundedUnitOfWorkFactory(Protocol):
    def begin(self, ctx: TenantContext) -> GroundedUnitOfWork: ...
