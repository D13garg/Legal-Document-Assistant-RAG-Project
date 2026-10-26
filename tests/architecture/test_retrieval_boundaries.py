"""Step 11 pins: what `retrieval` may touch and what it must never become (ADR-004/007/008/009).

Static pins are tripwires, not proofs: the behavioural tests in the module's own tree carry the
security claims (scope, proof, eligibility, purge races, content-free errors).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from test_chunking_boundaries import other_schema_references
from test_document_processing_boundaries import (
    import_targets,
    imported_names_from,
    py_files,
    rel,
)

from archguard.manifest import load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
RT = SRC / "modules/retrieval"
RT_PACKAGE = "lda.modules.retrieval"
DOMAIN = RT / "domain"

ALLOWED_API = {
    "lda.modules.chunking.api": {
        "ReadChunks",
        "ReadChunksCommand",
        "DocumentId",
        "VersionId",
        "CHUNKS_ARTIFACT_CORRUPT",
        "CHUNKS_ARTIFACT_MISSING",
        "CHUNKS_NOT_FOUND",
    },
    "lda.modules.documents.api": {
        "DOCUMENT_KIND",
        "READ_DOCUMENT",
        "DocumentId",
        "DocumentPurged",
        "DocumentVersionId",
        "ReadVersionState",
        "VersionStateQuery",
        "VersionStatus",
    },
    "lda.modules.access_control.api": {"AccessScope", "MAX_SCOPE_RESOURCES"},
    "lda.modules.ingestion.api": {
        "StageOutcome",
        "StagePermanentFailure",
        "StageRequest",
        "StageSucceeded",
    },
}
FORBIDDEN_TERMS = (
    "PromptRequest",
    "ContextSegment",
    "AuthorizedContext",
    "AuthorizationProof",
    "ProofClaims",
    "GenerateText",
    "GenerationRecord",
    "Citation",
)
NETWORK_AND_SDK = frozenset(
    {"httpx", "requests", "urllib", "urllib3", "aiohttp", "socket", "openai", "anthropic", "boto3"}
)
CACHES = ("lru_cache", "functools.cache", "cachetools", "redis", "diskcache", "shelve", "pickle")


def production_files() -> list[Path]:
    return [p for p in py_files(RT) if "tests" not in p.parts and "testing" not in p.parts]


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def code_only(source: str) -> str:
    """Source without docstrings and comments: prose may explain what the code must not do."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body[0].value.value = ""
    return ast.unparse(tree)


# ---- the detectors catch what they claim to ----------------------------------------------------


def test_code_only_strips_docstrings_but_keeps_code() -> None:
    assert "PromptRequest" not in code_only('"""PromptRequest"""\nx = 1\n')
    assert "PromptRequest" in code_only("x = PromptRequest()\n")


# ---- imports ------------------------------------------------------------------------------------


def test_the_manifest_declares_exactly_the_dag_edges_it_uses() -> None:
    manifests, _ = load_manifests(ROOT)
    declared = set(manifests["retrieval"].depends_on)
    assert declared == {"chunking", "documents", "access_control", "ingestion"}
    # approved by the DAG but deliberately unused: no embedding egress rule exists (ADR-007)
    assert "ai_gateway" not in declared
    assert not declared & {"prompts", "document_processing", "identity", "audit", "citations"}


def test_production_takes_only_approved_names_from_each_dependency_api() -> None:
    taken: dict[str, set[str]] = {module: set() for module in ALLOWED_API}
    for path in production_files():
        for module in taken:
            taken[module] |= imported_names_from(text_of(path), module)
    for module, names in taken.items():
        assert names <= ALLOWED_API[module], (module, sorted(names - ALLOWED_API[module]))
    assert {"ReadChunks", "ReadChunksCommand"} <= taken["lda.modules.chunking.api"]
    assert "ReadVersionState" in taken["lda.modules.documents.api"]


def test_other_modules_are_only_ever_imported_through_their_api() -> None:
    for path in production_files():
        for target in import_targets(text_of(path)):
            if target.startswith("lda.modules.") and not target.startswith(RT_PACKAGE):
                parts = target.split(".")
                assert parts[3] == "api", (rel(path), target)


