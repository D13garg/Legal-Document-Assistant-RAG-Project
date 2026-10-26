"""`.env.example` is documented as "load it into your shell" (headless-slice.md step 2).

An unquoted JSON value with spaces breaks that: the shell runs the second word as a command and the
variable ends up empty. Scope: POSIX `sh` parsing of the example file and JSON validity of the
queue map; it does not prove any service accepts the values.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


def _source(variable: str) -> subprocess.CompletedProcess[str]:
    script = f'set -a; . "{ENV_EXAMPLE}"; set +a; printf %s "${{{variable}}}"'
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True, check=False)


def test_sourcing_the_example_emits_no_shell_errors() -> None:
    result = _source("LDA_JOBS__QUEUES")
    assert result.returncode == 0
    assert result.stderr == "", result.stderr


def test_the_queue_map_survives_shell_loading_as_the_required_json() -> None:
    value = _source("LDA_JOBS__QUEUES").stdout
    assert json.loads(value) == {"documents": 1, "events": 1, "ingestion": 1}
