"""`TextEvidence`: the verified text of one generation's sources, bound to its `EvidenceSet`.

The text is content. It is `repr=False` everywhere, never persisted and never logged; the only
sink is the prompt of the one generation the evidence was built for. A `TextEvidence` is issued by
`TextEvidenceBuilder` only after the same proof and provenance checks as an `EvidenceSet`; on top of
that every passage must hash to the `text_hash` recorded in its citation, so text that is not the
stored chunk text cannot be paired with a citation. Constructing one by hand proves nothing, so
production code outside the builder must not (a pin in tests/architecture checks that).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from lda.modules.citations.domain.codes import INVALID_INPUT, PROVENANCE_MISMATCH
from lda.modules.citations.domain.evidence import EvidenceSet
from lda.shared.kernel.errors import ValidationError


def text_sha256(text: str) -> str:
    """The chunk text hash: SHA-256 over the UTF-8 bytes (the definition chunking records)."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True, slots=True)
class EvidencePassage:
    """The text of the source cited as `marker`."""

    marker: str
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class TextEvidence:
    evidence: EvidenceSet
    passages: tuple[EvidencePassage, ...] = field(repr=False)
    """One passage per citation, in marker order."""

    def __post_init__(self) -> None:
        if not isinstance(self.evidence, EvidenceSet):
            raise ValidationError(INVALID_INPUT, detail="evidence")
        if (
            not isinstance(self.passages, tuple)
            or len(self.passages) != len(self.evidence.citations)
            or not all(isinstance(p, EvidencePassage) for p in self.passages)
        ):
            raise ValidationError(INVALID_INPUT, detail="passages")
        for citation, passage in zip(self.evidence.citations, self.passages, strict=True):
            if passage.marker != citation.marker or not isinstance(passage.text, str):
                raise ValidationError(INVALID_INPUT, detail="passages")
            if not passage.text or text_sha256(passage.text) != citation.source.text_hash:
                raise ValidationError(PROVENANCE_MISMATCH)

    def passage(self, marker: str) -> EvidencePassage:
        for passage in self.passages:
            if passage.marker == marker:
                return passage
        raise ValidationError(INVALID_INPUT, detail="marker")
