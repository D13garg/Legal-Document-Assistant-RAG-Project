"""The ONE approved development embedder (ADR-007 amendment, ADR-045 D3 option (a)).

A deterministic, offline, in-process signed hashed bag of words. It exists so the headless
development slice can exercise real indexing and search mechanics. It is NOT a semantic embedding
and says nothing about retrieval quality: two texts are close only when they share words.

Algorithm (version 1; change it => bump `DEVELOPMENT_EMBEDDING.version`, a NEW index per ADR-007):
  1. NFKC-normalise and casefold the text; split into runs of Unicode word characters.
  2. For each word take SHA-256(word, UTF-8): bytes 0..3 pick one of the 256 dimensions, byte 4's
     parity picks the sign; add +1 or -1 there.
  3. L2-normalise. Text with no word characters (for example "---") gets one fixed direction
     derived from the text's own hash; text whose words cancel out likewise.
  4. Return the vector only after `checked_vector` confirms 256 finite numbers of unit length.
The same text gives the same vector in every process, on every platform: no `hash()`, no random
numbers, no clock, no file, no network, no SDK, no cache, no module-level mutable state. Input
text is used only inside the call; it is not stored, logged, or put in an error.

Limits: input must be a non-blank `str` of at most `MAX_INPUT_CHARACTERS`; anything else is a
`ValidationError` with a fixed code. The caller (the index stage) still bounds batch sizes.

Selection is NOT implicit: nothing here is chosen by default. `lda.bootstrap.development` builds
it only behind the explicit development guard (ADR-045 D1).
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Sequence

from lda.modules.retrieval.domain.embedding import (
    EMBEDDING_DIMENSIONS,
    EmbeddingIdentity,
    Vector,
    checked_vector,
)
from lda.shared.kernel.errors import ValidationError

EMBEDDING_INPUT_INVALID = "embedding_input_invalid"
MAX_INPUT_CHARACTERS = 100_000
"""A defensive cap on one text. Chunk and question limits are enforced upstream and are far
smaller; this bounds the work a single call can be asked to do."""

DEVELOPMENT_EMBEDDING = EmbeddingIdentity("development", "hashed-bag-of-words", "1")

_WORD = re.compile(r"\w+")


class DevelopmentEmbedder:
    """Implements the retrieval `Embedder` port (ADR-007). Development only."""

    @property
    def identity(self) -> EmbeddingIdentity:
        return DEVELOPMENT_EMBEDDING

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if isinstance(texts, str | bytes) or not isinstance(texts, Sequence):
            raise ValidationError(EMBEDDING_INPUT_INVALID)
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> Sequence[float]:
        return self._embed(text)

    @staticmethod
    def _embed(text: str) -> Vector:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_INPUT_CHARACTERS:
            raise ValidationError(EMBEDDING_INPUT_INVALID)
        normal = unicodedata.normalize("NFKC", text).casefold()
        values = [0.0] * EMBEDDING_DIMENSIONS
        for word in _WORD.findall(normal):
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            slot = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
            values[slot] += 1.0 if digest[4] % 2 == 0 else -1.0
        norm = math.sqrt(math.fsum(v * v for v in values))
        if norm == 0.0:  # no words, or they cancelled: one fixed direction for this exact text
            fixed = hashlib.sha256(normal.encode("utf-8")).digest()
            values[int.from_bytes(fixed[:4], "big") % EMBEDDING_DIMENSIONS] = 1.0
            return checked_vector(values)
        return checked_vector([v / norm for v in values])
