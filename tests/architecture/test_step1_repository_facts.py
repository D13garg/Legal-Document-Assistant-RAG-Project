"""Independent facts about the real repository (do not rely on archguard's own rule engine)."""

from __future__ import annotations

import ast
from fnmatch import fnmatch
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "lda"
SDK_HOMES = {
    "boto3": "platform/storage",
    "botocore": "platform/storage",
    "redis": "platform/ratelimit",
    "sqlalchemy": "platform/database",
    "alembic": "platform/database",
    "psycopg": "platform/database",
}
# Mirrors policy exactly. AG031: DB drivers are allowed in a module's infrastructure/persistence and
# in test zones (modules/*/tests, exempt from external-import checks); NOT in a module's testing/.
# AG020: lda.platform.testing doubles are importable by tests and by a module's testing/ only.
MODULE_PERSISTENCE = "modules/*/infrastructure/persistence/*"
MODULE_TESTS = "modules/*/tests/*"
DOUBLE_CONSUMERS = ("modules/*/tests/*", "modules/*/testing/*")
DB_SDKS = {"sqlalchemy", "alembic", "psycopg"}
CLIENT_FACTORIES = {"client", "Redis", "from_url", "create_engine"}


def modules() -> list[tuple[str, ast.Module]]:
    return [
        (p.relative_to(SRC).as_posix(), ast.parse(p.read_text(encoding="utf-8")))
        for p in sorted(SRC.rglob("*.py"))
    ]


def imported(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names += [f"{node.module}.{a.name}" for a in node.names]
    return names


def test_provider_sdks_are_imported_only_by_their_home_package() -> None:
    offenders = [
        f"{rel} imports {name}"
        for rel, tree in modules()
        for name in imported(tree)
        for sdk, home in SDK_HOMES.items()
        if name.split(".")[0] == sdk
        and not rel.startswith(home + "/")
        and not (
            sdk in DB_SDKS and (fnmatch(rel, MODULE_PERSISTENCE) or fnmatch(rel, MODULE_TESTS))
        )
    ]
    assert offenders == []


def test_sdk_clients_are_constructed_only_inside_platform() -> None:
    offenders = []
    for rel, tree in modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                owner = node.func.value
                if (
                    isinstance(owner, ast.Name)
                    and owner.id in {"boto3", "redis", "sqlalchemy"}
                    and node.func.attr in CLIENT_FACTORIES
                    and not rel.startswith("platform/")
                ):
                    offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


def test_testing_doubles_are_not_imported_by_production_code() -> None:
    offenders = [
        f"{rel} imports {name}"
        for rel, tree in modules()
        for name in imported(tree)
        if name == "lda.platform.testing" or name.startswith("lda.platform.testing.")
        if not rel.startswith("platform/testing/")
        if not any(fnmatch(rel, g) for g in DOUBLE_CONSUMERS)
    ]
    assert offenders == []


def test_no_noop_audit_exists_outside_the_test_doubles() -> None:
    offenders = []
    for rel, tree in modules():
        if rel.startswith("platform/testing/"):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and any(
                t in node.name.lower() for t in ("noop", "null", "dummy")
            ):
                offenders.append(f"{rel}:{node.name}")
            if isinstance(node, ast.FunctionDef) and node.name == "record":
                body = [s for s in node.body if not isinstance(s, ast.Expr)]
                if not body and rel.startswith(("platform/", "bootstrap/")):
                    offenders.append(f"{rel}:{node.lineno} empty AuditRecorder.record")
    assert offenders == []


def test_environment_is_read_only_by_platform_configuration() -> None:
    offenders = []
    for rel, tree in modules():
        if rel.startswith("platform/configuration/"):
            continue
        for node in ast.walk(tree):
            env = {"environ", "getenv", "putenv"}
            if (
                isinstance(node, ast.Attribute)
                and node.attr in env
                and isinstance(node.value, ast.Name)
                and node.value.id == "os"
            ) or (
                isinstance(node, ast.ImportFrom)
                and node.module == "os"
                and any(a.name in env for a in node.names)
            ):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []


def test_bootstrap_is_the_only_composition_root() -> None:
    importers = [
        rel
        for rel, tree in modules()
        for name in imported(tree)
        if name == "lda.bootstrap" or name.startswith("lda.bootstrap.")
        if not rel.startswith("bootstrap/")
    ]
    assert importers == []  # nothing outside bootstrap may import (and thus wire) concrete adapters


def test_platform_contains_no_domain_vocabulary() -> None:
    forbidden = ("document", "legal", "clause", "citation", "prompt", "embedding", "retriev")
    hits = []
    for rel, tree in modules():
        if not rel.startswith(("platform/", "bootstrap/")) or rel.startswith("platform/testing/"):
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                names.append(node.name)
            if isinstance(node, ast.arg):
                names.append(node.arg)
            hits += [f"{rel}:{n}" for n in names if any(f in n.lower() for f in forbidden)]
    assert hits == []
