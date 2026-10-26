"""Identifiers of cited material, as minted by Documents. Opaque here; never interpreted."""

from __future__ import annotations

from dataclasses import dataclass

from lda.shared.kernel.ids import EntityId


@dataclass(frozen=True, slots=True)
class CitedDocumentId(EntityId):
    """A document, as an identifier minted by Documents."""


@dataclass(frozen=True, slots=True)
class CitedVersionId(EntityId):
    """One immutable version of a document, as minted by Documents."""
