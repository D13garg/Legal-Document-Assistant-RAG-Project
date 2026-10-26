from __future__ import annotations

from dataclasses import dataclass

from lda.shared.kernel.ids import EntityId


@dataclass(frozen=True, slots=True)
class DocumentId(EntityId):
    """A document: the unit that is authorized, tombstoned and purged (ADR-015)."""


@dataclass(frozen=True, slots=True)
class DocumentVersionId(EntityId):
    """One immutable upload of a document."""
