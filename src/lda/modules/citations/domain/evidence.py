"""`Citation` and `EvidenceSet`: the sources one generation may cite, under per-generation markers.

Markers (`S1`, `S2`, ...) are assigned here, in the order the verified sources were given, so a
model or client can only name a marker, never an identifier. A marker that is not in the set is
refused. An `EvidenceSet` is issued by `EvidenceBuilder` after the proof and provenance checks;
constructing one by hand proves nothing, so production code outside the builder must not (a pin
in tests/architecture checks that).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

from lda.modules.citations.domain.codes import (
    CITATION_MISSING,
    INVALID_INPUT,
    LIMIT_EXCEEDED,
    UNKNOWN_MARKER,
)
from lda.modules.citations.domain.source import CitationSource
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.ids import PrincipalId, TenantId

_MARKER = re.compile(r"S[1-9][0-9]{0,3}")
MAX_EVIDENCE = 500
FINGERPRINT_SCHEME = "citations.evidence.v1"


def marker_for(position: int) -> str:
    return f"S{position}"


@dataclass(frozen=True, slots=True)
class Citation:
    marker: str
    source: CitationSource

    def __post_init__(self) -> None:
        if not isinstance(self.marker, str) or _MARKER.fullmatch(self.marker) is None:
            raise ValidationError(INVALID_INPUT, detail="marker")
        if not isinstance(self.source, CitationSource):
            raise ValidationError(INVALID_INPUT, detail="source")


@dataclass(frozen=True, slots=True)
class EvidenceSet:
    tenant_id: TenantId
    principal_id: PrincipalId
    citations: tuple[Citation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.citations, tuple) or not 1 <= len(self.citations) <= MAX_EVIDENCE:
            raise ValidationError(INVALID_INPUT, detail="citations")
        if not isinstance(self.tenant_id, TenantId) or not isinstance(
            self.principal_id, PrincipalId
        ):
            raise ValidationError(INVALID_INPUT, detail="owner")
        expected = [marker_for(i) for i in range(1, len(self.citations) + 1)]
        chunks = {c.source.chunk_id for c in self.citations if isinstance(c, Citation)}
        if (
            not all(isinstance(c, Citation) for c in self.citations)
            or [c.marker for c in self.citations] != expected
            or len(chunks) != len(self.citations)
        ):
            raise ValidationError(INVALID_INPUT, detail="citations")

    def fingerprint(self) -> str:
        """SHA-256 over identifiers and chunk hashes; content-derived, so keep it out of logs and
        client responses (it may be persisted in a `GenerationRecord`)."""
        digest = hashlib.sha256(FINGERPRINT_SCHEME.encode("ascii") + b"\x00")
        for part in (str(self.tenant_id), str(self.principal_id)):
            digest.update(part.encode("ascii") + b"\x00")
        for c in self.citations:
            s = c.source
            parts = [c.marker, str(s.document_id), str(s.version_id), str(s.chunk_id)]
            parts += [str(s.ordinal), s.text_hash, str(s.page_start), str(s.page_end)]
            digest.update("\x1f".join(parts).encode("ascii") + b"\x00")
        return digest.hexdigest()

    def contains(self, citation: Citation) -> bool:
        return isinstance(citation, Citation) and citation in self.citations

    def resolve(
        self, markers: Iterable[str], *, max_markers: int, allow_empty: bool = False
    ) -> tuple[Citation, ...]:
        """Map markers named by validated output to citations: unknown markers are refused, a
        repeated marker counts once, order of first use is kept. No marker at all is refused unless
        `allow_empty` (a non-answer cites nothing)."""
        named = list(markers)
        if len(named) > max_markers:
            raise ValidationError(LIMIT_EXCEEDED, detail="max_markers")
        by_marker = {c.marker: c for c in self.citations}
        found: dict[str, Citation] = {}
        for marker in named:
            citation = by_marker.get(marker) if isinstance(marker, str) else None
            if citation is None:
                raise ValidationError(UNKNOWN_MARKER)
            found.setdefault(marker, citation)
        if not found and not allow_empty:
            raise ValidationError(CITATION_MISSING)
        return tuple(found.values())
