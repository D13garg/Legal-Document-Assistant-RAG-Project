"""DEVELOPMENT ONLY headless slice driver (ADR-045): `python -m apps.dev_slice`.

A thin shell around `lda.bootstrap.headless.run_command`. A separate `apps.worker` process must
be running. With no registered development adapter set it refuses (exit 2).
"""

from __future__ import annotations

from lda.bootstrap.headless import run_command
from lda.platform.configuration.loader import ConfigurationLoader

if __name__ == "__main__":
    raise SystemExit(run_command(ConfigurationLoader.from_environment()))
