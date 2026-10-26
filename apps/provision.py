"""One-shot, non-destructive database provisioning (ADR-044): `python -m apps.provision`.

A thin shell: the logic lives in `lda.bootstrap.provisioning.run_command`. It needs an existing,
empty-or-managed database. It never drops, resets or truncates anything.
"""

from __future__ import annotations

from lda.bootstrap.provisioning import run_command
from lda.platform.configuration.loader import ConfigurationLoader

if __name__ == "__main__":
    raise SystemExit(run_command(ConfigurationLoader.from_environment()))
