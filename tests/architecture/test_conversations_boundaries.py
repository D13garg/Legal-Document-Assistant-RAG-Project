"""Step 15 pins: what `conversations` may touch and what it must never become (ADR-039).

Syntax-aware (AST) and text checks over the real repository, each paired with a synthetic
violation so no rule passes vacuously. They are static checks with known limits (no data flow,
evadable by dynamic imports) and complement the behavioural and PostgreSQL tests in the module.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from archguard.manifest import load_dag, load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
CM = SRC / "modules/conversations"
BOOTSTRAP_FILE = SRC / "bootstrap/conversations.py"
MIGRATION = next((CM / "infrastructure/persistence/migrations/versions").glob("n0001_*.py"))

FORBIDDEN_MODULES = frozenset(
    {
        "access_control",
        "identity",
        "documents",
        "ingestion",
        "document_processing",
        "chunking",
        "retrieval",
        "prompts",
        "ai_gateway",
        "grounded_generation",
        "answering",
        "evaluation",
        "audit",
    }
)
CORE_FORBIDDEN_ROOTS = frozenset(
    {
        "openai", "anthropic", "cohere", "google", "langchain", "boto3", "httpx", "requests",
        "urllib", "socket", "http", "sqlalchemy", "alembic", "psycopg", "redis", "pickle",
        "sqlite3", "logging", "warnings", "traceback", "random", "secrets", "time", "os", "sys",
        "pathlib", "shutil", "subprocess", "threading", "asyncio", "importlib", "functools",
        "tempfile", "pydantic",
    }
)  # fmt: skip
FORBIDDEN_NAMES = frozenset(
    {
        "GenerateText", "GenerateGroundedAnswer", "PromptRequest", "SearchIndex", "AccessScope",
        "AuthorizationProof", "AuthorizeAction", "ProofVerifier", "GetDocument", "ReadChunks",
    }
)  # fmt: skip
CACHE_NAMES = frozenset({"lru_cache", "cache", "cached_property", "memoize"})
RETENTION_WORDS = ("retention", "expire", "expiry", "ttl", "purge", "erase", "anonymi")


def production_files(*, infrastructure: bool) -> list[Path]:
    """The pure core (domain, application, api) or the persistence adapter; never tests/testing."""
    out = []
    for path in sorted(CM.rglob("*.py")):
        parts = set(path.relative_to(CM).parts)
        if parts & {"tests", "testing", "__pycache__"}:
            continue
        if ("infrastructure" in parts) == infrastructure:
            out.append(path)
    return out


def imported_names(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
            out += [f"{node.module}.{a.name}" for a in node.names]
    return out


def foreign_module_imports(tree: ast.AST) -> list[str]:
    """Anything from another module except `citations.api` (the one approved, declared edge)."""
    hits = []
    for name in imported_names(tree):
        parts = name.split(".")
        outside = parts[:2] == ["lda", "modules"] and len(parts) > 2 and parts[2] != "conversations"
        if outside and (parts[2] != "citations" or parts[3:4] != ["api"]):
            hits.append(name)
    return hits


def core_hits(tree: ast.AST) -> list[str]:
    hits = [n for n in imported_names(tree) if n.split(".")[0] in CORE_FORBIDDEN_ROOTS]
    hits += [n for n in imported_names(tree) if n.startswith(("lda.platform", "lda.bootstrap"))]
    return hits


def used_names(tree: ast.AST) -> set[str]:
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    return names


def identifiers(tree: ast.AST) -> set[str]:
    """Every name a developer chose: functions, classes, parameters, attributes, variables."""
    found: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(n.name)
        elif isinstance(n, ast.arg):
            found.add(n.arg)
        elif isinstance(n, ast.Name):
            found.add(n.id)
        elif isinstance(n, ast.Attribute):
            found.add(n.attr)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            found.add(n.target.id)
    return found


def parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


# ---- detectors catch synthetic violations (no vacuous pass) -------------------------------------
def test_the_detectors_fire_on_synthetic_violations() -> None:
    bad = ast.parse(
        "from lda.modules.documents.api import GetDocument\n"
        "from lda.modules.citations.application.ports import X\n"
        "import sqlalchemy, logging\nfrom lda.platform.database import y\n"
    )
    assert foreign_module_imports(bad) == [
        "lda.modules.documents.api",
        "lda.modules.documents.api.GetDocument",
        "lda.modules.citations.application.ports",
        "lda.modules.citations.application.ports.X",
    ]
    assert {"sqlalchemy", "logging"} <= set(core_hits(bad))
    assert "lda.platform.database" in core_hits(bad)
    good = ast.parse("from lda.modules.citations.api import GenerationRecordRepository\n")
    assert foreign_module_imports(good) == []
    assert used_names(bad) & FORBIDDEN_NAMES == {"GetDocument"}
    assert "expire_after" in identifiers(ast.parse("def expire_after(ttl): pass"))


# ---- dependencies: exactly the approved edge, through the public api ----------------------------
def test_the_manifest_declares_only_the_citations_edge_and_the_dag_is_unchanged() -> None:
    manifests, problems = load_manifests(ROOT)
    assert not problems
    declared = set(manifests["conversations"].depends_on)
    graph, dag_problems = load_dag(ROOT)
    assert not dag_problems
    assert declared == {"citations"}
    assert set(graph["conversations"]) == {"citations", "documents"}  # ADR-039: DAG not changed
    assert declared <= set(graph["conversations"])
    consumers = {name for name, deps in graph.items() if "conversations" in deps}
    assert consumers == {"answering"}  # nothing but the future Step 16 module may depend on it


def test_production_code_imports_only_the_citations_api_from_other_modules() -> None:
    files = production_files(infrastructure=False) + production_files(infrastructure=True)
    assert files
    for path in files:
        tree = parse(path)
        assert (
            foreign_module_imports(tree) == []
            or {n.split(".")[2] for n in foreign_module_imports(tree)} == set()
        ), path
        assert (
            not {n.split(".")[2] for n in imported_names(tree) if n.startswith("lda.modules.")}
            & FORBIDDEN_MODULES
        ), path


def test_only_the_use_case_that_needs_a_record_touches_citations() -> None:
    users = [
        p.relative_to(CM).as_posix()
        for p in production_files(infrastructure=False)
        if any(n.startswith("lda.modules.citations") for n in imported_names(parse(p)))
    ]
    assert users == ["application/use_cases/append_assistant_turn.py"]


def test_the_bootstrap_touches_citations_only_through_the_api() -> None:
    names = imported_names(parse(BOOTSTRAP_FILE))
    cited = {n for n in names if n.startswith("lda.modules.citations")}
    assert cited and all(n.startswith("lda.modules.citations.api") for n in cited)
    assert foreign_module_imports(parse(BOOTSTRAP_FILE)) == []


# ---- the core stays pure, generates nothing, authenticates nothing -----------------------------
def test_domain_and_application_are_framework_free() -> None:
    for path in production_files(infrastructure=False):
        if path.name == "config.py":
            continue  # the pydantic settings slice is the module's configuration boundary
        assert core_hits(parse(path)) == [], path


def test_no_generation_authorization_or_document_machinery_is_used() -> None:
    for path in production_files(infrastructure=False) + production_files(infrastructure=True):
        assert used_names(parse(path)) & FORBIDDEN_NAMES == set(), path


def test_nothing_in_the_module_holds_a_document_or_version_reference() -> None:
    """ADR-039 C3: provenance only through linked generation records."""
    for path in production_files(infrastructure=False) + production_files(infrastructure=True):
        ids = identifiers(parse(path))
        assert not {i for i in ids if re.search(r"document|version(?!s_?$)", i)}, path


def test_no_caches_logging_or_printing_exist() -> None:
    for path in production_files(infrastructure=False) + production_files(infrastructure=True):
        tree = parse(path)
        assert used_names(tree) & CACHE_NAMES == set(), path
        assert "print" not in used_names(tree), path
        assert not [n for n in imported_names(tree) if n.split(".")[0] == "logging"], path


def test_no_retention_or_erasure_machinery_exists_beyond_the_approved_delete() -> None:
    """ADR-039 C5: no time-based retention, no purge job, no erasure claim."""
    for path in production_files(infrastructure=False) + production_files(infrastructure=True):
        for ident in identifiers(parse(path)):
            assert not any(w in ident.lower() for w in RETENTION_WORDS), (path, ident)
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert not re.search(r"\b(ttl|expires?_at|retention|purge)\b", sql)


def test_every_use_case_requires_a_principal_context() -> None:
    for path in sorted((CM / "application").glob("*/*.py")):
        if path.name == "__init__.py":
            continue
        assert "require_principal(ctx)" in path.read_text(encoding="utf-8"), path


# ---- the migration enforces what the application promises --------------------------------------
def test_the_migration_forces_rls_on_every_table_and_references_nothing_outside() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("FORCE ROW LEVEL SECURITY") == 1  # inside the per-table helper...
    for table in ("conversations", "messages", "deletion_receipts"):
        assert f'"{table}"' in sql  # ...which is applied to all three tables
    schemas = set(re.findall(r"REFERENCES\s+(\w+)\.", sql))
    assert schemas <= {"conversations"}
    assert "app.tenant_id" in sql


def test_the_runtime_role_gets_only_the_privileges_the_design_needs() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    grants = re.findall(r"GRANT\s+([A-Z, ()_a-z]+?)\s+ON\s+([\w.]+)", sql)
    by_table: dict[str, set[str]] = {}
    for privileges, target in grants:
        by_table.setdefault(target, set()).update(p.strip() for p in privileges.split(","))
    assert by_table["conversations.messages"] == {"SELECT", "INSERT"}
    assert by_table["conversations.deletion_receipts"] == {"SELECT", "INSERT"}
    assert by_table["conversations.conversations"] == {
        "SELECT",
        "INSERT",
        "DELETE",
        "UPDATE (message_count)",
    }
    granted = " ".join(line for line in sql.splitlines() if "GRANT" in line)
    for forbidden in ("TRUNCATE", "ALL PRIVILEGES", "REFERENCES", "TRIGGER", "GRANT OPTION"):
        assert forbidden not in granted, forbidden


def test_the_immutability_and_consistency_triggers_are_all_declared() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for needed in (
        "_no_update BEFORE UPDATE",
        "deletion_receipts_no_delete BEFORE DELETE",
        "_no_truncate BEFORE TRUNCATE",
        "messages_delete_only_with_parent BEFORE DELETE ON conversations.messages",
        "conversations_update_guard BEFORE UPDATE",
        "CREATE CONSTRAINT TRIGGER messages_counter_consistent",
        "CREATE CONSTRAINT TRIGGER conversations_counter_consistent",
        "messages_record_unique UNIQUE (tenant_id, record_id)",
        "messages_client_message_unique",
        "messages_role_shape CHECK",
        "ON DELETE CASCADE",
    ):
        assert needed in sql, needed
    assert "text IS NULL" in sql  # an assistant row can never carry text


def test_codeowners_is_unchanged_and_does_not_route_conversations() -> None:
    """Owner decision (ADR-039 C7): consistent with Step 14, the module is left unrouted. Changing
    this is a visible, reviewed diff; no code-owner review is claimed."""
    owners = (ROOT / "CODEOWNERS").read_text(encoding="utf-8")
    assert "conversations" not in owners
