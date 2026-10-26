"""Deterministic test doubles: a fake embedder, a fake proof verifier and a fake chunk source.

The fake embedder is the ONLY embedder in Step 11. It runs in-process, never opens a socket and
needs no credentials: embedding egress has no approved authorization rule yet (ADR-007).
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence

from lda.modules.retrieval.application.source import SourceChunk, SourceChunks
from lda.modules.retrieval.domain.embedding import EMBEDDING_DIMENSIONS, EmbeddingIdentity
from lda.modules.retrieval.domain.failures import (
    CHUNKS_UNAVAILABLE,
    RetrievalRejected,
)
from lda.modules.retrieval.domain.ids import ChunkId, DocumentId, VersionId
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import AuthorizationError, InfrastructureError
from lda.shared.ports.authorization import ProofExpectation

FAKE_IDENTITY = EmbeddingIdentity("fake", "hashed-bag-of-words", "1")
_WORD = re.compile(r"[a-z0-9]+")


class FakeEmbedder:
    """Hashed bag of words: equal words share dimensions, so ranking tests mean something. The
    same text always gives the same unit vector, in every process (SHA-256, no `hash()`)."""

    def __init__(self, identity: EmbeddingIdentity = FAKE_IDENTITY) -> None:
        self._identity = identity
        self.query_override: Sequence[float] | None = None
        self.document_calls = 0
        self.query_calls = 0
        self.fail_with: Exception | None = None
        self.corrupt = False

    @property
    def identity(self) -> EmbeddingIdentity:
        return self._identity

    def switch_identity(self, identity: EmbeddingIdentity) -> None:
        """A different model or model version: a different index (ADR-007)."""
        self._identity = identity

    def embed_documents(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        self.document_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        out = [self._vector(t) for t in texts]
        return out[:-1] if self.corrupt and out else out

    def embed_query(self, text: str) -> Sequence[float]:
        self.query_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        if self.query_override is not None:
            return self.query_override
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        values = [0.0] * EMBEDDING_DIMENSIONS
        words = _WORD.findall(text.lower()) or [text]
        for word in words:
            digest = hashlib.sha256(word.encode("utf-8")).digest()
            values[int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS] += (
                1.0 if digest[4] % 2 == 0 else -1.0
            )
        norm = math.sqrt(sum(v * v for v in values))
        if norm == 0.0:  # opposite words cancelled: fall back to one fixed direction
            values[int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "big") % 256] = 1.0
            return values
        return [v / norm for v in values]


class FakeProofVerifier:
    """Accepts exactly the `(token, expectation)` pairs registered, until revoked or expired."""

    def __init__(self) -> None:
        self._granted: dict[str, ProofExpectation] = {}
        self.verified = 0
        self.fail_after: int | None = None  # the n-th call and later fail as `proof_stale`
        self.unavailable = False

    def grant(self, token: str, expected: ProofExpectation) -> None:
        self._granted[token] = expected

    def revoke(self, token: str) -> None:
        self._granted.pop(token, None)

    def verify(self, token: str, expected: ProofExpectation) -> None:
        self.verified += 1
        if self.unavailable:
            raise InfrastructureError("proof_verifier_unavailable")
        if self.fail_after is not None and self.verified > self.fail_after:
            raise AuthorizationError("proof_stale")
        if self._granted.get(token) != expected:
            raise AuthorizationError("proof_mismatch")


class FakeChunkSource:
    """Chunk text per version, set by the test. Unknown versions are refused like the real one."""

    def __init__(self) -> None:
        self._sets: dict[tuple[DocumentId, VersionId], SourceChunks] = {}
        self.reads = 0

    def put(self, document_id: DocumentId, version_id: VersionId, texts: Sequence[str]) -> None:
        chunks = tuple(
            SourceChunk(
                ChunkId(_uuid(f"{version_id}/{i}")),
                i,
                text,
                hashlib.sha256(text.encode()).hexdigest(),
            )
            for i, text in enumerate(texts)
        )
        digest = hashlib.sha256("|".join(c.text_hash for c in chunks).encode()).hexdigest()
        self._sets[(document_id, version_id)] = SourceChunks("fake-chunker-1", digest, chunks)

    def read(
        self, ctx: SystemContext, document_id: DocumentId, version_id: VersionId
    ) -> SourceChunks:
        self.reads += 1
        found = self._sets.get((document_id, version_id))
        if found is None:
            raise RetrievalRejected(CHUNKS_UNAVAILABLE)
        return found

    def forget(self, document_id: DocumentId, version_id: VersionId) -> None:
        """Chunking no longer has this version's chunk set."""
        self._sets.pop((document_id, version_id), None)

    def known_versions(self) -> list[tuple[DocumentId, VersionId]]:
        return list(self._sets)


def _uuid(name: str):  # type: ignore[no-untyped-def]
    from uuid import UUID

    return UUID(hashlib.sha256(name.encode()).hexdigest()[:32])
