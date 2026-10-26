"""`new-module`: the empty-module template (ADR-002)."""

from __future__ import annotations

from pathlib import Path

from archguard import apilock, policy
from archguard.manifest import Manifest, load_dag

README = """# {name}

> {summary}

## Responsibility
TODO: one paragraph. What this module owns and decides.

## Invariants
TODO: the rules this module guarantees (link to architecture.md invariants).

## Public API (`api/`)
TODO: use cases, DTOs, events other modules may use. Snapshot: `api.lock`.

## Dependencies
Declared in `module.toml`; must be a subset of `docs/architecture/module-dag.toml`.

## Owned data
Schema `{schema}` (no cross-schema reads or FKs).

## Events in / out
TODO

## Must NOT know about
TODO

## Testing in isolation
Unit and application tests use only this module's code plus `<dependency>.testing` fakes.
"""


def base_packages(root: Path) -> None:
    for p in (
        "src/lda",
        "src/lda/modules",
        "src/lda/shared",
        "src/lda/shared/kernel",
        "src/lda/shared/ports",
        "src/lda/platform",
        "src/lda/bootstrap",
    ):
        (root / p).mkdir(parents=True, exist_ok=True)
        (root / p / "__init__.py").touch()


def create_module(
    root: Path, name: str, *, presentation: bool = False, summary: str = "TODO"
) -> Manifest:
    dag, _ = load_dag(root)
    if name not in dag:
        raise SystemExit(f"'{name}' is not an approved context; add it via ADR + module-dag.toml")
    mdir = root / policy.MODULES_ROOT / name
    if mdir.exists():
        raise SystemExit(f"{mdir} already exists")
    base_packages(root)
    layers = [
        "domain",
        "application",
        "infrastructure",
        *(["presentation"] if presentation else []),
    ]
    for d in (*layers, "api", "testing"):
        (mdir / d).mkdir(parents=True)
        (mdir / d / "__init__.py").touch()
    (mdir / "__init__.py").touch()
    (mdir / "api" / "__init__.py").write_text(
        '"""Public surface of this module. The only package other modules may import."""\n\n__all__: list[str] = []\n',
        encoding="utf-8",
    )
    (mdir / "testing" / "__init__.py").write_text(
        '"""Public fakes and builders for consumers\' tests."""\n', encoding="utf-8"
    )
    for t in ("unit", "application", "integration", "contract"):
        (mdir / "tests" / t).mkdir(parents=True)
        (mdir / "tests" / t / ".gitkeep").touch()
    approved = ", ".join(dag[name]) or "none"
    layer_list = ", ".join(f'"{x}"' for x in layers)
    (mdir / "module.toml").write_text(
        f'[module]\nname = "{name}"\nsummary = "{summary}"\nowns_schema = "{name}"\nlayers = [{layer_list}]\n\n'
        f"[dependencies]\n# Declare only what is imported (AG011/AG012). Approved candidates: {approved}.\ndepends_on = []\n",
        encoding="utf-8",
    )
    (mdir / "README.md").write_text(
        README.format(name=name, summary=summary, schema=name), encoding="utf-8"
    )
    apilock.write_api_locks(root, [name])
    return Manifest(
        name, summary, (), name, tuple(layers), f"{policy.MODULES_ROOT}/{name}/module.toml"
    )
