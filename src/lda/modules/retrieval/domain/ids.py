from __future__ import annotations

from dataclasses import dataclass

from lda.shared.kernel.ids import EntityId


@dataclass(frozen=True, slots=True)
class DocumentId(EntityId):
    """A document, as minted by Documents. Never interpreted here."""


@dataclass(frozen=True, slots=True)
class VersionId(EntityId):
    """One immutable version of a document, as minted by Documents."""


@dataclass(frozen=True, slots=True)
class ChunkId(EntityId):
    """A chunk, as minted by Chunking. Never derived here."""
