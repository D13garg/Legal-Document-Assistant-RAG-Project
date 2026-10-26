"""Step 12 pins: what `evaluation` may touch and what it must never become (ADR-018, ADR-030).

Syntax-aware (AST) detectors over the real repository; each is first exercised on synthetic
violations so no rule passes vacuously. They are static checks with known limits (no data flow,
evadable by dynamic imports) and complement the behavioural tests in the module.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from archguard.manifest import load_dag, load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
EM = SRC / "modules/evaluation"
BOOTSTRAP_FILE = SRC / "bootstrap/evaluation.py"

FORBIDDEN_ROOTS = frozenset(
    {
        # providers, network, persistence, logging, clock, randomness, filesystem, concurrency
        "openai", "anthropic", "cohere", "google", "langchain", "boto3", "httpx", "requests",
        "urllib", "socket", "http", "sqlalchemy", "alembic", "psycopg", "redis", "pickle",
        "sqlite3", "logging", "warnings", "traceback", "random", "secrets", "uuid", "time",
        "datetime", "os", "sys", "pathlib", "shutil", "subprocess", "threading", "asyncio",
        "importlib", "functools", "tempfile",
    }
)  # fmt: skip
AUTHORIZATION_NAMES = frozenset(
    {"AccessScope", "AuthorizationProof", "AuthorizedContext", "ContextSegment", "ProofVerifier"}
)
CACHE_NAMES = frozenset({"lru_cache", "cache", "cached_property", "memoize"})


def production_files() -> list[Path]:
    """The pure core: everything except tests, test doubles and (Step 17, ADR-041 D3) the
    infrastructure layer, which is the only place persistence may live."""
    return sorted(
        p
        for p in EM.rglob("*.py")
        if not {"tests", "testing", "infrastructure"} & set(p.relative_to(EM).parts)
    )


def infrastructure_files() -> list[Path]:
    return sorted(p for p in (EM / "infrastructure").rglob("*.py") if "migrations" not in p.parts)


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
        if parts[:2] == ["lda", "modules"] and len(parts) > 2 and parts[2] != "evaluation":
            hits.append(name)
        if parts[:2] in (["lda", "platform"], ["lda", "bootstrap"]):
            hits.append(name)
    return hits


def authorization_names(tree: ast.AST) -> list[str]:
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    return sorted(names & AUTHORIZATION_NAMES)


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


def bound_exceptions(tree: ast.AST) -> list[str]:
    return [h.name for h in handlers(tree) if h.name]


def test_the_detectors_catch_synthetic_violations() -> None:
    bad = ast.parse(
        "import logging\nimport httpx\nfrom datetime import datetime\n"
        "from lda.modules.ai_gateway.api import GenerateText\nfrom lda.platform import x\n"
        "from lda.modules.evaluation.api import Fine\n"
    )
    assert forbidden_imports(bad) == [
        "logging",
        "httpx",
        "datetime",
        "datetime.datetime",
        "lda.modules.ai_gateway.api",
        "lda.modules.ai_gateway.api.GenerateText",
        "lda.platform",
        "lda.platform.x",
    ]
    assert authorization_names(ast.parse("from m import AccessScope\nx = ProofVerifier")) == [
        "AccessScope",
        "ProofVerifier",
    ]
    assert cache_uses(ast.parse("import functools\n@functools.cache\ndef f(): ...")) == ["cache"]
    assert bound_exceptions(ast.parse("try:\n    pass\nexcept Exception as e:\n    raise")) == ["e"]
    assert bound_exceptions(ast.parse("try:\n    pass\nexcept Exception:\n    pass")) == []


def test_the_manifest_and_dag_keep_the_dependency_ceiling_and_declare_the_schema() -> None:
    dag, _ = load_dag(ROOT)
    manifests, _ = load_manifests(ROOT)
    assert set(dag["evaluation"]) == {"ai_gateway", "prompts"}  # permitted ceiling, unchanged
    assert list(manifests["evaluation"].depends_on) == []  # and nothing is imported
    # Step 17 (ADR-041 D3): the module owns its own schema and gains an infrastructure layer.
    assert manifests["evaluation"].owns_schema == "evaluation"
    assert tuple(manifests["evaluation"].layers) == ("domain", "application", "infrastructure")
    raw = tomllib.loads((ROOT / "docs/architecture/module-dag.toml").read_text(encoding="utf-8"))
    assert len(raw["modules"]) == 15
    assert not [m for m, spec in raw["modules"].items() if "evaluation" in spec["depends_on"]]


def test_only_permitted_directories_exist_and_persistence_is_confined_to_infrastructure() -> None:
    top = {p.name for p in EM.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert top <= {"api", "domain", "application", "infrastructure", "testing", "tests"}, top
    persistence_dirs = {p.relative_to(EM).as_posix() for p in EM.rglob("persistence")}
    migrations = {p.relative_to(EM).as_posix() for p in EM.rglob("migrations")}
    assert persistence_dirs == {"infrastructure/persistence"}
    assert migrations == {"infrastructure/persistence/migrations"}


def test_infrastructure_touches_only_its_own_schema_and_no_provider_clock_or_logging() -> None:
    """The persistence adapter may use SQLAlchemy and the shared kernel; nothing else outside the
    module, no other module, no provider, network, logging, randomness or clock."""
    allowed_roots = {"sqlalchemy", "lda", "collections", "uuid", "__future__", "pathlib"}
    files = infrastructure_files()
    assert len(files) >= 4
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in imported_names(tree):
            parts = name.split(".")
            assert parts[0] in allowed_roots, (path.name, name)
            if parts[0] == "lda":
                assert parts[:3] == ["lda", "modules", "evaluation"] or parts[:2] == [
                    "lda",
                    "shared",
                ], (path.name, name)
        assert authorization_names(tree) == [], path.name
        assert cache_uses(tree) == [], path.name
        assert bound_exceptions(tree) == [], path.name  # driver errors embed parameters: discarded


def test_production_code_imports_no_module_provider_persistence_clock_or_logging() -> None:
    files = production_files()
    assert len(files) >= 10
    for path in files:
        hits = forbidden_imports(ast.parse(path.read_text(encoding="utf-8")))
        assert hits == [], (path.name, hits)


def test_evaluation_creates_and_names_no_authorization_type_and_has_no_cache() -> None:
    for path in [*production_files(), BOOTSTRAP_FILE]:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert authorization_names(tree) == [], path.name
        assert cache_uses(tree) == [], path.name


def test_the_only_exception_handler_discards_the_exception() -> None:
    """A target failure can carry content, so it is neither bound nor re-raised nor formatted."""
    found = []
    for path in production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        assert bound_exceptions(tree) == [], path.name
        found += [(path.name, ast.unparse(h.type) if h.type else "") for h in handlers(tree)]
    # Step 17 (ADR-041): `release.py` parses stored text; it discards the parse error (the stored
    # text is untrusted) and raises one content-free code.
    assert sorted(found) == [
        (
            "release.py",
            "(ValueError, TypeError, KeyError, IndexError, AttributeError, ValidationError)",
        ),
        ("release.py", "ValidationError"),  # verify_run: a corrupt record is one failure code
        ("run_evaluation.py", "Exception"),
    ]


def test_the_use_cases_are_the_reviewed_set_and_are_declared() -> None:
    uc = sorted(
        p.name for p in (EM / "application/use_cases").glob("*.py") if p.name != "__init__.py"
    )
    assert uc == ["release_gating.py", "run_evaluation.py"]
    for name in uc:
        source = (EM / "application/use_cases" / name).read_text(encoding="utf-8")
        assert "@public(" in source and "@not_audited(" in source and "@audited" not in source


def test_the_bootstrap_adds_no_provider_and_only_the_evaluation_api() -> None:
    names = imported_names(ast.parse(BOOTSTRAP_FILE.read_text(encoding="utf-8")))
    lda_modules = {n.split(".")[2] for n in names if n.startswith("lda.modules.")}
    assert lda_modules == {"evaluation"}
    assert not [n for n in names if n.split(".")[0] in FORBIDDEN_ROOTS]
