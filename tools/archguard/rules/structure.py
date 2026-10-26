"""AG001 module structure, AG003 conformance with the approved DAG."""

from __future__ import annotations

import ast
from pathlib import Path

from archguard import policy
from archguard.manifest import Manifest, module_dirs
from archguard.model import Violation

_ALLOWED_DIRS = {*policy.MODULE_LAYERS, *policy.MODULE_SURFACES, "tests", "__pycache__"}


def _is_empty_module(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return all(isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant) for n in tree.body)


def check_structure(root: Path, manifests: dict[str, Manifest]) -> list[Violation]:
    out: list[Violation] = []
    for mdir in module_dirs(root):
        base = f"{policy.MODULES_ROOT}/{mdir.name}"

        def bad(msg: str, rel: str = base) -> None:
            out.append(Violation("AG001", rel, 0, msg))

        for required in (
            "module.toml",
            "README.md",
            "api.lock",
            "api/__init__.py",
            "testing/__init__.py",
            "__init__.py",
        ):
            if not (mdir / required).exists():
                bad(f"missing required {required}")
        if (mdir / "__init__.py").exists() and not _is_empty_module(mdir / "__init__.py"):
            bad("module root __init__.py must be empty (public surface is api/)")
        if not (mdir / "tests").is_dir():
            bad("missing tests/ directory")
        present_layers = set()
        for entry in sorted(mdir.iterdir()):
            if entry.is_dir():
                if entry.name not in _ALLOWED_DIRS:
                    bad(f"unknown directory '{entry.name}/': code lives in a layer (ADR-002)")
                    continue
                if entry.name in policy.MODULE_LAYERS:
                    present_layers.add(entry.name)
                if (
                    entry.name not in ("tests", "__pycache__")
                    and not (entry / "__init__.py").exists()
                ):
                    bad(f"{entry.name}/ must be a package (__init__.py)")
            elif entry.name not in policy.MODULE_ALLOWED_FILES:
                bad(f"unexpected file '{entry.name}' at module root")
        manifest = manifests.get(mdir.name)
        if manifest and set(manifest.layers) != present_layers:
            bad(
                f"module.toml layers {sorted(manifest.layers)} != directories {sorted(present_layers)}",
                manifest.path,
            )
    return out


def check_dag_conformance(
    manifests: dict[str, Manifest], dag: dict[str, tuple[str, ...]]
) -> list[Violation]:
    out: list[Violation] = []
    for name, m in manifests.items():
        if name not in dag:
            out.append(
                Violation(
                    "AG003",
                    m.path,
                    0,
                    f"'{name}' is not an approved context (needs ADR + module-dag.toml)",
                )
            )
            continue
        for dep in m.depends_on:
            if dep not in dag[name]:
                out.append(
                    Violation(
                        "AG003", m.path, 0, f"edge {name} -> {dep} is not in the approved DAG"
                    )
                )
    return out
