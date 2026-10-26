"""ADR-043: the development operator is reachable only from its own one-shot command."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = "lda.bootstrap.dev_operator"
ALLOWED_IMPORTERS = {
    "apps/dev_bootstrap.py",
    "tests/platform/test_dev_operator.py",
    "src/lda/bootstrap/development.py",  # ADR-045: the shared development guard only
    "tests/platform/test_development_adapters.py",
    "src/lda/bootstrap/headless.py",  # ADR-045: the development slice driver
}


def _importers() -> set[str]:
    found: set[str] = set()
    for base in ("src", "apps", "tests"):
        for path in (ROOT / base).rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel == "src/lda/bootstrap/dev_operator.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
                elif isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                if any(n == TARGET or n.startswith(TARGET + ".") for n in names):
                    found.add(rel)
    return found


def test_only_the_one_shot_command_and_its_tests_import_the_dev_operator() -> None:
    assert _importers() <= ALLOWED_IMPORTERS, sorted(_importers() - ALLOWED_IMPORTERS)


def test_the_dev_operator_module_never_names_an_admin_url_or_a_second_grant_surface() -> None:
    source = (ROOT / "src/lda/bootstrap/dev_operator.py").read_text(encoding="utf-8")
    assert "admin_url" not in source and "ADMIN" not in source
    assert source.count("GrantAccessCommand(") == 1  # the single, fixed-shape grant
