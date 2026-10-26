"""Fakes for tests of this module and of modules that depend on it (never imported by production
code)."""

from lda.modules.retrieval.testing.doubles import (
    FAKE_IDENTITY,
    FakeChunkSource,
    FakeEmbedder,
    FakeProofVerifier,
)
from lda.modules.retrieval.testing.in_memory import (
    InMemoryRetrievalStore,
    InMemoryRetrievalUnitOfWork,
    InMemoryRetrievalUnitOfWorkFactory,
)

__all__ = [
    "FAKE_IDENTITY",
    "FakeChunkSource",
    "FakeEmbedder",
    "FakeProofVerifier",
    "InMemoryRetrievalStore",
    "InMemoryRetrievalUnitOfWork",
    "InMemoryRetrievalUnitOfWorkFactory",
]
