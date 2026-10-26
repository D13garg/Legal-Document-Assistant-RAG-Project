"""Step 8 pins: what `chunking` may touch and what it must never become (ADR-038, ADR-037).

These inspect the real repository. Generic detectors are shared with the document_processing pins;
the rules specific to this module (determinism, no read path, one event handler, one lifecycle
lock) have their own detectors, and each is exercised against a synthetic violation so that no
rule can pass vacuously.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from test_document_processing_boundaries import (
    DOCUMENTS_TABLES,
    event_patterns,
    forbidden_import_or_call,
    import_targets,
    imported_names_from,
    outcome_usage,
    parser_library_imports,
    py_files,
    rel,
)

from archguard.manifest import load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
CH = SRC / "modules/chunking"
CH_PACKAGE = "lda.modules.chunking"
DOMAIN = CH / "domain"

DP_API = "lda.modules.document_processing.api"
DOCUMENTS_API = "lda.modules.documents.api"
INGESTION_API = "lda.modules.ingestion.api"
DP_API_ALLOWED = frozenset(
    {
        "DocumentId",
        "VersionId",
        "ReadExtraction",
        "ReadExtractionCommand",
        "StoredExtraction",
        "EXTRACTION_NOT_FOUND",
        "EXTRACTION_ARTIFACT_MISSING",
        "EXTRACTION_ARTIFACT_CORRUPT",
    }
)
DOCUMENTS_API_ALLOWED = frozenset(
    {
        "DocumentPurged",  # Step 8: the purge handler
        # Step 11 (ADR-008, approved additive `ReadChunks`): eligibility is Documents' answer
        "DocumentId",
        "DocumentVersionId",
        "ReadVersionState",
        "VersionStateQuery",
        "VersionStatus",
    }
)
INGESTION_API_ALLOWED = frozenset(
    {"StageOutcome", "StagePermanentFailure", "StageRequest", "StageSucceeded"}
)
# Anything that would make a chunk set depend on the moment, the machine or chance.
NON_DETERMINISTIC_ROOTS = frozenset({"random", "secrets", "time", "os", "locale", "platform"})
NON_DETERMINISTIC_CALLS = frozenset(
    {"uuid1", "uuid4", "now", "utcnow", "today", "random", "getenv"}
)
OTHER_SCHEMAS = ("documents", "document_processing", "ingestion", "audit", "platform", "identity")


def production_files() -> list[Path]:
    return [p for p in py_files(CH) if "tests" not in p.parts]


def non_deterministic(source: str) -> set[str]:
    found = {t.split(".")[0] for t in import_targets(source)} & NON_DETERMINISTIC_ROOTS
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else ""
            )
            if name in NON_DETERMINISTIC_CALLS:
                found.add(f"{name}()")
    return found


def other_schema_references(source: str) -> set[str]:
    found: set[str] = set()
    for schema in OTHER_SCHEMAS:
        if re.search(rf"""schema\s*=\s*[rb]?["']{schema}["']""", source):
            found.add(f'schema="{schema}"')
        if re.search(
            rf"\b(FROM|JOIN|INTO|UPDATE|REFERENCES|TABLE)\s+\"?{schema}\"?\.\w", source, re.I
        ):
            found.add(f"SQL on {schema}")
    return found


def api_names(path: Path, module: str) -> set[str]:
    return imported_names_from(path.read_text(encoding="utf-8"), module)


# ---- the detectors catch what they claim to ----------------------------------------------------


def test_the_detectors_catch_synthetic_violations() -> None:
    assert non_deterministic("import random\n") == {"random"}
    assert non_deterministic("import uuid\nx = uuid.uuid4()\n") == {"uuid4()"}
    assert non_deterministic("from datetime import datetime\nx = datetime.now()\n") == {"now()"}
    assert non_deterministic("import os\n") == {"os"}
    assert non_deterministic("from uuid import uuid5\nx = uuid5(a, b)\n") == set()
    assert other_schema_references("SELECT * FROM documents.versions") == {"SQL on documents"}
    assert other_schema_references('Table("t", m, schema="ingestion")') == {'schema="ingestion"'}
    assert other_schema_references("REFERENCES document_processing.extractions") == {
        "SQL on document_processing"
    }
    assert other_schema_references("CREATE TABLE chunking.chunk_sets (a int)") == set()
    assert event_patterns("class X(DomainEvent): pass") == {"class X(DomainEvent)"}
    assert outcome_usage("from a import RecordVersionOutcome") == {"RecordVersionOutcome"}
    assert parser_library_imports("import pypdf") == {"pypdf"}


# ---- imports ------------------------------------------------------------------------------------


def test_the_manifest_declares_exactly_the_three_dag_edges_it_uses() -> None:
    manifests, _ = load_manifests(ROOT)
    assert set(manifests["chunking"].depends_on) == {
        "document_processing",
        "documents",
        "ingestion",
    }
    used = {
        t.split(".")[2]
        for path in production_files()
        for t in import_targets(path.read_text(encoding="utf-8"))
        if t.startswith("lda.modules.") and not t.startswith(CH_PACKAGE)
    }
    assert used == {"document_processing", "documents", "ingestion"}  # all three are really used


def test_production_takes_only_approved_names_from_each_dependency_api() -> None:
    taken: dict[str, set[str]] = {DP_API: set(), DOCUMENTS_API: set(), INGESTION_API: set()}
    for path in production_files():
        for module, names in taken.items():
            names |= api_names(path, module)
    assert taken[DP_API] <= DP_API_ALLOWED, sorted(taken[DP_API] - DP_API_ALLOWED)
    assert taken[DOCUMENTS_API] == DOCUMENTS_API_ALLOWED
    assert taken[INGESTION_API] <= INGESTION_API_ALLOWED, sorted(
        taken[INGESTION_API] - INGESTION_API_ALLOWED
    )
    assert {"ReadExtraction", "ReadExtractionCommand"} <= taken[DP_API]


def test_the_domain_imports_no_other_module_and_no_port() -> None:
    for path in py_files(DOMAIN):
        for target in import_targets(path.read_text(encoding="utf-8")):
            assert not target.startswith(
                (
                    "lda.modules.document_processing",
                    "lda.modules.documents",
                    "lda.modules.ingestion",
                )
            ), (rel(path), target)
            assert not target.startswith(("lda.platform", "lda.shared.ports", "lda.bootstrap")), (
                rel(path),
                target,
            )


def test_only_the_bootstrap_composition_root_imports_chunking() -> None:
    importers = {
        rel(p)
        for p in py_files(SRC)
        if CH not in p.parents
        and any(
            t == CH_PACKAGE or t.startswith(CH_PACKAGE + ".")
            for t in import_targets(p.read_text(encoding="utf-8"))
        )
    }
    others = importers - {"src/lda/bootstrap/chunking.py"}
    # Step 11 (ADR-008): retrieval, and only retrieval, reads chunks, and only through `chunking.api`.
    # Step 13 (ADR-016, owner-approved): citations reads chunk provenance through `chunking.api` only.
    allowed = (
        "src/lda/modules/retrieval/",
        "src/lda/bootstrap/retrieval.py",
        "src/lda/modules/citations/",
        "src/lda/bootstrap/citations.py",
        # Step 17 (ADR-041 D2): the composition-only identity mapping reads the settings slice.
        "src/lda/bootstrap/rag_configuration.py",
    )
    assert all(o.startswith(allowed) for o in others), sorted(others)
    for path in py_files(SRC):
        if rel(path) in others:
            targets = {
                t
                for t in import_targets(path.read_text(encoding="utf-8"))
                if t == CH_PACKAGE or t.startswith(CH_PACKAGE + ".")
            }
            config_only = rel(path) == "src/lda/bootstrap/rag_configuration.py"
            assert all(
                t == CH_PACKAGE + ".api"
                or t.startswith(CH_PACKAGE + ".api.")
                or (config_only and t.startswith(CH_PACKAGE + ".config"))
                for t in targets
            ), (
                rel(path),
                sorted(targets),
            )
    assert "src/lda/bootstrap/chunking.py" in importers


def test_no_manifest_depends_on_chunking_yet() -> None:
    manifests, _ = load_manifests(ROOT)
    dependants = {n for n, m in manifests.items() if "chunking" in m.depends_on}
    # retrieval (Step 11, ADR-008) and citations (Step 13, ADR-016) added themselves
    assert dependants == {"retrieval", "citations"}


# ---- what the module must never become ----------------------------------------------------------


def test_chunking_never_parses_documents_logs_spawns_or_runs_ocr() -> None:
    for path in production_files():
        source = path.read_text(encoding="utf-8")
        assert parser_library_imports(source) == set(), rel(path)
        assert forbidden_import_or_call(source) == set(), rel(path)


def test_the_domain_is_deterministic() -> None:
    for path in py_files(DOMAIN):
        assert non_deterministic(path.read_text(encoding="utf-8")) == set(), rel(path)


def test_chunking_consumes_events_but_never_emits_or_defines_one() -> None:
    for path in production_files():
        assert event_patterns(path.read_text(encoding="utf-8")) == set(), rel(path)


def test_chunking_never_records_a_version_outcome() -> None:
    for path in production_files():
        assert outcome_usage(path.read_text(encoding="utf-8")) == set(), rel(path)
    bootstrap = (SRC / "bootstrap/chunking.py").read_text(encoding="utf-8")
    assert outcome_usage(bootstrap) == set()


def test_the_only_event_handled_is_document_purged_by_decision() -> None:
    """ADR-038 §8: DocumentDeleted needs no handler (no read path; the stage asks Documents first).
    Adding one is a decision that must change this test together with the ADR."""
    registered = (SRC / "bootstrap/chunking.py").read_text(encoding="utf-8")
    assert re.findall(r"handlers\.register\((\w+)", registered) == ["DocumentPurged"]
    for path in production_files():
        assert "DocumentDeleted" not in path.read_text(encoding="utf-8").replace(
            "`DocumentDeleted`", ""
        ).replace("DocumentDeleted needs", ""), rel(path)


APPROVED_READ_NAMES = frozenset({"ReadChunks", "ReadChunksCommand"})
"""Step 11 (ADR-008): the one approved, narrowly scoped read path for chunk text and chunk ids."""


def test_chunking_has_exactly_one_read_path_for_chunk_text() -> None:
    surface = (CH / "api/__init__.py").read_text(encoding="utf-8").split('"""', 2)[2]
    found = set(re.findall(r"\bRead\w*|\bGet\w*|\bList\w*|\bSearch\w*", surface))
    assert found <= APPROVED_READ_NAMES, sorted(found - APPROVED_READ_NAMES)
    assert "ReadChunks" in found
    for path in production_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and node.name.startswith(("Read", "Get", "List")):
                assert node.name in APPROVED_READ_NAMES, (rel(path), node.name)


def test_the_read_path_checks_eligibility_first_and_never_leaks_a_key() -> None:
    source = (CH / "application/use_cases/read_chunks.py").read_text(encoding="utf-8")
    assert source.index("self._versions.execute") < source.index("self._store.get")
    assert source.index("ELIGIBLE_STATUSES") < source.index("self._store.get")
    assert "content_hash(artifact) != record.content_hash" in source  # integrity before parsing
    reader = (CH / "application/read_dto.py").read_text(encoding="utf-8")
    assert "key" not in reader.lower().replace("keyword", "")  # the key layout stays private


def test_the_stage_never_computes_extraction_or_original_keys() -> None:
    """Keys of other modules' objects stay theirs (ADR-005, ADR-037): chunking reads extracted text
    only through `ReadExtraction`."""
    for path in production_files():
        source = path.read_text(encoding="utf-8")
        assert "extracted_path" not in source and "document_processing/" not in source, rel(path)
        assert "original_path" not in source and "ReadOriginal" not in source, rel(path)


def test_every_writer_of_derived_data_takes_the_lifecycle_lock() -> None:
    stage = (CH / "application/chunk_stage.py").read_text(encoding="utf-8")
    purge = (CH / "application/use_cases/purge_chunks.py").read_text(encoding="utf-8")
    assert stage.count("lock_document(") == 2  # the record transaction and the confirmation
    assert purge.count("lock_document(") == 1  # the final transaction
    assert "pg_advisory_xact_lock" in (CH / "infrastructure/persistence/repositories.py").read_text(
        encoding="utf-8"
    )


def test_the_artifact_is_never_overwritten() -> None:
    stage = (CH / "application/chunk_stage.py").read_text(encoding="utf-8")
    assert "overwrite=False" in stage and "overwrite=True" not in stage


# ---- data ownership -----------------------------------------------------------------------------


def test_the_module_references_no_other_schema_and_no_foreign_key() -> None:
    for path in py_files(CH):
        if "tests" in path.parts:
            continue
        assert other_schema_references(path.read_text(encoding="utf-8")) == set(), rel(path)
    migration = next((CH / "infrastructure/persistence/migrations/versions").glob("*.py"))
    assert "REFERENCES" not in migration.read_text(encoding="utf-8").upper()


def test_chunking_owns_no_table_documents_owns() -> None:
    assert DOCUMENTS_TABLES.exists()
    tables = (CH / "infrastructure/persistence/tables.py").read_text(encoding="utf-8")
    assert 'schema="chunking"' in tables and tables.count("schema=") == tables.count(
        'schema="chunking"'
    )
