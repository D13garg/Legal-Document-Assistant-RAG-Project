"""Source discovery, zone classification and import extraction."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from archguard import policy
from archguard.model import Violation

KNOWN_LAYERS = frozenset({*policy.MODULE_LAYERS, *policy.MODULE_SURFACES, "tests", "config"})


@dataclass(frozen=True)
class Zone:
    kind: str  # kernel|ports|platform|bootstrap|apps|tests_top|module|modules_pkg|lda_other|other
    module: str = ""
    layer: str = ""

    @property
    def is_test(self) -> bool:
        return self.kind == "tests_top" or (self.kind == "module" and self.layer == "tests")


@dataclass(frozen=True)
class ImportRef:
    line: int
    module: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class SourceFile:
    rel: str
    tree: ast.Module
    zone: Zone
    imports: tuple[ImportRef, ...]


def _module_zone(parts: list[str]) -> Zone:
    if not parts:
        return Zone("modules_pkg")
    name = parts[0]
    if len(parts) == 1:
        return Zone("module", name, "root")
    seg = parts[1].removesuffix(".py")
    if seg == "__init__":
        return Zone("module", name, "root")
    return Zone("module", name, seg if seg in KNOWN_LAYERS else "unknown")


def _classify_parts(parts: list[str]) -> Zone:
    if not parts:
        return Zone("lda_other")
    head = parts[0]
    if head == "modules":
        return _module_zone(parts[1:])
    if head == "shared" and len(parts) > 1 and parts[1].removesuffix(".py") in ("kernel", "ports"):
        return Zone(parts[1].removesuffix(".py"))
    if head == "platform":
        return Zone("platform", layer=parts[1].removesuffix(".py") if len(parts) > 1 else "")
    if head == "bootstrap":
        return Zone(head)
    return Zone("lda_other")


def classify_path(rel: str) -> Zone:
    parts = rel.split("/")
    if parts[:2] == ["src", "lda"]:
        return _classify_parts(parts[2:])
    if parts[0] == "apps":
        return Zone("apps")
    if parts[0] == "tests":
        return Zone("tests_top")
    return Zone("other")


def classify_dotted(dotted: str) -> Zone | None:
    parts = dotted.split(".")
    if parts[0] == "lda":
        return _classify_parts(parts[1:])
    if parts[0] == "apps":
        return Zone("apps")
    if parts[0] == "tests":
        return Zone("tests_top")
    return None


def dotted_for(rel: str) -> tuple[str, bool]:
    path = rel.removeprefix("src/").removesuffix(".py")
    is_pkg = path.endswith("/__init__")
    path = path.removesuffix("/__init__")
    return path.replace("/", "."), is_pkg


def collect_imports(tree: ast.Module, dotted: str, is_pkg: bool) -> list[ImportRef]:
    refs: list[ImportRef] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            refs.extend(ImportRef(node.lineno, a.name, ()) for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                pkg = dotted if is_pkg else dotted.rpartition(".")[0]
                base = pkg.split(".") if pkg else []
                up = node.level - 1
                if up > len(base):
                    continue
                base = base[: len(base) - up]
                prefix = ".".join(base)
                module = f"{prefix}.{node.module}" if node.module else prefix
            else:
                module = node.module or ""
            refs.append(ImportRef(node.lineno, module, tuple(a.name for a in node.names)))
    return refs


def load_sources(root: Path) -> tuple[list[SourceFile], list[Violation]]:
    files: list[SourceFile] = []
    problems: list[Violation] = []
    for scan_root in policy.SCAN_ROOTS:
        base = root / scan_root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(root).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            except SyntaxError as exc:
                problems.append(
                    Violation("AG000", rel, exc.lineno or 0, f"syntax error: {exc.msg}")
                )
                continue
            dotted, is_pkg = dotted_for(rel)
            files.append(
                SourceFile(
                    rel, tree, classify_path(rel), tuple(collect_imports(tree, dotted, is_pkg))
                )
            )
    return files, problems