def test_the_domain_imports_no_other_module_and_no_port() -> None:
    for path in py_files(DOMAIN):
        for target in import_targets(text_of(path)):
            assert not target.startswith(("lda.modules.chunking", "lda.modules.documents")), (
                rel(path),
                target,
            )
            assert not target.startswith(("lda.modules.access_control", "lda.modules.ingestion"))
            assert not target.startswith(("lda.platform", "lda.shared.ports", "lda.bootstrap"))


def test_only_the_bootstrap_composition_root_and_answering_import_retrieval() -> None:
    importers = {
        rel(p)
        for p in py_files(SRC)
        if RT not in p.parents
        and any(
            t == RT_PACKAGE or t.startswith(RT_PACKAGE + ".") for t in import_targets(text_of(p))
        )
    }
    answering = {i for i in importers if i.startswith("src/lda/modules/answering/")}
    # Step 16 (ADR-040): answering is the one module consumer; it may import the public api only.
    assert importers - answering == {
        "src/lda/bootstrap/answering.py",
        "src/lda/bootstrap/retrieval.py",
        "src/lda/bootstrap/rag_configuration.py",  # Step 17 (ADR-041 D2): settings slice only
    }
    for path in py_files(SRC):
        if rel(path) in answering:
            for target in import_targets(text_of(path)):
                if target == RT_PACKAGE or target.startswith(RT_PACKAGE + "."):
                    api = f"{RT_PACKAGE}.api"
                    assert target == api or target.startswith(api + "."), rel(path)


def test_only_answering_depends_on_retrieval() -> None:
    manifests, _ = load_manifests(ROOT)
    dependants = {n for n, m in manifests.items() if "retrieval" in m.depends_on}
    assert dependants == {"answering"}  # Step 16 (ADR-040); the DAG approves this one edge only


# ---- what the module must never become ----------------------------------------------------------


def test_retrieval_never_builds_prompts_proofs_authorized_context_or_citations() -> None:
    """D1/D2 (Step 11): retrieval ends with a ranked, provenance-carrying result."""
    for path in production_files():
        code = code_only(text_of(path))
        for term in FORBIDDEN_TERMS:
            assert term not in code, (rel(path), term)


def test_retrieval_makes_no_network_call_and_imports_no_provider_sdk() -> None:
    for path in [*production_files(), RT / "testing/doubles.py"]:
        roots = {t.split(".")[0] for t in import_targets(text_of(path))}
        assert not roots & NETWORK_AND_SDK, (rel(path), sorted(roots & NETWORK_AND_SDK))
        assert "ai_gateway" not in text_of(path).replace("ai_gateway` stays", ""), rel(path)


def test_retrieval_keeps_no_cache_and_no_second_copy_of_text_or_permissions() -> None:
    for path in production_files():
        code = code_only(text_of(path))
        for cache in CACHES:
            assert cache not in code, (rel(path), cache)
    tables = text_of(RT / "infrastructure/persistence/tables.py")
    migration = text_of(
        RT / "infrastructure/persistence/migrations/versions/l0001_retrieval.py"
    ).lower()
    for forbidden in ("tsvector", "gin ", "content", "body", "version_number", "status", "grant_"):
        assert forbidden not in tables.lower() and forbidden not in migration, forbidden
    assert not re.search(r"\btext\s*=\s*Column|Column\(\s*\"text\"", tables)
    assert "tsvector" not in migration


