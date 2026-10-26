from __future__ import annotations

import datetime as dt
from pathlib import Path

from archguard import adr, apilock, exceptions, importlinter, policy
from archguard.manifest import load_dag, load_manifests, module_dirs
from archguard.model import Violation
from archguard.rules import design, imports, isolation, structure
from archguard.source import load_sources


def platform_parts(root: Path) -> list[str]:
    """Every `lda.platform.<part>` except `testing`, discovered so a new part is covered at once."""
    base = root / "src" / "lda" / "platform"
    if not base.is_dir():
        return []
    names = {
        e.name.removesuffix(".py")
        for e in base.iterdir()
        if (e.is_dir() and (e / "__init__.py").exists())
        or (e.suffix == ".py" and e.name != "__init__.py")
    }
    return sorted(names - {"testing"})


def generated_importlinter(root: Path) -> str:
    manifests, _ = load_manifests(root)
    dag, _ = load_dag(root)
    return importlinter.generate(manifests, dag, platform_parts(root))


def run_all(root: Path, today: dt.date | None = None) -> list[Violation]:
    today = today or dt.date.today()
    files, found = load_sources(root)
    manifests, problems = load_manifests(root)
    dag, dag_problems = load_dag(root)
    found += problems + dag_problems
    found += structure.check_structure(root, manifests)
    found += structure.check_dag_conformance(manifests, dag)
    found += imports.check_imports(files, manifests)
    for sf in files:
        found += imports.check_config_access(sf)
        found += imports.check_restricted_constructors(sf)
        found += design.check_naming(sf)
        found += design.check_class_metrics(sf)
        found += design.check_use_cases(sf)
        found += design.check_audit_emission(sf)
        found += design.check_marker_placement(sf)
        found += isolation.check_schema_isolation(sf)
        found += isolation.check_repository_instantiation(sf)
    found += design.check_inheritance(files)
    found += apilock.check_api_locks(root, [d.name for d in module_dirs(root)])
    found += adr.lint_adrs(root)
    lint_file = root / policy.IMPORTLINTER_FILE
    expected = importlinter.generate(manifests, dag, platform_parts(root))
    if not lint_file.exists() or lint_file.read_text(encoding="utf-8") != expected:
        found.append(
            Violation(
                "AG004",
                policy.IMPORTLINTER_FILE,
                0,
                "stale; run `python -m archguard gen-importlinter`",
            )
        )
    waivers, waiver_problems = exceptions.load_waivers(root, today)
    return sorted(exceptions.apply_waivers(found, waivers) + waiver_problems)
