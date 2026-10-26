from __future__ import annotations

from dataclasses import dataclass

from lda.shared.kernel.ids import EntityId


@dataclass(frozen=True, slots=True)
class IngestionRunId(EntityId):
    """One run (one generation) of the pipeline for one document version. Deterministic."""


@dataclass(frozen=True, slots=True)
class DocumentRef(EntityId):
    """A document, as an opaque identifier minted by Documents. Ingestion never interprets it."""


@dataclass(frozen=True, slots=True)
class VersionRef(EntityId):
    """A document version, as an opaque identifier minted by Documents."""