def test_retrieval_logs_and_prints_nothing() -> None:
    for path in production_files():
        tree = ast.parse(text_of(path))
        roots = {t.split(".")[0] for t in import_targets(text_of(path))}
        assert not roots & {"logging", "structlog", "warnings"}, rel(path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "print", rel(path)


def test_retrieval_touches_no_other_modules_schema() -> None:
    for path in production_files():
        assert other_schema_references(text_of(path)) == set(), rel(path)


def test_the_proof_is_verified_before_the_query_is_embedded_and_again_before_returning() -> None:
    source = text_of(RT / "application/use_cases/search_index.py")
    assert source.count("self._verifier.verify(") == 2
    first = source.index("self._verifier.verify(")
    assert first < source.index("self._embedder.embed_query(")
    assert first < source.index("self._collect(")
    assert source.rindex("self._verifier.verify(") > source.index("self._collect(")


def test_eligibility_is_asked_of_documents_never_stored() -> None:
    search = text_of(RT / "application/use_cases/search_index.py")
    assert "self._versions.read(" in search  # ReadVersionState, per candidate document, per search
    assert "READ_DOCUMENT" in search and "DOCUMENT_KIND" in search
    records = text_of(DOMAIN / "record.py")
    assert "number" not in records and "status" not in records  # no projection of Documents' facts


def test_every_writer_of_derived_data_takes_the_lifecycle_lock() -> None:
    stage = text_of(RT / "application/index_stage.py")
    purge = text_of(RT / "application/use_cases/purge_index.py")
    assert stage.count("lock_document(") == 1  # the one write transaction
    assert purge.count("lock_document(") == 1
    assert "pg_advisory_xact_lock" in text_of(RT / "infrastructure/persistence/repositories.py")
    assert stage.index("lock_document(") < stage.index("add_version(")
    assert stage.index("receipts.get(") < stage.index("add_version(")


def test_the_receipt_claims_only_the_vector_rows() -> None:
    assert 'PURGE_SCOPE = "retrieval.vector_rows"' in text_of(DOMAIN / "record.py")


APPROVED_EMBEDDER_IMPLEMENTATIONS = [
    # ADR-007 amendment (ADR-045 D3 option (a)): ONE offline development embedder, exactly here.
    "src/lda/modules/retrieval/application/development_embedder.py",
    "src/lda/modules/retrieval/testing/doubles.py",  # the deterministic test double
]


def test_the_embedding_port_documents_the_egress_gap_and_only_the_approved_implementations_exist() -> (
    None
):
    ports = text_of(RT / "application/ports.py")
    assert "NOT permitted until an approved rule" in ports  # the egress restriction stands
    implementers = sorted(
        rel(p)
        for p in py_files(RT)
        if re.search(r"def embed_(query|documents)\(", text_of(p)) and "ports.py" not in p.name
    )
    assert implementers == APPROVED_EMBEDDER_IMPLEMENTATIONS


def test_no_other_production_code_anywhere_implements_the_embedder_port() -> None:
    allowed = {"src/lda/modules/retrieval/application/ports.py", *APPROVED_EMBEDDER_IMPLEMENTATIONS}
    found = {
        rel(p)
        for p in py_files(ROOT / "src")
        if re.search(r"def embed_(query|documents)\(", text_of(p))
    }
    assert found <= allowed, sorted(found - allowed)


def test_the_development_embedder_is_one_named_class_with_no_forbidden_imports() -> None:
    path = RT / "application/development_embedder.py"
    tree = ast.parse(text_of(path))
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert classes == ["DevelopmentEmbedder"]
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    stdlib = {"__future__", "hashlib", "math", "re", "unicodedata", "collections.abc"}
    allowed = {
        "lda.modules.retrieval.domain.embedding",
        "lda.shared.kernel.errors",
    }
    assert imported <= stdlib | allowed, sorted(imported - stdlib - allowed)
    source = text_of(path)
    for word in ("socket", "urllib", "http", "requests", "boto3", "openai", "anthropic", "open("):
        assert word not in source.replace("# ", ""), word
    assert "global " not in source and "lru_cache" not in source


def test_adr_007_records_the_amendment_and_the_exact_file() -> None:
    adr = text_of(
        ROOT / "docs/architecture/ADR/ADR-007-embedding-abstraction-and-index-identity.md"
    )
    assert "Amendment (ADR-045 D3" in adr
    assert "src/lda/modules/retrieval/application/development_embedder.py" in adr
    assert "no network" in adr.lower() and "egress" in adr.lower()


def test_search_returns_no_text_field() -> None:
    dto = ast.parse(text_of(RT / "application/dto.py"))
    for node in ast.walk(dto):
        if isinstance(node, ast.ClassDef) and node.name in {"SearchHit", "SearchResult"}:
            fields = {
                s.target.id
                for s in node.body
                if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
            }
            assert not fields & {"text", "content", "snippet", "body"}, node.name
