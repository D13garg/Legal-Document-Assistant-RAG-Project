"""Step 10 pins: what `prompts` may touch and what it must never become (ADR-017).

Syntax-aware (AST) detectors over the real repository. Each detector is first exercised against
synthetic violations, so no rule can pass vacuously. Like the Step 9 detector these are static
checks with known limits: they see imports, names, calls and statement shapes, not data flow. A
renamed import or a dynamic `importlib` call could evade them, so they complement, and do not
replace, review and the behavioural tests.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from archguard import policy
from archguard.manifest import load_dag, load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
PM = SRC / "modules/prompts"
PACKAGE = "lda.modules.prompts"
BOOTSTRAP_FILE = SRC / "bootstrap/prompts.py"

FORBIDDEN_MODULES = ("access_control", "ai_gateway")
SDK_ROOTS = frozenset(
    {
        "openai",
        "anthropic",
        "cohere",
        "voyageai",
        "mistralai",
        "google",
        "transformers",
        "torch",
        "langchain",
        "llama_index",
        "boto3",
        "httpx",
        "requests",
        "aiohttp",
        "urllib3",
        "urllib",
        "socket",
        "http",
    }
)
PERSISTENCE_ROOTS = frozenset(
    {"sqlalchemy", "alembic", "psycopg", "asyncpg", "redis", "pickle", "shelve", "sqlite3", "dbm"}
)
LOGGING_ROOTS = frozenset({"logging", "warnings", "traceback", "pprint"})
NONDETERMINISTIC_ROOTS = frozenset(
    {
        "random",
        "secrets",
        "uuid",
        "time",
        "datetime",
        "os",
        "sys",
        "platform",
        "locale",
        "tempfile",
        "pathlib",
        "shutil",
        "glob",
        "importlib",
        "pkgutil",
        "threading",
        "asyncio",
        "multiprocessing",
        "subprocess",
        "functools",
        "hashlib",
        "unicodedata",
    }
)
CACHE_NAMES = frozenset({"lru_cache", "cache", "cached_property", "memoize"})
DYNAMIC_BUILTINS = frozenset({"open", "eval", "exec", "compile", "__import__", "input", "globals"})
AUTHORIZATION_NAMES = frozenset(
    {
        "AuthorizedContext",
        "Authorized",
        "AccessScope",
        "AuthorizationProof",
        "AuthorizationDecision",
        "ContextSegment",
        "ProofVerifier",
        "ProofExpectation",
    }
)
CONTENT_NAMES = frozenset(
    {"text", "question", "instructions", "context", "request", "rendered", "body", "template"}
)
MUTABLE_FACTORIES = frozenset(
    {"list", "dict", "set", "bytearray", "defaultdict", "deque", "OrderedDict", "Counter"}
)
ALLOWED_LAYER_DIRS = frozenset({"domain", "application", "api", "testing", "tests"})
REVEAL_ALLOWED = ("application/renderer.py",)
ERROR_DETAIL_NAMES = frozenset({"field", "name"})


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def py_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def module_files() -> list[Path]:
    return py_files(PM)


def production_files() -> list[Path]:
    return [p for p in module_files() if not {"tests", "testing"} & set(p.relative_to(PM).parts)]


def non_test_files() -> list[Path]:
    return [p for p in module_files() if "tests" not in p.relative_to(PM).parts]


def imports(source: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out |= {f"{node.module}.{a.name}" for a in node.names}
    return out


def roots(source: str) -> set[str]:
    return {t.split(".")[0] for t in imports(source)}


def imports_prefix(source: str, prefix: str) -> bool:
    return any(t == prefix or t.startswith(prefix + ".") for t in imports(source))


def identifiers(source: str) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg | ast.keyword) and node.arg is not None:
            found.add(node.arg)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            found.add(node.name)
        elif isinstance(node, ast.alias):
            # Both names: `import ContextSegment as CS` must not hide the original name.
            found.add(node.name.split(".")[-1])
            if node.asname:
                found.add(node.asname)
    return found


def call_names(source: str) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                out.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                out.add(node.func.attr)
    return out


def builtin_calls(source: str) -> set[str]:
    """Names called directly (`compile(...)`), not attributes (`re.compile(...)`)."""
    return {
        n.func.id
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def _mutable_value(value: ast.AST | None) -> bool:
    if isinstance(value, ast.List | ast.Dict | ast.Set | ast.ListComp | ast.DictComp | ast.SetComp):
        return True
    if isinstance(value, ast.Call):
        name = (
            value.func.id
            if isinstance(value.func, ast.Name)
            else value.func.attr
            if isinstance(value.func, ast.Attribute)
            else ""
        )
        return name in MUTABLE_FACTORIES
    return False


def _is_dunder_all(node: ast.Assign | ast.AnnAssign) -> bool:
    """The literal `__all__` list is required by AG080 and is never mutated."""
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return all(isinstance(t, ast.Name) and t.id == "__all__" for t in targets)


def mutable_module_state(source: str) -> list[int]:
    """Top-level assignments to a mutable container, class-body mutable attributes, and any
    `global`/`nonlocal` statement."""
    bad: list[int] = []
    tree = ast.parse(source)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign | ast.AnnAssign)
            and _mutable_value(node.value)
            and not _is_dunder_all(node)
        ):
            bad.append(node.lineno)
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                if isinstance(stmt, ast.Assign | ast.AnnAssign) and _mutable_value(stmt.value):
                    bad.append(stmt.lineno)
    bad += [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Global | ast.Nonlocal)]
    return sorted(set(bad))


def state_written_outside_init(source: str) -> list[int]:
    """`self.x = ...`, `self.x += ...`, or `self.x.append(...)`-style writes outside `__init__`
    and `__post_init__` (the renderer and catalog must hold no state that changes)."""
    bad: list[int] = []

    def targets_self(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and (node.value.id == "self")
        )

    for cls in (n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ClassDef)):
        for fn in (n for n in cls.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)):
            if fn.name in {"__init__", "__post_init__"}:
                continue
            for node in ast.walk(fn):
                if (
                    (isinstance(node, ast.Assign) and any(targets_self(t) for t in node.targets))
                    or (
                        isinstance(node, ast.AugAssign | ast.AnnAssign)
                        and targets_self(node.target)
                    )
                    or (
                        isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr
                        in {
                            "append",
                            "extend",
                            "add",
                            "update",
                            "pop",
                            "clear",
                            "insert",
                            "setdefault",
                        }
                        and targets_self(node.func.value)
                    )
                ):
                    bad.append(node.lineno)
    return sorted(set(bad))


def implicit_ordering(source: str) -> list[int]:
    """`sorted(...)` and `.sort(...)`: rendering must keep the caller's order."""
    return sorted(
        n.lineno
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id in {"sorted", "reversed"})
            or (isinstance(n.func, ast.Attribute) and n.func.attr in {"sort", "reverse"})
        )
    )


