"""Identity checkpoint: DAG position, isolation, purity and the absence of authorization."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from archguard.manifest import load_dag, load_manifests
from archguard.runner import run_all

ROOT = Path(__file__).resolve().parents[2]
IDENTITY = ROOT / "src/lda/modules/identity"


def sources(layer: str) -> dict[Path, ast.Module]:
    return {p: ast.parse(p.read_text(encoding="utf-8")) for p in (IDENTITY / layer).rglob("*.py")}


def imported(tree: ast.Module) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def test_the_repository_passes_every_archguard_rule_with_identity_present() -> None:
    assert run_all(ROOT) == []


def test_identity_depends_on_no_other_module() -> None:
    manifests, _ = load_manifests(ROOT)
    dag, _ = load_dag(ROOT)
    assert manifests["identity"].depends_on == () and dag["identity"] == ()
    for layer in ("domain", "application", "api", "infrastructure", "testing", "tests"):
        for path, tree in sources(layer).items():
            for name in imported(tree):
                other = re.match(r"lda\.modules\.(\w+)", name)
                assert other is None or other.group(1) == "identity", (path, name)


def test_identity_owns_exactly_the_identity_schema() -> None:
    assert load_manifests(ROOT)[0]["identity"].owns_schema == "identity"
    persistence = (IDENTITY / "infrastructure/persistence").rglob("*")
    text = "\n".join(p.read_text(encoding="utf-8") for p in persistence if p.suffix == ".py")
    schemas = set(re.findall(r"\b(?:FROM|INTO|UPDATE|JOIN|TABLE|SCHEMA)\s+([a-z_]+)\.", text))
    assert schemas <= {"identity"}


def test_domain_and_application_are_framework_free() -> None:
    allowed_third_party: set[str] = set()
    for layer in ("domain", "application"):
        for path, tree in sources(layer).items():
            for name in imported(tree):
                top = name.split(".")[0]
                assert (
                    top in {"lda", "__future__"} or top in _stdlib() or top in allowed_third_party
                ), (
                    path,
                    name,
                )
                assert not name.startswith(
                    ("sqlalchemy", "fastapi", "alembic", "psycopg", "lda.platform")
                )


def _stdlib() -> frozenset[str]:
    import sys

    return frozenset(sys.stdlib_module_names)


def test_domain_never_imports_application_api_or_infrastructure() -> None:
    for path, tree in sources("domain").items():
        for name in imported(tree):
            assert not re.match(
                r"lda\.modules\.identity\.(application|api|infrastructure)", name
            ), (
                path,
                name,
            )


def test_no_provider_sdks_or_banned_names_in_identity() -> None:
    banned_imports = ("openai", "anthropic", "boto3", "redis", "celery", "jwt", "authlib", "httpx")
    banned_names = ("Service", "Manager", "Helper", "Coordinator", "Orchestrator")
    for layer in ("domain", "application", "api", "infrastructure", "testing"):
        for path, tree in sources(layer).items():
            assert not [n for n in imported(tree) if n.split(".")[0] in banned_imports], path
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    assert not node.name.endswith(banned_names), (path, node.name)


def test_identity_makes_no_authorization_decisions() -> None:
    """Identity labels use cases; it never evaluates, grants or checks anything."""
    forbidden = re.compile(
        r"\b(authorize|is_allowed|has_permission|check_permission|AccessScope|AuthorizationProof|"
        r"Authorized|policy_epoch|acl)\b",
        re.IGNORECASE,
    )
    for layer in ("domain", "application", "api", "infrastructure", "testing"):
        for path in (IDENTITY / layer).rglob("*.py"):
            code = "\n".join(
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith(("#", '"""'))
            )
            assert forbidden.search(code) is None, path


def test_no_credentials_or_secret_fields_in_identity_state() -> None:
    forbidden = re.compile(r"password|passwd|secret|token|credential|api_key", re.IGNORECASE)
    for path, tree in sources("domain").items():
        names = [n.id for n in ast.walk(tree) if isinstance(n, ast.Name)]
        names += [n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
        names += [n.arg for n in ast.walk(tree) if isinstance(n, ast.arg)]
        assert not [n for n in names if forbidden.search(n)], path
    migration = next((IDENTITY / "infrastructure/persistence/migrations").rglob("i0001*.py"))
    assert forbidden.search(migration.read_text(encoding="utf-8")) is None


def test_every_use_case_file_declares_markers_from_the_kernel() -> None:
    for layer in ("use_cases", "queries"):
        files = [
            p for p in (IDENTITY / "application" / layer).glob("*.py") if p.name != "__init__.py"
        ]
        assert files
        for path in files:
            assert "from lda.shared.kernel.declarations import" in path.read_text(encoding="utf-8")


def test_only_the_module_unit_of_work_builds_the_repository() -> None:
    users = [
        p
        for p in IDENTITY.rglob("*.py")
        if "SqlPrincipalRepository(" in p.read_text(encoding="utf-8") and "tests" not in p.parts
    ]
    assert [p.name for p in users] == ["unit_of_work.py"]
