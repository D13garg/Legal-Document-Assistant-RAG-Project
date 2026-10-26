"""Development services in docker/docker-compose.yml must publish ports on loopback only.

The database and object store use known development-only credentials, so publishing them on all
interfaces (`"5432:5432"` binds 0.0.0.0) would expose them to the local network.

Scope: static text of the Compose file only. It does not prove Docker was run or that the binding
takes effect. It fails closed on any `ports:` entry it cannot parse, rather than passing silently.
"""

from __future__ import annotations

import re
from pathlib import Path

COMPOSE = Path(__file__).resolve().parents[2] / "docker" / "docker-compose.yml"
FLOW_PORTS = re.compile(r"^\s*ports:\s*\[(?P<items>[^\]]*)\]\s*(?:#.*)?$")
ANY_PORTS = re.compile(r"^\s*ports:")


def _published() -> list[str]:
    entries: list[str] = []
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#") or not ANY_PORTS.match(line):
            continue
        match = FLOW_PORTS.match(line)
        assert match, f"unparsable ports entry (use flow style or extend this test): {line!r}"
        entries += [i.strip().strip("\"'") for i in match["items"].split(",") if i.strip()]
    return entries


def test_compose_publishes_at_least_one_port() -> None:
    assert _published(), "parser found no ports: the guard would pass vacuously"


def test_every_published_port_binds_loopback() -> None:
    open_binds = [p for p in _published() if not p.startswith("127.0.0.1:")]
    assert not open_binds, f"ports not bound to 127.0.0.1: {open_binds}"
