"""DEVELOPMENT ONLY one-shot command (ADR-043): `python -m apps.dev_bootstrap`.

A thin shell: the logic lives in `lda.bootstrap.dev_operator.run_command`. Never run by the
worker.
"""

from __future__ import annotations

from lda.bootstrap.dev_operator import run_command
from lda.platform.configuration.loader import ConfigurationLoader

if __name__ == "__main__":
    raise SystemExit(run_command(ConfigurationLoader.from_environment()))
