"""Step 13 pins: what `citations` may touch and what it must never become (ADR-016).

Syntax-aware (AST) detectors over the real repository; each is first exercised on synthetic
violations so no rule passes vacuously. They are static checks with known limits (no data flow,
evadable by dynamic imports) and complement the behavioural tests in the module.
"""

from __future__ import annotations

import ast
import dataclasses
import tomllib
from pathlib import Path

from archguard.manifest import load_dag, load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
CM = SRC / "modules/citations"
BOOTSTRAP_FILE = SRC / "bootstrap/citations.py"

FORBIDDEN_ROOTS = frozenset(
    {
        # providers, network, persistence, logging, clock source, randomness, filesystem, concurrency
        "openai", "anthropic", "cohere", "google", "langchain", "boto3", "httpx", "requests",
        "urllib", "socket", "http", "sqlalchemy", "alembic", "psycopg", "redis", "pickle",
        "sqlite3", "logging", "warnings", "traceback", "random", "secrets", "time", "os", "sys",
        "pathlib", "shutil", "subprocess", "threading", "asyncio", "importlib", "functools",
        "tempfile",
    }
)  # fmt: skip
ALLOWED_MODULE_APIS = frozenset({"access_control", "chunking"})
FORBIDDEN_NAMES = frozenset(
    {
        "AccessScope", "AuthorizationProof", "AuthorizedContext", "ContextSegment", "PromptRequest",
        "GenerateText", "SearchIndex", "SearchResult", "SigningKey", "SigningKeyring",
    }
)  # fmt: skip
CACHE_NAMES = frozenset({"lru_cache", "cache", "cached_property", "memoize"})
TEXT_FIELD_NAMES = frozenset(
    {"text", "excerpt", "snippet", "answer", "prompt", "question", "section_path", "title", "proof"}
)
DECLARATIONS = frozenset({"public", "requires", "audited", "not_audited"})


def production_files() -> list[Path]:
    """The pure core (domain, application, api): every strict rule below applies to it.

    The persistence adapter (Step 13 completion, ADR-016 amendment) lives in `infrastructure/` and
    is checked separately by `infrastructure_files()`; it is NOT exempt from the core rules by
    being skipped, it has its own pinned rules.
    """
    return sorted(
        p
        for p in CM.rglob("*.py")
        if not {"tests", "testing", "infrastructure"} & set(p.relative_to(CM).parts)
    )


def infrastructure_files() -> list[Path]:
    return sorted(p for p in (CM / "infrastructure").rglob("*.py") if "__pycache__" not in p.parts)


def imported_names(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
            out += [f"{node.module}.{a.name}" for a in node.names]
    return out


def forbidden_imports(tree: ast.AST) -> list[str]:
    hits = []
    for name in imported_names(tree):
        parts = name.split(".")
        if parts[0] in FORBIDDEN_ROOTS:
            hits.append(name)
        foreign = parts[:2] == ["lda", "modules"] and len(parts) > 2 and parts[2] != "citations"
        if foreign and (parts[2] not in ALLOWED_MODULE_APIS or parts[3:4] != ["api"]):
            hits.append(name)
        if parts[:2] in (["lda", "platform"], ["lda", "bootstrap"]):
            hits.append(name)
    return hits


def forbidden_names(tree: ast.AST) -> list[str]:
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    return sorted(names & FORBIDDEN_NAMES)


def cache_uses(tree: ast.AST) -> list[str]:
    found = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and n.id in CACHE_NAMES:
            found.add(n.id)
        if isinstance(n, ast.Attribute) and n.attr in CACHE_NAMES:
            found.add(n.attr)
        if isinstance(n, ast.ImportFrom):
            found |= {a.name for a in n.names if a.name in CACHE_NAMES}
    return sorted(found)


def handlers(tree: ast.AST) -> list[ast.ExceptHandler]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]


def constructs(tree: ast.AST, name: str) -> bool:
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
        for n in ast.walk(tree)
    )


def declaration_uses(tree: ast.AST) -> list[str]:
    found = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module == "lda.shared.kernel.declarations":
            found |= {a.name for a in n.names}
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in DECLARATIONS:
            found.add(n.func.id)
    return sorted(found)


