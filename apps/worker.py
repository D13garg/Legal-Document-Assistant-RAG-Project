"""The worker process (ADR-044): `python -m apps.worker`.

A thin shell: the logic lives in `lda.bootstrap.worker.run_worker`. Requires
`LDA_WORKER__ADAPTER_SET`; with no registered adapter set it refuses (exit 2).
"""

from __future__ import annotations

from lda.bootstrap.worker import run_worker
from lda.platform.configuration.loader import ConfigurationLoader

if __name__ == "__main__":
    raise SystemExit(run_worker(ConfigurationLoader.from_environment()))
