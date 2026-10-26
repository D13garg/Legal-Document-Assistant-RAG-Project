"""Public fakes for consumers' tests."""

from lda.modules.documents.testing.in_memory import (
    InMemoryDocumentsStore,
    InMemoryDocumentsUnitOfWorkFactory,
)

__all__ = ["InMemoryDocumentsStore", "InMemoryDocumentsUnitOfWorkFactory"]