def swallowing_handlers(source: str) -> list[int]:
    bad = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ExceptHandler):
            names = (
                {n.id for n in ast.walk(node.type) if isinstance(n, ast.Name)}
                if node.type
                else {"<bare>"}
            )
            if names & {"<bare>", "Exception", "BaseException"}:
                bad.append(node.lineno)
    return bad


def _risky(expr: ast.AST) -> bool:
    for n in ast.walk(expr):
        if isinstance(n, ast.JoinedStr) or (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod)):
            return True
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"format", "reveal"}
        ):
            return True
        if isinstance(n, ast.Name) and n.id in CONTENT_NAMES:
            return True
        if isinstance(n, ast.Attribute) and n.attr in CONTENT_NAMES:
            return True
    return False


def content_in_errors(source: str) -> list[int]:
    """Errors must be built from constants and codes. `raise X(<content>)`, and a `detail=` that is
    neither a string constant nor one of the field-name parameters, are violations."""
    bad: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            args = [*node.exc.args, *(k.value for k in node.exc.keywords)]
            if any(_risky(a) for a in args):
                bad.append(node.lineno)
        elif isinstance(node, ast.keyword) and node.arg == "detail":
            value = node.value
            ok = (isinstance(value, ast.Constant) and isinstance(value.value, str)) or (
                isinstance(value, ast.Name) and value.id in ERROR_DETAIL_NAMES
            )
            if not ok or _risky(value):
                bad.append(value.lineno)
    return sorted(set(bad))


def reveal_calls(source: str) -> list[int]:
    return sorted(
        n.lineno
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "reveal"
    )


def lines_setting_test_only(source: str) -> list[int]:
    """`test_only=True` outside the one sample template (and tests)."""
    return sorted(
        k.value.lineno
        for k in (n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.keyword))
        if k.arg == "test_only" and isinstance(k.value, ast.Constant) and k.value.value is True
    )


