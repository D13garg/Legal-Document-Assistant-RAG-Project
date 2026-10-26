"""Step 16 pins: what `answering` may touch and what it must never become (ADR-040).

AST and value checks over the real repository, each paired with a synthetic violation so no rule
passes vacuously. Static checks have known limits (no data flow, evadable by dynamic imports);
they complement the behavioural, workflow and PostgreSQL tests.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from archguard.manifest import load_dag, load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
AM = SRC / "modules/answering"
BOOTSTRAP_FILE = SRC / "bootstrap/answering.py"
MODULES = "lda.modules."

APPROVED = frozenset({"retrieval", "grounded_generation", "conversations", "access_control"})
FORBIDDEN = frozenset(
    {
        "citations",  # Q1: no citations edge; claims are built through the neutral ClaimReference
        "documents",
        "identity",
        "ai_gateway",  # egress is reached only through grounded_generation
        "prompts",  # templates are reached only through grounded_generation
        "ingestion",
        "document_processing",
        "chunking",
        "evaluation",
        "audit",
    }
)
FORBIDDEN_ROOTS = frozenset(
    {
        "openai", "anthropic", "cohere", "google", "langchain", "boto3", "httpx", "requests",
        "urllib", "socket", "http", "sqlalchemy", "alembic", "psycopg", "redis", "pickle",
        "sqlite3", "logging", "warnings", "traceback", "random", "secrets", "os", "sys",
        "subprocess", "threading", "asyncio", "importlib", "tempfile",
    }
)  # fmt: skip

PRE_STEP16_GROUNDED_NAMES = frozenset(
    {
        "INVALID_INPUT", "LIMIT_EXCEEDED", "NOT_AUTHORIZED", "OUTPUT_CITATION_INVALID",
        "OUTPUT_MALFORMED", "OUTPUT_QUOTE_UNSUPPORTED", "OUTPUT_SCHEMA", "OUTPUT_TOO_LARGE",
        "OUTPUT_UNSAFE_TEXT", "PERSISTENCE_FAILED", "REQUIRES_PRINCIPAL", "TEMPLATE_UNAVAILABLE",
        "AnswerDrafter", "AnswerStatement", "ContractLimits", "DocumentAccess",
        "EvidenceAssembler", "GenerateGroundedAnswer", "GenerateGroundedAnswerCommand",
        "GroundedAnswer", "GroundedUnitOfWork", "GroundedUnitOfWorkFactory", "GroundingPolicy",
    }
)  # fmt: skip


def py_files(root: Path, *, tests: bool = False) -> list[Path]:
    return [
        p
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
        and (tests or ("tests" not in p.parts and "testing" not in p.parts))
    ]


def imported(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
            out += [f"{node.module}.{a.name}" for a in node.names]
    return out


def other_modules(tree: ast.AST) -> list[str]:
    """`lda.modules.<name>.<rest>` imports of modules other than answering, as (name.rest)."""
    hits = []
    for target in imported(tree):
        if target.startswith(MODULES) and not target.startswith(MODULES + "answering"):
            hits.append(target[len(MODULES) :])
    return hits


def violations(source: str) -> list[str]:
    """Imports of a non-approved module, or of an approved module other than through `.api`."""
    bad = []
    for item in other_modules(ast.parse(source)):
        name, _, rest = item.partition(".")
        if name not in APPROVED or not (rest == "api" or rest.startswith("api.")):
            bad.append(item)
    return bad


# ---- manifest and DAG ---------------------------------------------------------------------------


def test_the_manifest_declares_exactly_the_edges_it_uses_and_no_new_edge() -> None:
    manifests, _ = load_manifests(ROOT)
    assert set(manifests["answering"].depends_on) == APPROVED
    dag, _ = load_dag(ROOT)
    assert set(dag["answering"]) >= APPROVED  # every edge was already approved in the DAG
    assert manifests["answering"].owns_schema == ""


def test_the_dag_entry_for_answering_is_exactly_the_one_approved_before_step_16() -> None:
    """Q1 requirement 6: the DAG was not altered to accommodate this step."""
    dag, _ = load_dag(ROOT)
    assert set(dag["answering"]) == APPROVED | {"ai_gateway", "prompts"}
    assert "citations" not in dag["answering"]
    manifests, _ = load_manifests(ROOT)
    assert not FORBIDDEN & set(manifests["answering"].depends_on)


def test_only_answering_consumes_the_new_grounded_api_names() -> None:
    names = ("ClaimReference", "claims_from_references")
    users = {
        p.relative_to(ROOT).as_posix()
        for p in py_files(SRC)
        if "modules/grounded_generation/" not in p.as_posix()
        and any(n in p.read_text() for n in names)
    }
    assert users == {
        "src/lda/modules/answering/application/use_cases/answer.py",
    }


# ---- imports ------------------------------------------------------------------------------------


def test_answering_imports_only_the_public_api_of_approved_modules() -> None:
    for path in py_files(AM):
        assert violations(path.read_text()) == [], path.relative_to(ROOT).as_posix()


def test_the_bootstrap_imports_only_api_or_config_slices() -> None:
    for item in other_modules(ast.parse(BOOTSTRAP_FILE.read_text())):
        name, _, rest = item.partition(".")
        if name == "answering":
            continue
        assert rest.split(".")[0] in {"api", "config"}, item


def test_the_checker_catches_each_kind_of_violation() -> None:
    assert violations("from lda.modules.citations.api import SourceClaim")
    assert violations("from lda.modules.documents.api import DOCUMENT_KIND")
    assert violations("from lda.modules.identity.api import GetPrincipal")
    assert violations("from lda.modules.ai_gateway.api import GenerateText")
    assert violations("from lda.modules.retrieval.domain.ids import ChunkId")
    assert violations("from lda.modules.conversations.infrastructure.x import Y")
    assert violations("import lda.modules.grounded_generation.application.dto")
    assert violations("from lda.modules.retrieval.api import SearchQuery") == []
    assert violations("from lda.modules.access_control.api import ActionId") == []


def test_answering_has_no_io_logging_persistence_or_provider_imports() -> None:
    for path in py_files(AM):
        for target in imported(ast.parse(path.read_text())):
            assert target.split(".")[0] not in FORBIDDEN_ROOTS, (path.name, target)


def test_answering_has_no_infrastructure_layer_and_no_persistence_files() -> None:
    assert {p.name for p in AM.iterdir() if p.is_dir() and p.name != "__pycache__"} == {
        "api",
        "application",
        "domain",
        "testing",
        "tests",
    }
    assert not list(AM.rglob("migrations")) and not list(AM.rglob("*.sql"))


def test_answering_never_prints_or_logs_and_never_stores() -> None:
    for path in py_files(AM):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"print", "open", "exec", "eval", "__import__"}


# ---- values copied across a boundary the DAG forbids importing --------------------------------


def test_the_literal_copies_equal_their_sources_of_truth() -> None:
    from lda.modules.access_control.domain.scope import MAX_SCOPE_RESOURCES
    from lda.modules.answering.application.limits import HARD_MAX_CANDIDATES
    from lda.modules.answering.domain import vocabulary
    from lda.modules.citations.api import GenerationOutcome
    from lda.modules.documents.api import DOCUMENT_KIND, READ_DOCUMENT
    from lda.modules.grounded_generation.application.policy import CONFIG_ID

    assert vocabulary.DOCUMENT_KIND == DOCUMENT_KIND
    assert vocabulary.READ_DOCUMENT == READ_DOCUMENT
    assert vocabulary.CONFIG_ID.pattern == CONFIG_ID.pattern
    assert HARD_MAX_CANDIDATES == MAX_SCOPE_RESOURCES
    assert {o.name: o.value for o in vocabulary.AnswerOutcome} == {
        o.name: o.value for o in GenerationOutcome
    }


def test_the_neutral_claim_validation_is_not_laxer_than_the_citations_contract() -> None:
    from lda.modules.citations.domain import source
    from lda.modules.grounded_generation.application import claim_reference

    assert claim_reference.MAX_ORDINAL == source.MAX_ORDINAL
    assert claim_reference._DIGEST.pattern == source._DIGEST.pattern  # noqa: SLF001
    assert re.fullmatch(claim_reference._DIGEST.pattern, "a" * 64)  # noqa: SLF001


def test_the_settings_ceilings_equal_what_the_limits_enforce() -> None:
    from lda.modules.answering.application.limits import (
        HARD_MAX_CANDIDATES,
        HARD_MAX_QUESTION_CHARACTERS,
        HARD_MAX_SOURCES,
    )
    from lda.modules.answering.config import AnsweringSettings

    meta = {n: f.metadata for n, f in AnsweringSettings.model_fields.items()}
    ceilings = {
        n: next(m.le for m in meta[n] if hasattr(m, "le"))
        for n in ("max_candidates", "max_question_characters", "max_sources")
    }
    assert ceilings == {
        "max_candidates": HARD_MAX_CANDIDATES,
        "max_question_characters": HARD_MAX_QUESTION_CHARACTERS,
        "max_sources": HARD_MAX_SOURCES,
    }


# ---- the authorized, strictly additive change to a frozen API ------------------------------------


def test_the_grounded_api_only_grew() -> None:
    from lda.modules.grounded_generation import api

    names = set(api.__all__)
    assert names >= PRE_STEP16_GROUNDED_NAMES  # nothing frozen was removed or renamed
    assert names - PRE_STEP16_GROUNDED_NAMES == {"ClaimReference", "claims_from_references"}


def test_the_existing_command_signature_is_unchanged() -> None:
    import dataclasses

    from lda.modules.grounded_generation.api import GenerateGroundedAnswerCommand

    assert [f.name for f in dataclasses.fields(GenerateGroundedAnswerCommand)] == [
        "question",
        "claims",
        "config_id",
    ]
    assert "SourceClaim" in str(
        {f.name: f.type for f in dataclasses.fields(GenerateGroundedAnswerCommand)}["claims"]
    )
