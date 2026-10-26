"""Step 4 checkpoint: Audit's place in the DAG and what stays out of it (ADR-019).

These tests inspect the real repository (not fixtures): Audit depends on nothing and nobody depends
on it; producers reach it only through the shared port; no read API, no deletion path, no service
objects; the migration grants INSERT only; every audit declaration in the repository is honoured.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from archguard import policy
from archguard.manifest import load_dag, load_manifests
from archguard.runner import run_all

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
AUDIT = SRC / "modules/audit"
MIGRATION = AUDIT / "infrastructure/persistence/migrations/versions/u0001_audit_records.py"
USE_CASE_DIRS = ("application/use_cases", "application/queries")


def py_files(base: Path) -> list[Path]:
    return [p for p in base.rglob("*.py") if "__pycache__" not in p.parts]


def imports(path: Path) -> list[str]:
    out: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def use_case_classes() -> list[tuple[Path, ast.ClassDef]]:
    found: list[tuple[Path, ast.ClassDef]] = []
    for sub in USE_CASE_DIRS:
        for path in (SRC / "modules").glob(f"*/{sub}/*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found += [
                (path, c)
                for c in tree.body
                if isinstance(c, ast.ClassDef) and not c.name.startswith("_")
            ]
    return found


def code_only(path: Path) -> str:
    """Source without docstrings and comments, so prose cannot trip a code check."""
    return re.sub(r'""".*?"""|#.*', "", path.read_text(encoding="utf-8"), flags=re.DOTALL)


def marker(cls: ast.ClassDef, name: str) -> ast.Call | None:
    for dec in cls.decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name) and dec.func.id == name:
            return dec
    return None


# ---- module boundary ---------------------------------------------------------------------------


def test_audit_depends_on_nothing_and_nothing_depends_on_audit() -> None:
    manifests, _ = load_manifests(ROOT)
    dag, _ = load_dag(ROOT)
    assert dag["audit"] == ()
    assert manifests["audit"].depends_on == ()
    assert not [m for m, deps in dag.items() if "audit" in deps]
    assert not [m for m, man in manifests.items() if "audit" in man.depends_on]


def test_no_module_imports_audit_except_bootstrap_and_audits_own_tests() -> None:
    offenders = []
    for path in py_files(SRC):
        rel = path.relative_to(SRC)
        if rel.parts[:2] == ("modules", "audit") or rel.parts[0] == "bootstrap":
            continue
        if any(i.startswith("lda.modules.audit") for i in imports(path)):
            offenders.append(str(rel))
    assert offenders == []


def test_audit_imports_no_other_module() -> None:
    for path in py_files(AUDIT):
        for name in imports(path):
            if name.startswith("lda.modules."):
                assert name.startswith("lda.modules.audit"), (path, name)


def test_audits_domain_imports_neither_ports_nor_platform_nor_bootstrap() -> None:
    for path in py_files(AUDIT / "domain"):
        for name in imports(path):
            assert not name.startswith(("lda.shared.ports", "lda.platform", "lda.bootstrap")), path


def test_only_the_sql_adapter_and_bootstrap_touch_the_database_layer() -> None:
    for path in py_files(AUDIT):
        if "tests" in path.parts or "infrastructure" in path.parts:
            continue
        assert not any(i.startswith(("sqlalchemy", "lda.platform.database")) for i in imports(path))


def test_the_kernel_and_shared_port_are_unchanged_in_shape() -> None:
    """Step 4 changes no kernel behaviour: the port is still the whole producer-facing contract."""
    from lda.shared.ports.audit import AuditFact, AuditOutcome, AuditRecorder

    assert list(AuditFact.__dataclass_fields__) == [
        "tenant_id",
        "actor",
        "action",
        "resource_type",
        "resource_id",
        "outcome",
        "request_id",
        "occurred_at",
        "attributes",
    ]
    assert {o.value for o in AuditOutcome} == {"success", "denied", "failure"}
    assert [n for n in vars(AuditRecorder) if not n.startswith("_")] == ["record"]


# ---- what Audit does not contain (frozen D2/D3/D7/D8) --------------------------------------------


def test_audit_has_no_use_cases_no_read_api_and_no_service_objects() -> None:
    assert not (AUDIT / "application/use_cases").exists()
    assert not (AUDIT / "application/queries").exists()
    banned = re.compile(r"Service|Manager|Coordinator|Orchestrator|Reader|Query|Retention|Purge")
    for path in py_files(AUDIT):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                assert not banned.search(node.name), (path, node.name)
    api = (AUDIT / "api/__init__.py").read_text(encoding="utf-8")
    assert "requires" not in api and "audited" not in api


def test_no_audit_statement_in_the_module_selects_updates_or_deletes() -> None:
    for path in py_files(AUDIT / "infrastructure"):
        if "migrations" in path.parts:
            continue
        source = code_only(path)
        assert not re.search(r"\b(select|update|delete|truncate)\(", source), path
        assert "returning" not in source.lower(), path


def test_the_migration_grants_insert_and_nothing_else() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    grants = re.findall(r"GRANT\s+([A-Z, ]+?)\s+ON\s+([\w.]+)", sql)
    assert sorted(grants) == [("INSERT", "audit.records"), ("USAGE", "SCHEMA")] or sorted(
        grants
    ) == [("INSERT", "audit.records"), ("USAGE", "SCHEMA audit")]
    for forbidden in ("SELECT", "UPDATE", "DELETE", "TRUNCATE", "ALL PRIVILEGES"):
        assert not re.search(rf"GRANT[^\n]*\b{forbidden}\b", sql), forbidden
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "BEFORE UPDATE OR DELETE" in sql and "BEFORE TRUNCATE" in sql
    assert "REFERENCES" not in sql and "FOREIGN KEY" not in sql


def test_no_retention_deletion_or_hash_chain_exists_yet() -> None:
    words = re.compile(r"retention|purge|expire|hash_chain|prev_hash|merkle", re.IGNORECASE)
    for path in py_files(AUDIT):
        if "tests" in path.parts:
            continue
        assert not words.search(code_only(path)), path


# ---- every audit declaration in the repository is honoured (D1, D9) ------------------------------


def test_every_audited_use_case_emits_exactly_its_declared_action() -> None:
    audited = {}
    for path, cls in use_case_classes():
        dec = marker(cls, "audited")
        if dec is None:
            continue
        label = ast.literal_eval(dec.args[0])
        emitted = [
            n
            for n in ast.walk(cls)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "record"
            and isinstance(n.func.value, ast.Attribute)
            and n.func.value.attr == "audit"
        ]
        assert len(emitted) == 1, (path, cls.name)
        fact = emitted[0].args[0]
        assert isinstance(fact, ast.Call)
        action = next(k.value for k in fact.keywords if k.arg == "action")
        assert ast.literal_eval(action) == label, (path, cls.name)
        audited[cls.name] = label
    assert audited == {
        "RegisterPrincipal": "identity.principal_registered",
        "DisablePrincipal": "identity.principal_disabled",
        "EnablePrincipal": "identity.principal_enabled",
        "GrantAccess": "access_control.access_granted",
        "RevokeAccess": "access_control.access_revoked",
        # Step 5 (documents): each effective mutation emits its own declared fact
        "UploadDocument": "documents.document_uploaded",
        "AddDocumentVersion": "documents.document_version_added",
        "DeleteDocument": "documents.document_deleted",
        "PurgeDocument": "documents.document_purged",
        # Step 6: the terminal version transition (Documents) and the explicit replay (ingestion)
        "RecordVersionOutcome": "documents.version_outcome_recorded",
        "ReplayIngestion": "ingestion.ingestion_replayed",
        # Step 14 (ADR-029): each recorded generation, in the transaction that records it
        "GenerateGroundedAnswer": "grounded_generation.generation_recorded",
        # Step 15 (ADR-039): each effective conversation mutation; replays are no-ops, not facts
        "StartConversation": "conversations.conversation_started",
        "AppendUserMessage": "conversations.user_message_added",
        "AppendAssistantTurn": "conversations.assistant_turn_added",
        "DeleteConversation": "conversations.conversation_deleted",
    }


def test_the_not_audited_exemptions_are_exactly_the_reviewed_set() -> None:
    """Adding an exemption is a visible, reviewed diff (ADR-019 D9). It is no permission to skip
    any future security-mandated audit requirement. Step 5 added three, all read-only or
    housekeeping: GetDocument, VerifyDocumentPurge, ReapAbandonedUpload. Step 6 added seven in
    ingestion (ADR-010 §15: no per-stage audit): pipeline state by the system (ExecuteRun,
    RunWatchdog, AcknowledgeOutcome, CancelIngestion, PurgeIngestionRecords) and two read-only
    queries (GetIngestionStatus, GetIngestionMetrics). Step 7 added two (ADR-037): ReadOriginal
    (read-only content access for the pipeline) and PurgeExtractions (derived-data purge; the
    receipt is its evidence and Documents audits the purge itself). Step 8 added three (ADR-038):
    ReadVersionState (Documents' read-only lifecycle check), ReadExtraction (read-only content
    access for the pipeline) and PurgeChunks (derived-data purge with its own receipt). Step 9
    added one (ADR-006): GenerateText (egress; auditing is deferred by ADR-019 D3 and the use case
    returns content-free call evidence instead). Step 11 added three (ADR-008): ReadChunks
    (read-only content access for derived-data modules), SearchIndex (read-only query, proof-gated)
    and PurgeRetrievalIndex (derived-data purge with its own receipt). Step 12 added one (ADR-018):
    RunEvaluation (offline, writes nothing; the report is its result). Step 15 added two (ADR-039):
    ReadConversation and ListConversations (owner-only read queries; reads are not audited).
    Step 16 added one (ADR-040): Answer, which writes nothing itself; each write it causes (user
    message, generation record, assistant turn) is audited by the audited use case that makes it.
    Step 17 added three (ADR-041): EvaluateConfiguration, RecordEligibility and CheckEligibility,
    global synthetic evaluation data with no tenant or principal; the insert-only tables are the
    record."""
    exempt = sorted(cls.name for _, cls in use_case_classes() if marker(cls, "not_audited"))
    assert exempt == [
        "AcknowledgeOutcome",
        "Answer",
        "AuthorizeAction",
        "CancelIngestion",
        "CheckEligibility",
        "EvaluateConfiguration",
        "ExecuteRun",
        "GenerateText",
        "GetDocument",
        "GetIngestionMetrics",
        "GetIngestionStatus",
        "GetPrincipal",
        "ListConversations",
        "PurgeChunks",
        "PurgeExtractions",
        "PurgeIngestionRecords",
        "PurgeRetrievalIndex",
        "ReadChunks",
        "ReadConversation",
        "ReadExtraction",
        "ReadOriginal",
        "ReadVersionState",
        "ReapAbandonedUpload",
        "RecordEligibility",
        "ResolveAccessScope",
        "RunEvaluation",
        "RunWatchdog",
        "SearchIndex",
        "VerifyDocumentPurge",
    ]


def test_not_audited_use_cases_emit_nothing() -> None:
    for path, cls in use_case_classes():
        if marker(cls, "not_audited") is None:
            continue
        source = ast.unparse(cls)
        assert ".audit." not in source, (path, cls.name)


def test_every_emitting_use_case_takes_actor_and_tenant_from_the_context() -> None:
    """Resource and outcome come from the use case; tenant, actor and request id from the context."""
    for path, cls in use_case_classes():
        if marker(cls, "audited") is None:
            continue
        source = ast.unparse(cls)
        assert "tenant_id=ctx.tenant_id" in source, (path, cls.name)
        assert "attribution_of(ctx)" in source, (path, cls.name)
        assert "outcome=AuditOutcome.SUCCESS" in source, (path, cls.name)


def test_the_repository_conforms_to_ag056() -> None:
    assert [v for v in run_all(ROOT) if v.rule == "AG056"] == []


def test_ag056_is_registered_in_the_enforcement_catalogue() -> None:
    catalogue = (ROOT / "docs/architecture/enforcement.md").read_text(encoding="utf-8")
    assert "AG056" in catalogue
    assert policy.AUDIT_FACT_TYPE == "AuditFact"