# ---- the detectors catch what they claim to --------------------------------------------------------


def test_the_detectors_catch_synthetic_violations() -> None:
    assert imports_prefix(
        "from lda.modules.access_control.api import X\n", "lda.modules.access_control"
    )
    assert imports_prefix("import lda.modules.ai_gateway.api\n", "lda.modules.ai_gateway")
    assert imports_prefix("from lda import bootstrap\n", "lda.bootstrap")
    assert imports_prefix("from lda.bootstrap import x\n", "lda.bootstrap")
    assert "openai" in roots("import openai\n")
    assert "logging" in roots("import logging as log\n")
    assert "ContextSegment" in identifiers("x = ContextSegment(r, t)\n")
    assert "ContextSegment" in identifiers("from m import ContextSegment as CS\n")
    assert "eval" in call_names("eval('1')\n")
    assert builtin_calls("compile('x', 'f', 'exec')\n") == {"compile"}
    assert builtin_calls("import re\nre.compile('a')\n") == set()

    assert mutable_module_state("CACHE = {}\n") == [1]
    assert mutable_module_state("SEEN: list[str] = []\n") == [1]
    assert mutable_module_state("X = defaultdict(list)\n") == [1]
    assert mutable_module_state("class A:\n    items = []\n") == [2]
    assert mutable_module_state("def f():\n    global X\n    X = 1\n") == [2]
    assert mutable_module_state("__all__ = ['a']\n") == []
    assert mutable_module_state("__all__ = ['a']\nOTHER = ['b']\n") == [2]
    assert "ContextSegment" in identifiers("import m.ContextSegment as CS\n")
    assert mutable_module_state("X = ()\nY = re.compile('a')\nZ = frozenset({1})\n") == []

    assert state_written_outside_init("class A:\n    def f(self):\n        self.x = 1\n") == [3]
    assert state_written_outside_init("class A:\n    def f(self):\n        self.x += 1\n") == [3]
    assert state_written_outside_init("class A:\n    def f(self):\n        self.x.append(1)\n") == [
        3
    ]
    assert (
        state_written_outside_init("class A:\n    def __init__(self):\n        self.x = 1\n") == []
    )

    assert implicit_ordering("def f(x):\n    return sorted(x)\n") == [2]
    assert implicit_ordering("def f(x):\n    x.sort()\n") == [2]
    assert implicit_ordering("def f(x):\n    return list(x)\n") == []

    assert swallowing_handlers("try:\n    pass\nexcept Exception:\n    pass\n") == [3]
    assert swallowing_handlers("try:\n    pass\nexcept:\n    pass\n") == [3]
    assert swallowing_handlers("try:\n    pass\nexcept UnicodeEncodeError:\n    pass\n") == []

    assert content_in_errors("def f(text):\n    raise ValueError(f'bad {text}')\n") == [2]
    assert content_in_errors("def f(question):\n    raise ValueError(question)\n") == [2]
    assert content_in_errors("def f(v):\n    raise ValueError(v.reveal())\n") == [2]
    assert content_in_errors("def f(v):\n    raise E('c', detail=v)\n") == [2]
    assert content_in_errors("def f(v):\n    raise E('c', detail='a %s' % v)\n") == [2]
    assert content_in_errors("def f(field):\n    raise E('c', detail=field)\n") == []
    assert content_in_errors("def f():\n    raise E('c', detail='max_parts')\n") == []

    assert reveal_calls("x = y.reveal()\n") == [1]
    assert lines_setting_test_only("T(test_only=True)\nU(test_only=False)\n") == [1]


# ---- the DAG, the manifest and the structure -------------------------------------------------------


def test_the_dag_and_the_manifest_declare_no_dependencies_and_no_schema() -> None:
    dag, _ = load_dag(ROOT)
    manifests, _ = load_manifests(ROOT)
    assert list(dag["prompts"]) == []
    assert list(manifests["prompts"].depends_on) == []
    assert manifests["prompts"].owns_schema == ""
    assert tuple(manifests["prompts"].layers) == ("domain", "application")


def test_the_dag_file_still_gives_prompts_no_edges_adds_no_module_and_keeps_its_consumers() -> None:
    raw = tomllib.loads((ROOT / "docs/architecture/module-dag.toml").read_text(encoding="utf-8"))
    modules = raw["modules"]
    assert modules["prompts"]["depends_on"] == []
    assert len(modules) == 15
    dependants = {name for name, spec in modules.items() if "prompts" in spec["depends_on"]}
    assert dependants == {"grounded_generation", "answering", "evaluation"}


