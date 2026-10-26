"""module.toml manifests and the approved module DAG."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from archguard import policy
from archguard.model import Violation

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Manifest:
    name: str
    summary: str
    depends_on: tuple[str, ...]
    owns_schema: str
    layers: tuple[str, ...]
    path: str


def module_dirs(root: Path) -> list[Path]:
    base = root / policy.MODULES_ROOT
    if not base.exists():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))


def _str_list(value: object) -> list[str] | None:
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def load_manifests(root: Path) -> tuple[dict[str, Manifest], list[Violation]]:
    manifests: dict[str, Manifest] = {}
    problems: list[Violation] = []
    for mdir in module_dirs(root):
        rel = f"{policy.MODULES_ROOT}/{mdir.name}/module.toml"
        file = mdir / "module.toml"
        if not file.exists():
            continue  # reported by the structure rule (AG001)
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            problems.append(Violation("AG002", rel, 0, f"invalid TOML: {exc}"))
            continue
        section = data.get("module", {})
        deps = _str_list(data.get("dependencies", {}).get("depends_on"))
        layers = _str_list(section.get("layers"))
        schema = section.get("owns_schema", "")
        errors: list[str] = []
        if section.get("name") != mdir.name:
            errors.append(f"[module].name must equal directory name '{mdir.name}'")
        if not isinstance(section.get("summary"), str) or not section["summary"].strip():
            errors.append("[module].summary is required")
        if deps is None:
            errors.append("[dependencies].depends_on must be a list of module names")
        if layers is None or not set(layers) <= set(policy.MODULE_LAYERS):
            errors.append(f"[module].layers must be a subset of {list(policy.MODULE_LAYERS)}")
        if not isinstance(schema, str) or (
            schema and (schema != mdir.name or not _NAME.match(schema))
        ):
            errors.append("[module].owns_schema must be empty or equal the module name (ADR-027)")
        if deps is not None and mdir.name in deps:
            errors.append("module cannot depend on itself")
        if errors:
            problems.extend(Violation("AG002", rel, 0, e) for e in errors)
            continue
        assert deps is not None and layers is not None  # noqa: S101 - narrowed by errors check
        manifests[mdir.name] = Manifest(
            mdir.name, section["summary"], tuple(deps), schema, tuple(layers), rel
        )
    return manifests, problems


def find_cycle(graph: dict[str, tuple[str, ...]]) -> list[str] | None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, ()):
            if state.get(nxt, 0) == 1:
                return [*stack[stack.index(nxt) :], nxt]
            if state.get(nxt, 0) == 0:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for n in sorted(graph):
        if state.get(n, 0) == 0:
            found = visit(n)
            if found:
                return found
    return None


def load_dag(root: Path) -> tuple[dict[str, tuple[str, ...]], list[Violation]]:
    file = root / policy.DAG_FILE
    if not file.exists():
        return {}, [Violation("AG003", policy.DAG_FILE, 0, "approved module DAG file is missing")]
    data = tomllib.loads(file.read_text(encoding="utf-8"))
    graph: dict[str, tuple[str, ...]] = {}
    problems: list[Violation] = []
    for name, spec in data.get("modules", {}).items():
        graph[name] = tuple(spec.get("depends_on", []))
    for name, deps in graph.items():
        for dep in deps:
            if dep not in graph:
                problems.append(Violation("AG003", policy.DAG_FILE, 0, f"{name} -> unknown {dep}"))
    cycle = find_cycle(graph)
    if cycle:
        problems.append(Violation("AG003", policy.DAG_FILE, 0, "cycle: " + " -> ".join(cycle)))
    return graph, problems


def topological_order(graph: dict[str, tuple[str, ...]], present: set[str]) -> list[str]:
    """Dependents first, dependencies last (top-to-bottom for an import-linter layers contract)."""
    sub = {n: tuple(d for d in graph.get(n, ()) if d in present) for n in sorted(present)}
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(n: str) -> None:
        if n in seen:
            return
        seen.add(n)
        for d in sub[n]:
            visit(d)
        ordered.append(n)  # dependencies appended before dependents

    for n in sub:
        visit(n)
    return list(reversed(ordered))
