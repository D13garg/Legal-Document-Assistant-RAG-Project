"""Fakes for tests of this module and of modules that depend on it (never imported by production
code)."""

from lda.modules.ingestion.testing.fakes import ScriptedScanner, ScriptedStage
from lda.modules.ingestion.testing.in_memory import (
    InMemoryIngestionStore,
    InMemoryIngestionUnitOfWork,
    InMemoryIngestionUnitOfWorkFactory,
)

__all__ = [
    "InMemoryIngestionStore",
    "InMemoryIngestionUnitOfWork",
    "InMemoryIngestionUnitOfWorkFactory",
    "ScriptedScanner",
    "ScriptedStage",
]