def test_only_the_permitted_directories_exist_and_nothing_persists() -> None:
    top = {p.name for p in PM.iterdir() if p.is_dir() and p.name != "__pycache__"}
    assert top <= ALLOWED_LAYER_DIRS, top - ALLOWED_LAYER_DIRS
    assert not (PM / "infrastructure").exists()
    assert not list(PM.rglob("migrations"))
    assert not list(PM.rglob("persistence"))
    assert not (PM / "application/use_cases").exists()  # ADR-017: a pure service, not a use case
    assert (PM / "domain/catalog").is_dir()
    assert not (PM / "catalog").exists()  # AG001 would reject a module-root `catalog`


def test_codeowners_points_at_the_real_template_location_and_changes_nothing_else() -> None:
    lines = (ROOT / "CODEOWNERS").read_text(encoding="utf-8").splitlines()
    entries = [ln.split() for ln in lines if ln.strip() and not ln.startswith("#")]
    assert ["/src/lda/modules/prompts/domain/catalog/", "@legal-rag/ai-quality"] in entries
    assert not any(e[0] == "/src/lda/modules/prompts/catalog/" for e in entries)
    assert (ROOT / "src/lda/modules/prompts/domain/catalog").is_dir()


def test_prompts_uses_no_use_case_markers() -> None:
    for path in module_files():
        names = identifiers(path.read_text(encoding="utf-8"))
        assert not names & {"public", "requires", "audited", "not_audited"}, rel(path)


# ---- imports ---------------------------------------------------------------------------------------


def test_no_file_imports_access_control_ai_gateway_bootstrap_or_another_module() -> None:
    for path in module_files():
        source = path.read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_MODULES:
            assert not imports_prefix(source, f"lda.modules.{forbidden}"), (rel(path), forbidden)
        assert not imports_prefix(source, "lda.bootstrap"), rel(path)
        others = {
            t for t in imports(source) if t.startswith("lda.modules.") and not t.startswith(PACKAGE)
        }
        assert not others, (rel(path), others)


def test_no_sdk_network_persistence_or_platform_import_anywhere_in_the_module() -> None:
    for path in module_files():
        source = path.read_text(encoding="utf-8")
        assert not roots(source) & (SDK_ROOTS | PERSISTENCE_ROOTS), rel(path)
        for banned in (
            "lda.platform.database",
            "lda.shared.ports.object_store",
            "lda.shared.ports.unit_of_work",
            "lda.shared.ports.audit",
            "lda.shared.ports.rate_limiter",
            "lda.shared.ports.authorization.ProofVerifier",
            "lda.shared.ports.authorization.ProofExpectation",
        ):
            assert not imports_prefix(source, banned), (rel(path), banned)


def test_production_code_uses_only_stdlib_pydantic_kernel_and_the_neutral_proof_resource() -> None:
    for path in production_files():
        source = path.read_text(encoding="utf-8")
        for target in imports(source):
            if target.startswith("lda.") and not target.startswith(PACKAGE):
                allowed = target.startswith("lda.shared.kernel") or target in {
                    "lda.shared.ports.authorization",
                    "lda.shared.ports.authorization.ProofResource",
                }
                assert allowed, (rel(path), target)
    for path in (PM / "domain").rglob("*.py"):
        assert not imports_prefix(path.read_text(encoding="utf-8"), "lda.shared.ports"), rel(path)


GROUNDED_BOOTSTRAP_FILE = SRC / "bootstrap/grounded_generation.py"
GROUNDED = SRC / "modules/grounded_generation"