def test_the_detectors_catch_synthetic_violations() -> None:
    bad = ast.parse(
        "import logging\nimport httpx\nfrom lda.modules.documents.api import X\n"
        "from lda.modules.chunking.domain import Y\nfrom lda.platform import z\n"
        "from lda.modules.chunking.api import Fine\nfrom uuid import UUID\n"
    )
    assert forbidden_imports(bad) == [
        "logging",
        "httpx",
        "lda.modules.documents.api",
        "lda.modules.documents.api.X",
        "lda.modules.chunking.domain",
        "lda.modules.chunking.domain.Y",
        "lda.platform",
        "lda.platform.z",
    ]
    assert forbidden_names(ast.parse("from m import ContextSegment\nx = AccessScope")) == [
        "AccessScope",
        "ContextSegment",
    ]
    assert cache_uses(ast.parse("import functools\n@functools.cache\ndef f(): ...")) == ["cache"]
    assert handlers(ast.parse("try:\n    pass\nexcept Exception as e:\n    raise"))[0].name == "e"
    assert constructs(ast.parse("x = EvidenceSet(1)"), "EvidenceSet")
    assert not constructs(ast.parse("x = other.EvidenceSet(1)"), "EvidenceSet")
    assert declaration_uses(ast.parse("from lda.shared.kernel.declarations import public")) == [
        "public"
    ]


def test_the_manifest_and_dag_keep_citations_to_its_two_edges_and_its_own_schema() -> None:
    dag, _ = load_dag(ROOT)
    manifests, _ = load_manifests(ROOT)
    assert set(dag["citations"]) == {"access_control", "chunking"}
    assert set(manifests["citations"].depends_on) == {"access_control", "chunking"}
    assert manifests["citations"].owns_schema == "citations"
    assert tuple(manifests["citations"].layers) == ("domain", "application", "infrastructure")
    raw = tomllib.loads((ROOT / "docs/architecture/module-dag.toml").read_text(encoding="utf-8"))
    assert len(raw["modules"]) == 15
    assert {m for m, spec in raw["modules"].items() if "citations" in spec["depends_on"]} == {
        "grounded_generation",
        "conversations",
    }


