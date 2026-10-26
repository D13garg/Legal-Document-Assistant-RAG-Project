"""Fakes for tests of this module and of modules that depend on it (never imported by production
code)."""

from lda.modules.document_processing.testing.in_memory import (
    InMemoryProcessingStore,
    InMemoryProcessingUnitOfWork,
    InMemoryProcessingUnitOfWorkFactory,
)

__all__ = [
    "InMemoryProcessingStore",
    "InMemoryProcessingUnitOfWork",
    "InMemoryProcessingUnitOfWorkFactory",
]
