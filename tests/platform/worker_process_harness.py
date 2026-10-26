"""TEST-ONLY process harness: the real `run_worker` with test doubles for the two adapters.

Production has no adapter set yet (ADR-044); this lets a test exercise real signal handling in a
separate process. It is a test support file, never an entry point under `apps/`.
"""

from __future__ import annotations

from lda.bootstrap.worker import WorkerAdapters, run_worker
from lda.modules.ingestion.testing import ScriptedScanner
from lda.modules.retrieval.testing import FakeEmbedder
from lda.platform.configuration.loader import ConfigurationLoader

if __name__ == "__main__":
    raise SystemExit(
        run_worker(
            ConfigurationLoader.from_environment(),
            WorkerAdapters(ScriptedScanner(), FakeEmbedder()),
        )
    )