def test_only_permitted_directories_exist_and_persistence_is_confined_to_infrastructure() -> None:
    top = {p.name for p in CM.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert top <= {"api", "domain", "application", "infrastructure", "testing", "tests"}, top
    persistence = CM / "infrastructure" / "persistence"
    assert set(CM.rglob("migrations")) == {persistence / "migrations"}
    assert set(CM.rglob("persistence")) == {persistence}
    assert {p.name for p in (persistence / "migrations" / "versions").glob("*.py")} == {
        "c0001_citations.py"
    }


def test_the_persistence_adapter_is_content_free_and_discards_every_driver_cause() -> None:
    files = infrastructure_files()
    assert {p.name for p in files} >= {"repositories.py", "unit_of_work.py", "tables.py"}
    allowed_roots = {"lda", "sqlalchemy", "alembic", "collections", "uuid", "pathlib", "__future__"}
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in imported_names(tree):
            parts = name.split(".")
            assert parts[0] in allowed_roots, (path.name, name)
            if parts[:2] == ["lda", "modules"]:
                assert parts[2] == "citations", (path.name, name)  # no other module, not even api
            if parts[0] == "lda" and parts[1:2] not in (["modules"], ["shared"], ["platform"]):
                raise AssertionError((path.name, name))
        assert forbidden_names(tree) == [] and cache_uses(tree) == [], path.name
        assert not [c for c in ast.walk(tree) if isinstance(c, ast.Name) and c.id == "logging"]
        for h in handlers(tree):
            assert h.name is None, path.name  # the driver error embeds statement parameters
    found = sorted(
        (p.name, ast.unparse(h.type) if h.type else "")
        for p in files
        for h in handlers(ast.parse(p.read_text(encoding="utf-8")))
    )
    assert found == [
        ("repositories.py", "(ValidationError, ValueError)"),
        ("repositories.py", "SQLAlchemyError"),
        ("unit_of_work.py", "SQLAlchemyError"),
    ]
    for path in files:
        if any(h for h in handlers(ast.parse(path.read_text(encoding="utf-8")))):
            assert "from None" in path.read_text(encoding="utf-8"), path.name


def test_production_code_imports_only_the_two_permitted_apis_and_no_provider_or_persistence() -> (
    None
):
    files = production_files()
    assert len(files) >= 12
    for path in files:
        hits = forbidden_imports(ast.parse(path.read_text(encoding="utf-8")))
        assert hits == [], (path.name, hits)


def test_citations_builds_no_scope_proof_context_prompt_or_search_and_has_no_cache() -> None:
    for path in [*production_files(), BOOTSTRAP_FILE]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert forbidden_names(tree) == [], path.name
        assert cache_uses(tree) == [], path.name


def test_the_layers_stay_clean() -> None:
    for path in (CM / "domain").glob("*.py"):
        names = imported_names(ast.parse(path.read_text(encoding="utf-8")))
        assert not [n for n in names if n.startswith("lda.modules.") and ".citations." not in n]
        assert not [n for n in names if n.startswith("lda.shared.ports")], path.name


def test_the_only_exception_handler_discards_the_cause() -> None:
    """A store failure must not leak which of purged, foreign or ineligible it was."""
    found = []
    for path in production_files():
        for h in handlers(ast.parse(path.read_text(encoding="utf-8"))):
            assert h.name is None, path.name
            found.append((path.name, ast.unparse(h.type) if h.type else ""))
    assert found == [("evidence_builder.py", "(NotFoundError, ConflictError)")]
    source = (CM / "application/evidence_builder.py").read_text(encoding="utf-8")
    assert "from None" in source


def test_citations_declares_no_use_case_so_the_audit_exemption_set_is_untouched() -> None:
    for path in production_files():
        assert declaration_uses(ast.parse(path.read_text(encoding="utf-8"))) == [], path.name
    assert not (CM / "application/use_cases").exists()
    assert not (CM / "application/queries").exists()


def test_only_the_evidence_builder_constructs_an_evidence_set_in_production() -> None:
    builders = [
        p.name
        for p in production_files()
        if constructs(ast.parse(p.read_text(encoding="utf-8")), "EvidenceSet")
    ]
    assert builders == ["evidence_builder.py"]


def test_no_source_or_record_field_can_hold_text_or_proof() -> None:
    from lda.modules.citations.api import Citation, CitationSource, EvidenceSet, GenerationRecord

    for cls in (CitationSource, Citation, EvidenceSet, GenerationRecord):
        names = {f.name for f in dataclasses.fields(cls)}
        assert not names & TEXT_FIELD_NAMES, (cls.__name__, names & TEXT_FIELD_NAMES)


def test_the_repository_port_is_insert_only() -> None:
    from lda.modules.citations.api import GenerationRecordRepository

    methods = {n for n in vars(GenerationRecordRepository) if not n.startswith("_")}
    assert methods == {"add", "get"}


def test_the_duplicated_document_identifiers_equal_the_documents_module() -> None:
    from lda.modules.citations.domain.source import DOCUMENT_KIND, READ_DOCUMENT
    from lda.modules.documents import api as documents

    assert (DOCUMENT_KIND, READ_DOCUMENT) == (documents.DOCUMENT_KIND, documents.READ_DOCUMENT)


def test_only_the_bootstrap_imports_citations_from_production_code() -> None:
    importers = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC)
        if rel.parts[:2] == ("modules", "citations") or "tests" in rel.parts:
            continue
        if any(
            n.startswith("lda.modules.citations")
            for n in imported_names(ast.parse(path.read_text(encoding="utf-8")))
        ):
            importers.add(rel.as_posix())
    # Step 14 (ADR-016 amendment, ADR-029): grounded_generation consumes the public api only; its
    # composition root additionally binds the PostgreSQL record repository (see the next test).
    # Step 15 (ADR-039): conversations is the second consumer, again through the public api only
    # (GenerationRecordRepository); its bootstrap only types the port it is handed.
    # Step 16 (ADR-040): answering imports no citations name at all (Q1: no citations edge); only
    # its bootstrap reads the `citations` settings slice, as bootstrap/grounded_generation.py does.
    consumers = ("modules/grounded_generation/", "modules/conversations/")
    assert {i for i in importers if not i.startswith(consumers)} == {
        "bootstrap/answering.py",
        "bootstrap/citations.py",
        "bootstrap/conversations.py",
        "bootstrap/grounded_generation.py",
        "bootstrap/rag_configuration.py",  # Step 17 (ADR-041 D2): reads the settings slice only
    }
    for consumer in ("grounded_generation", "conversations"):
        for path in SRC.glob(f"modules/{consumer}/**/*.py"):
            if "tests" in path.relative_to(SRC).parts:
                continue
            for name in imported_names(ast.parse(path.read_text(encoding="utf-8"))):
                if name.startswith("lda.modules.citations"):
                    assert name.startswith("lda.modules.citations.api"), path.name