def test_only_the_bootstrap_and_grounded_generation_import_prompts_from_production_code() -> None:
    """Step 14 (ADR-029) is the DAG's sanctioned second consumer of the renderer; like the bootstrap
    it may use the public `prompts.api` only (never `domain`, `application` or `testing`)."""
    importers = set()
    for path in py_files(SRC):
        parts = set(path.relative_to(SRC).parts)
        if path.is_relative_to(PM) or {"tests", "testing"} & parts or path == BOOTSTRAP_FILE:
            continue
        source = path.read_text(encoding="utf-8")
        if imports_prefix(source, PACKAGE):
            importers.add(rel(path))
            # Step 17 (ADR-041 D1): bootstrap/rag_configuration.py reads the approved template to
            # hash its wording into the configuration identity (public api only, asserted below).
            assert (
                path.is_relative_to(GROUNDED)
                or path == GROUNDED_BOOTSTRAP_FILE
                or rel(path) == "src/lda/bootstrap/rag_configuration.py"
            ), rel(path)
            assert all(
                t == f"{PACKAGE}.api" or t.startswith(f"{PACKAGE}.api.")
                for t in imports(source)
                if t.startswith(PACKAGE)
            ), rel(path)
    assert any(i.startswith("src/lda/modules/grounded_generation/") for i in importers)


def test_the_bootstrap_uses_only_the_public_api_and_config_and_no_access_control() -> None:
    source = BOOTSTRAP_FILE.read_text(encoding="utf-8")
    for target in imports(source):
        if target.startswith(PACKAGE):
            assert target.startswith((f"{PACKAGE}.api", f"{PACKAGE}.config")), target
    assert not imports_prefix(source, "lda.modules.access_control")


def test_the_gateway_and_access_control_never_import_prompts() -> None:
    for module in ("ai_gateway", "access_control"):
        for path in py_files(SRC / "modules" / module):
            assert not imports_prefix(path.read_text(encoding="utf-8"), PACKAGE), rel(path)


# ---- authorization is not prompts' job -------------------------------------------------------------


def test_prompts_never_names_or_builds_authorization_types_or_segments() -> None:
    for path in non_test_files():
        found = identifiers(path.read_text(encoding="utf-8")) & AUTHORIZATION_NAMES
        assert not found, (rel(path), found)


def test_ag090_is_unchanged_and_grants_prompts_nothing() -> None:
    assert policy.RESTRICTED_CONSTRUCTORS["ContextSegment"] == (
        "src/lda/modules/grounded_generation/",
        "src/lda/modules/answering/",
        "src/lda/modules/ai_gateway/testing/",
        "src/lda/modules/ai_gateway/tests/",
    )
    assert not any(
        "prompts" in p for paths in policy.RESTRICTED_CONSTRUCTORS.values() for p in paths
    )
    assert tuple(policy.NON_WAIVABLE) and "AG090" in policy.NON_WAIVABLE


def test_the_audit_exemption_pin_was_not_widened_for_prompts() -> None:
    source = (ROOT / "tests/architecture/test_audit_boundaries.py").read_text(encoding="utf-8")
    assert "RenderPrompt" not in source
    assert "PromptRenderer" not in source


# ---- determinism, state, privacy -------------------------------------------------------------------


def test_production_code_is_deterministic_and_stateless() -> None:
    for path in non_test_files():
        source = path.read_text(encoding="utf-8")
        assert not roots(source) & NONDETERMINISTIC_ROOTS, rel(path)
        assert not identifiers(source) & CACHE_NAMES, rel(path)
        assert not builtin_calls(source) & DYNAMIC_BUILTINS, rel(path)
        assert mutable_module_state(source) == [], rel(path)
        assert state_written_outside_init(source) == [], rel(path)
    for path in production_files():
        assert implicit_ordering(path.read_text(encoding="utf-8")) == [], rel(path)


def test_nothing_is_logged_printed_or_audited() -> None:
    for path in non_test_files():
        source = path.read_text(encoding="utf-8")
        assert not roots(source) & LOGGING_ROOTS, rel(path)
        assert "print" not in identifiers(source), rel(path)
        assert not imports_prefix(source, "lda.shared.ports.audit"), rel(path)
        assert not imports_prefix(source, "lda.platform"), rel(path)
    bootstrap = BOOTSTRAP_FILE.read_text(encoding="utf-8")
    assert not roots(bootstrap) & LOGGING_ROOTS
    assert "print" not in identifiers(bootstrap)


def test_nothing_is_hashed_or_fingerprinted_for_telemetry() -> None:
    for path in non_test_files():
        names = {n.lower() for n in identifiers(path.read_text(encoding="utf-8"))}
        assert not {n for n in names if "sha" in n or "digest" in n or "fingerprint" in n}, rel(
            path
        )


def test_no_production_code_swallows_exceptions_broadly() -> None:
    for path in non_test_files():
        assert swallowing_handlers(path.read_text(encoding="utf-8")) == [], rel(path)


