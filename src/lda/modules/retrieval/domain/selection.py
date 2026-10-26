"""Which version of a document is searchable (ADR-008, ADR-015).

Only `ready` versions are eligible; of those, only the highest-numbered one is searched. The number
and the status both come from Documents on every search: nothing here stores or caches them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from lda.modules.retrieval.domain.ids import ChunkId, DocumentId, VersionId
from lda.shared.kernel.errors import ConflictError


@dataclass(frozen=True, slots=True)
class VersionFact:
    version_id: VersionId
    number: int
    ready: bool


@dataclass(frozen=True, slots=True)
class Candidate:
    """One nearest-neighbour row, before the live eligibility check."""

    document_id: DocumentId
    version_id: VersionId
    chunk_id: ChunkId
    ordinal: int
    text_hash: str
    similarity: float


VERSION_NUMBERS_AMBIGUOUS = "version_numbers_ambiguous"


def current_version(facts: Iterable[VersionFact]) -> VersionFact | None:
    """The highest-numbered ready version, or None when none is ready. Two ready versions with one
    number would mean Documents is inconsistent: refuse rather than pick one."""
    ready = [f for f in facts if f.ready]
    if not ready:
        return None
    top = max(f.number for f in ready)
    winners = [f for f in ready if f.number == top]
    if len(winners) != 1:
        raise ConflictError(VERSION_NUMBERS_AMBIGUOUS)
    return winners[0]


def keep_current(
    candidates: Iterable[Candidate], current: Mapping[DocumentId, VersionFact | None]
) -> list[Candidate]:
    """Candidates of the current version of their document, in the order given. A document with
    no decision, or no current version, contributes nothing (fail closed)."""
    return [
        c
        for c in candidates
        if (fact := current.get(c.document_id)) is not None and fact.version_id == c.version_id
    ]