def test_the_grounded_generation_bootstrap_touches_citations_only_for_api_and_the_unit_of_work() -> (
    None
):
    names = imported_names(
        ast.parse((SRC / "bootstrap/grounded_generation.py").read_text(encoding="utf-8"))
    )
    cited = {n for n in names if n.startswith("lda.modules.citations")}
    assert all(
        n.startswith("lda.modules.citations.api")
        or n == "lda.modules.citations.config"
        or n.startswith("lda.modules.citations.config.")
        or n.startswith("lda.modules.citations.infrastructure.persistence.unit_of_work")
        for n in cited
    )


def test_the_bootstrap_uses_only_the_two_permitted_apis_and_no_provider() -> None:
    names = imported_names(ast.parse(BOOTSTRAP_FILE.read_text(encoding="utf-8")))
    lda_modules = {n.split(".")[2] for n in names if n.startswith("lda.modules.")}
    assert lda_modules == {"chunking", "citations"}
    assert not [n for n in names if n.split(".")[0] in FORBIDDEN_ROOTS]


# ---- Step 14 (ADR-016 amendment 2): text-bearing evidence stays confined ---------------------------


def _citations_production_files() -> list[Path]:
    return [
        p
        for p in (SRC / "modules/citations").rglob("*.py")
        if "tests" not in p.relative_to(SRC).parts and "testing" not in p.relative_to(SRC).parts
    ]


def test_only_the_text_builder_constructs_text_evidence() -> None:
    builders = []
    for path in _citations_production_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"TextEvidence", "EvidencePassage"}
            ):
                builders.append((path.name, node.func.id))
    assert sorted(set(builders)) == [
        ("evidence_builder.py", "EvidencePassage"),
        ("evidence_builder.py", "TextEvidence"),
    ]


def test_text_bearing_types_are_exactly_the_two_reviewed_ones() -> None:
    """A `text` field is content. Only the transient evidence types may carry one; no stored or
    returned-to-persistence type (record, citation, source, evidence set) may."""
    carriers = set()
    for path in _citations_production_files():
        for cls in [
            n
            for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(n, ast.ClassDef)
        ]:
            fields = {
                n.target.id
                for n in cls.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            }
            if "text" in fields:
                carriers.add(cls.name)
    assert carriers == {"ChunkContent", "EvidencePassage"}


def test_chunk_text_is_read_by_one_adapter_through_chunkings_public_api() -> None:
    readers = set()
    for path in _citations_production_files():
        for name in imported_names(ast.parse(path.read_text(encoding="utf-8"))):
            if name.startswith("lda.modules.chunking"):
                assert name.startswith("lda.modules.chunking.api"), path.name
                readers.add(path.name)
    assert readers == {"provenance.py", "content.py"}
    provenance = (SRC / "modules/citations/application/provenance.py").read_text(encoding="utf-8")
    assert "c.text," not in provenance and ".text" not in provenance.replace("text_hash", "")


def test_no_persistence_code_touches_text_evidence() -> None:
    for path in (SRC / "modules/citations/infrastructure").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for name in ("TextEvidence", "EvidencePassage", "ChunkContent", "ChunkContentReader"):
            assert name not in source, (path.name, name)