def test_no_error_is_built_from_content_and_reveal_is_confined_to_the_renderer() -> None:
    for path in non_test_files():
        source = path.read_text(encoding="utf-8")
        assert content_in_errors(source) == [], rel(path)
        if reveal_calls(source):
            assert rel(path).endswith(REVEAL_ALLOWED), rel(path)


# ---- the test-only template ------------------------------------------------------------------------


def test_production_code_cannot_reach_the_test_only_template() -> None:
    for path in production_files():
        source = path.read_text(encoding="utf-8")
        assert not imports_prefix(source, f"{PACKAGE}.domain.catalog.sample_template"), rel(path)
        assert not imports_prefix(source, f"{PACKAGE}.testing"), rel(path)
        if path != PM / "domain/catalog/sample_template.py":
            assert lines_setting_test_only(source) == [], rel(path)  # the approved file has none
    catalog = (PM / "domain/catalog/__init__.py").read_text(encoding="utf-8")
    assert not any("sample_template" in t for t in imports(catalog))
    assert not imports_prefix((BOOTSTRAP_FILE).read_text(encoding="utf-8"), f"{PACKAGE}.testing")


def test_the_approved_template_is_imported_by_production_code_in_exactly_one_place() -> None:
    """The approved wording reaches production only through the catalog list (ADR-029): no other
    production file of any module imports the template module."""
    target = f"{PACKAGE}.domain.catalog.proposed_grounded_answer"
    importers = {
        rel(path)
        for path in py_files(SRC)
        if not {"tests", "testing"} & set(path.relative_to(SRC).parts)
        and imports_prefix(path.read_text(encoding="utf-8"), target)
    }
    assert importers == {rel(PM / "domain/catalog/__init__.py")}


def test_the_production_template_list_holds_exactly_the_approved_grounded_answer() -> None:
    """Adding or removing a production template needs its own explicit approval of the wording;
    this pin makes that a visible, reviewed change rather than a silent one."""
    tree = ast.parse((PM / "domain/catalog/__init__.py").read_text(encoding="utf-8"))
    assigned = [
        n
        for n in tree.body
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "PRODUCTION_TEMPLATES"
    ]
    assert len(assigned) == 1
    value = assigned[0].value
    assert isinstance(value, ast.Tuple)
    assert [e.id for e in value.elts if isinstance(e, ast.Name)] == ["PROPOSED_GROUNDED_ANSWER"]
    assert len(value.elts) == 1


def test_the_sample_template_declares_itself_test_only() -> None:
    source = (PM / "domain/catalog/sample_template.py").read_text(encoding="utf-8")
    assert lines_setting_test_only(source) != []
    assert "TEST-ONLY" in source


def test_the_grounded_answer_template_is_the_owner_approved_production_template() -> None:
    """ADR-029 D8: approved by the project owner (2026-10-16) for one exact hash. It is labelled as
    approved, is not test-only, is a production catalog member, and no test-only template is."""
    path = PM / "domain/catalog/proposed_grounded_answer.py"
    source = path.read_text(encoding="utf-8")
    assert "APPROVED by the project owner on 2026-10-16" in source
    assert "not an\napproval by `@legal-rag/ai-quality`" in source
    assert "UNAPPROVED" not in source
    assert lines_setting_test_only(source) == []
    assert "test_only=False" in source
    from lda.modules.prompts import api
    from lda.modules.prompts.domain.catalog import PRODUCTION_TEMPLATES
    from lda.modules.prompts.domain.catalog.proposed_grounded_answer import (
        PROPOSED_GROUNDED_ANSWER,
    )
    from lda.modules.prompts.domain.catalog.sample_template import TEST_ONLY_TEMPLATE
    from lda.modules.prompts.domain.template import PromptCatalog

    assert PROPOSED_GROUNDED_ANSWER.test_only is False
    assert PRODUCTION_TEMPLATES == (PROPOSED_GROUNDED_ANSWER,)
    assert TEST_ONLY_TEMPLATE not in PRODUCTION_TEMPLATES
    assert not any("PROPOSED" in name for name in api.__all__)  # the template is not public API
    assert PromptCatalog(PRODUCTION_TEMPLATES, production=True).templates == PRODUCTION_TEMPLATES
    try:
        PromptCatalog((TEST_ONLY_TEMPLATE,), production=True)
    except ValueError:
        pass
    else:
        raise AssertionError("a production catalog accepted a test-only template")
