"""Step 5 checkpoint: what `documents` is, what it is not, and the edges it does not add (ADR-005,
ADR-015, ADR-023). These inspect the real repository, not fixtures."""

from __future__ import annotations

import ast
import dataclasses
import re
import tomllib
from pathlib import Path

from archguard.manifest import load_dag, load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
DOCS = SRC / "modules/documents"
MIGRATION = DOCS / "infrastructure/persistence/migrations/versions/d0001_documents.py"


def py_files(base: Path) -> list[Path]:
    return [
        p for p in base.rglob("*.py") if "__pycache__" not in p.parts and "tests" not in p.parts
    ]


def imports(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def code_only(path: Path) -> str:
    return re.sub(r'""".*?"""|#.*', "", path.read_text(encoding="utf-8"), flags=re.DOTALL)


# ---- dependency edges --------------------------------------------------------------------------


def test_documents_depends_only_on_ingestions_public_api_and_the_dag_is_unchanged() -> None:
    """Step 6 creates the documents -> ingestion edge (ADR-010) and nothing else: the edge was
    already in the DAG; Documents imports ingestion's PUBLIC API only; access_control stays out."""
    manifests, _ = load_manifests(ROOT)
    dag, _ = load_dag(ROOT)
    assert manifests["documents"].depends_on == ("ingestion",)
    assert set(dag["documents"]) == {"access_control", "ingestion"}  # the permitted ceiling, intact
    assert dag["ingestion"] == ()  # ingestion imports no module: the edge points INTO it
    seen = set()
    for path in py_files(DOCS):
        for name in imports(path):
            if name.startswith("lda.modules."):
                if name.startswith("lda.modules.ingestion"):
                    seen.add(name)
                    assert name == "lda.modules.ingestion.api", (path, name)
                else:
                    assert name.startswith("lda.modules.documents"), (path, name)
    assert seen == {"lda.modules.ingestion.api"}  # the declared edge is really used


def test_only_documents_and_bootstrap_import_ingestion() -> None:
    """Step 7 (ADR-037): document_processing implements `PipelineStage`, so it may import
    `ingestion.api`; Step 8 (ADR-038): so does chunking (the module checks in archguard AG010
    already restrict both to that surface)."""
    offenders = []
    for path in py_files(SRC):
        rel = path.relative_to(SRC)
        if rel.parts[:2] in (
            ("modules", "ingestion"),
            ("modules", "documents"),
            ("modules", "document_processing"),
            ("modules", "chunking"),
            (
                "modules",
                "retrieval",
            ),  # Step 11 (ADR-008): the `index` stage implements PipelineStage
        ):
            continue
        if rel.parts[0] == "bootstrap":
            continue
        if any(i.startswith("lda.modules.ingestion") for i in imports(path)):
            offenders.append(str(rel))
    assert offenders == []


def test_ingestion_imports_no_module_and_never_names_documents_tables_or_ids() -> None:
    ing = SRC / "modules/ingestion"
    for path in py_files(ing):
        for name in imports(path):
            if name.startswith("lda.modules."):
                assert name.startswith("lda.modules.ingestion"), (path, name)
        if "tests" in path.parts:
            continue
        assert not re.search(r"documents\.(documents|versions)\b", code_only(path)), path


def test_only_bootstrap_imports_documents() -> None:
    """Step 7 (ADR-037): document_processing reads originals through `documents.api.ReadOriginal`
    and subscribes to `DocumentPurged`; Step 8 (ADR-038) adds chunking, which subscribes to
    `DocumentPurged` too. Nothing else may import Documents."""
    offenders = []
    for path in py_files(SRC):
        rel = path.relative_to(SRC)
        if (
            rel.parts[:2]
            in (
                ("modules", "documents"),
                ("modules", "document_processing"),
                ("modules", "chunking"),
                ("modules", "retrieval"),  # Step 11 (ADR-008): live eligibility, purge event
                (
                    "modules",
                    "grounded_generation",
                ),  # Step 14 (ADR-029): DOCUMENT_KIND, READ_DOCUMENT
            )
            or rel.parts[0] == "bootstrap"
        ):
            continue
        if any(i.startswith("lda.modules.documents") for i in imports(path)):
            offenders.append(str(rel))
    assert offenders == []
    for path in py_files(SRC / "modules/grounded_generation"):
        if "tests" in path.relative_to(SRC).parts:
            continue
        assert all(
            i == "lda.modules.documents.api" or i.startswith("lda.modules.documents.api.")
            for i in imports(path)
            if i.startswith("lda.modules.documents")
        ), path.name


def test_documents_domain_is_pure() -> None:
    for path in py_files(DOCS / "domain"):
        for name in imports(path):
            assert not name.startswith(("lda.shared.ports", "lda.platform", "lda.bootstrap")), path
            assert not name.startswith(("sqlalchemy", "boto3", "pydantic")), path


def test_no_architecture_waiver_and_no_import_linter_ignore_exists() -> None:
    exceptions = tomllib.loads((ROOT / "architecture-exceptions.toml").read_text(encoding="utf-8"))
    assert not exceptions.get("waiver") and not exceptions.get("waivers")
    assert "ignore_imports" not in (ROOT / ".importlinter").read_text(encoding="utf-8")


# ---- the shared port (B1) ----------------------------------------------------------------------


def test_the_revoker_port_is_one_method_and_access_control_implements_it() -> None:
    from lda.shared.ports.access_revoker import ResourceAccessRevoker

    assert [n for n in vars(ResourceAccessRevoker) if not n.startswith("_")] == ["revoke_all"]
    tree = ast.parse(
        (SRC / "modules/access_control/infrastructure/persistence/unit_of_work.py").read_text(
            encoding="utf-8"
        )
    )
    revoker = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SqlResourceAccessRevoker"
    )
    called = {
        n.func.attr
        for n in ast.walk(revoker)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "commit" not in called and "advance" in called and "remove_all_for_resource" in called


def test_documents_reaches_access_control_only_through_the_shared_port() -> None:
    uses = []
    for path in py_files(DOCS):
        names = {n.id for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.Name)}
        if (
            any("access_control" in i for i in imports(path))
            or {
                "GrantRepository",
                "GrantAccess",
                "RevokeAccess",
            }
            & names
        ):
            uses.append(str(path.relative_to(SRC)))
    assert uses == []


# ---- what documents is not (frozen scope) ---------------------------------------------------------


def test_no_ingestion_parsing_chunking_retrieval_or_search_code_exists_in_documents() -> None:
    banned = re.compile(
        r"chunk|embedding|ingest|parse|tokeniz|extract|retriev|search|rerank|citation|ocr",
        re.IGNORECASE,
    )
    for path in py_files(DOCS):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            name = getattr(node, "name", None)
            if isinstance(node, ast.FunctionDef | ast.ClassDef | ast.AsyncFunctionDef) and name:
                assert not banned.search(name), (path, name)


READ_ORIGINAL = DOCS / "application/queries/read_original.py"


def test_documents_reads_original_bytes_only_in_read_original_and_never_hands_out_urls() -> None:
    """Step 7 (ADR-037) supersedes the Step 5 rule "Documents never reads original bytes": the
    one read path is `ReadOriginal`, which serves pending versions of active documents only. No
    other file may read an object and no file may presign a URL."""
    for path in py_files(DOCS):
        source = code_only(path)
        assert "presign_get" not in source.replace("def presign_get", ""), path
        reads = len(re.findall(r"_store\.get\(", source))
        assert reads == (1 if path == READ_ORIGINAL else 0), path


def test_the_lifecycle_is_pending_to_three_terminal_outcomes_and_active_deleted() -> None:
    from lda.modules.documents.domain.document import DocumentStatus, VersionStatus

    # Step 6 (ADR-015 amendment): one non-terminal state and three terminal outcomes
    assert {s.value for s in VersionStatus} == {"pending", "ready", "failed", "quarantined"}
    assert {s.value for s in DocumentStatus} == {"active", "deleted"}


def test_events_carry_identifiers_and_counts_only() -> None:
    from lda.modules.documents.domain import events

    allowed = {"event_id", "tenant_id", "occurred_at", "document_id", "version_id"}
    allowed |= {"version_number", "versions_purged"}
    for name in ("DocumentVersionAdded", "DocumentDeleted", "DocumentPurged"):
        fields = {f.name for f in dataclasses.fields(getattr(events, name))}
        assert fields <= allowed, (name, fields - allowed)


def test_there_is_no_per_version_acl_and_the_resource_kind_is_the_document() -> None:
    from lda.modules.documents.domain.resource import DOCUMENT_ACTIONS, DOCUMENT_KIND

    assert DOCUMENT_KIND == "document" and "version" not in DOCUMENT_KIND
    assert all(a.startswith("documents.") for a in DOCUMENT_ACTIONS)


def test_no_creator_grant_is_made_in_step_5() -> None:
    for path in py_files(DOCS):
        assert "GrantAccess" not in code_only(path), path


# ---- the migration (least privilege, immutability, isolation) -----------------------------------


def test_the_migration_grants_least_privilege_and_never_truncate() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    grants = {t: p for p, t in re.findall(r"GRANT ([A-Z, ]+) ON (documents\.\w+) TO", sql)}
    assert grants == {
        "documents.documents": "SELECT, INSERT, UPDATE, DELETE",
        "documents.versions": "SELECT, INSERT, DELETE",  # versions are never updated in step 5
        "documents.upload_intents": "SELECT, INSERT, UPDATE, DELETE",  # UPDATE only to lock
        "documents.purge_receipts": "SELECT, INSERT",
    }
    assert not re.search(r"GRANT[^\n]*TRUNCATE|ALL PRIVILEGES", sql)


def test_the_migration_forces_rls_on_every_table_and_has_no_cross_schema_reference() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert sql.count("FORCE ROW LEVEL SECURITY") == 1  # emitted once in the per-table loop
    assert 'TABLES = ("documents", "versions", "upload_intents", "purge_receipts")' in sql
    assert "tenant_id = {TENANT}" in sql
    for schema in ("identity.", "access_control.", "audit.", "platform."):
        assert schema not in sql.replace("documents.", ""), schema


def test_the_migration_installs_every_guard_trigger() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for trigger in (
        "documents_guard_update",
        "documents_guard_delete",
        "versions_guard_update",
        "versions_guard_delete",
        "intents_no_update",
        "receipts_no_update_delete",
        "receipts_no_truncate",
    ):
        assert f"CREATE TRIGGER {trigger}" in sql, trigger


def test_documents_does_not_touch_audit_records() -> None:
    for path in py_files(DOCS):
        assert "audit.records" not in code_only(path), path
        assert "lda.modules.audit" not in "\n".join(imports(path)), path
