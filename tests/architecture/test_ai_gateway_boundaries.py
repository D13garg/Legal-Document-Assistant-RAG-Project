"""Step 9 pins: what `ai_gateway` may touch and what it must never become (ADR-006, ADR-026).

These inspect the real repository with syntax-aware (AST) detectors. Each detector is exercised
against a synthetic violation first, so no rule can pass vacuously.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path
from typing import TypeGuard

from archguard.manifest import load_dag, load_manifests

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src/lda"
GW = SRC / "modules/ai_gateway"
PACKAGE = "lda.modules.ai_gateway"
USE_CASE_FILE = GW / "application/use_cases/generate_text.py"

SDK_ROOTS = frozenset(
    {
        "openai",
        "anthropic",
        "cohere",
        "voyageai",
        "mistralai",
        "google",
        "sentence_transformers",
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
RUNTIME_ROOTS = frozenset({"time", "threading", "asyncio", "multiprocessing", "subprocess"})
CACHE_NAMES = frozenset({"lru_cache", "cache", "cached_property", "memoize"})
TOOL_NAMES = frozenset({"tools", "tool", "tool_choice", "tool_use", "functions", "function_call"})
CONTENT_NAMES = frozenset(
    {
        "text",
        "instructions",
        "token",
        "proof_token",
        "segment",
        "segments",
        "command",
        "request",
        "response",
        "context",
    }
)
REVEAL_ALLOWED = ("application/use_cases/generate_text.py", "testing/fakes.py")
PORT_NAMES = frozenset({"LlmProvider", "ProviderRequest", "ProviderResponse"})
BOOTSTRAP_FILE = SRC / "bootstrap/ai_gateway.py"


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def py_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def module_files() -> list[Path]:
    return py_files(GW)


def production_files() -> list[Path]:
    return [p for p in module_files() if not {"tests", "testing"} & set(p.relative_to(GW).parts)]


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
    """Every name that can carry a field, parameter, keyword, attribute or variable."""
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
            found.add((node.asname or node.name).split(".")[-1])
    return found


def is_call(node: ast.AST, attr: str) -> TypeGuard[ast.Call]:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
    )


_NESTED_BLOCKS = frozenset({"body", "orelse", "finalbody", "handlers", "cases"})


def is_verify_statement(stmt: ast.stmt) -> bool:
    """A bare `[await] x.verify(...)` statement: the only form credited as proof verification.

    `verify` returns nothing and raises on failure, so a statement is the proof. A verify buried in
    a condition, `assert` (stripped by `-O`), `and`/`or`, a conditional expression, a lambda or an
    argument is never credited.
    """
    if not isinstance(stmt, ast.Expr):
        return False
    value = stmt.value.value if isinstance(stmt.value, ast.Await) else stmt.value
    return is_call(value, "verify")


def is_provider_reference(node: ast.AST) -> bool:
    """`x.generate` in any form (call, alias, callback) or `getattr(x, "generate")`."""
    if isinstance(node, ast.Attribute):
        return node.attr == "generate"
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value == "generate"
    )


def _scan(node: ast.AST, verified: bool, bad: list[int]) -> None:
    """Flag provider references in an expression; a lambda body runs later, so it starts unverified."""
    if is_provider_reference(node) and not verified:
        bad.append(getattr(node, "lineno", 0))
    inner = False if isinstance(node, ast.Lambda) else verified
    for child in ast.iter_child_nodes(node):
        _scan(child, inner, bad)


def _own_expressions(stmt: ast.stmt) -> list[ast.AST]:
    out: list[ast.AST] = []
    for name, value in ast.iter_fields(stmt):
        if name in _NESTED_BLOCKS:
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, ast.AST):
                out.append(item)
    return out


def _join(a: bool | None, b: bool | None) -> bool | None:
    """Merge two paths; `None` means the path never reaches the join (raise/return/break)."""
    if a is None:
        return b
    if b is None:
        return a
    return a and b


def _flow(stmts: list[ast.stmt], verified: bool | None, bad: list[int]) -> bool | None:
    state = verified
    dead = state is None
    for stmt in stmts:
        if dead:  # unreachable code is still held to the rule, as unverified
            _statement(stmt, False, bad)
            continue
        assert state is not None
        state = _statement(stmt, state, bad)
        dead = state is None
    return state


def _statement(stmt: ast.stmt, verified: bool, bad: list[int]) -> bool | None:
    """Check one statement; return whether verification definitely happened once it completes."""
    for expr in _own_expressions(stmt):
        _scan(expr, verified, bad)
    if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        _flow(stmt.body, False, bad)  # a new scope: an outer verify proves nothing inside it
        return verified
    if isinstance(stmt, ast.Raise | ast.Return | ast.Break | ast.Continue):
        return None
    if is_verify_statement(stmt):
        return True
    if isinstance(stmt, ast.If):
        return _join(_flow(stmt.body, verified, bad), _flow(stmt.orelse, verified, bad))
    if isinstance(stmt, ast.While | ast.For | ast.AsyncFor):
        _flow(stmt.body, verified, bad)  # may run zero times: nothing inside is credited after
        _flow(stmt.orelse, verified, bad)
        return verified
    if isinstance(stmt, ast.With | ast.AsyncWith):
        _flow(stmt.body, verified, bad)  # a context manager may swallow the exception: not credited
        return verified
    if isinstance(stmt, ast.Try | ast.TryStar):
        return _try(stmt, verified, bad)
    if isinstance(stmt, ast.Match):
        paths: list[bool | None] = []
        for case in stmt.cases:
            if case.guard is not None:
                _scan(case.guard, verified, bad)
            paths.append(_flow(case.body, verified, bad))
        irrefutable = any(
            isinstance(c.pattern, ast.MatchAs) and c.pattern.pattern is None and c.guard is None
            for c in stmt.cases
        )
        merged: bool | None = None if irrefutable else verified
        for path in paths:
            merged = _join(merged, path)
        return merged
    return verified


def _try(stmt: ast.Try | ast.TryStar, verified: bool, bad: list[int]) -> bool | None:
    body_end = _flow(stmt.body, verified, bad)
    normal = _flow(stmt.orelse, body_end, bad)  # the else clause runs only after a clean body
    merged = normal
    for handler in stmt.handlers:
        if handler.type is not None:
            _scan(handler.type, verified, bad)
        # The body may have raised before (or inside) its verify, so a handler starts unverified
        # unless verified before the `try`: a handler that swallows a failed verify and falls
        # through leaves the code after the `try` unverified.
        merged = _join(merged, _flow(handler.body, verified, bad))
    final_end = _flow(stmt.finalbody, verified, bad)
    if merged is None or final_end is None:
        return None
    return merged or final_end


def unverified_provider_calls(source: str) -> list[int]:
    """Lines where the provider (`.generate`) is reached on a path where no `.verify(...)` statement
    has definitely completed first.

    A forward control-flow pass over each scope (module, function, class) carrying one fact,
    "a bare `verify(...)` statement has run on every path to here". `if`/`try` branches are
    intersected (a branch that raises or returns is ignored), so a conditional verify, a verify in
    one `else` arm, or a handler that swallows a failed verify does not count. Loops, `with` and
    `match` are credited only for what runs before them: a verify inside them does not count
    afterwards. A nested function or lambda starts unverified. Limitations: it proves a `.verify`
    call dominates, not that the receiver is the real verifier or its arguments are right (the
    egress and order tests cover that), and a constant-false condition is not folded.
    """
    bad: list[int] = []
    _flow(ast.parse(source).body, False, bad)
    return sorted(set(bad))


def call_lines(source: str, attr: str) -> list[int]:
    return sorted(n.lineno for n in ast.walk(ast.parse(source)) if is_call(n, attr))


def swallowing_handlers(source: str) -> list[int]:
    """Bare `except:` and `except Exception/BaseException`: these hide programming errors."""
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


def content_in_errors(source: str) -> list[int]:
    """`raise X(<expression that formats or names content>)` and `detail=` keywords doing the same."""
    bad: list[int] = []

    def risky(expr: ast.AST) -> bool:
        for n in ast.walk(expr):
            if isinstance(n, ast.JoinedStr) or (
                isinstance(n, ast.BinOp) and isinstance(n.op, ast.Mod)
            ):
                return True
            if is_call(n, "format") or is_call(n, "reveal"):
                return True
            if isinstance(n, ast.Name) and n.id in CONTENT_NAMES:
                return True
            if isinstance(n, ast.Attribute) and n.attr in CONTENT_NAMES:
                return True
        return False

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            args = [*node.exc.args, *(k.value for k in node.exc.keywords)]
            if any(risky(a) for a in args):
                bad.append(node.lineno)
        elif isinstance(node, ast.keyword) and node.arg == "detail" and risky(node.value):
            bad.append(node.value.lineno)
    return sorted(set(bad))


def reveal_calls(source: str) -> list[int]:
    return call_lines(source, "reveal")


def use_case_classes(source: str) -> set[str]:
    return {
        n.name
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.ClassDef)
        and any(
            isinstance(d, ast.Call) and getattr(d.func, "id", "") in {"public", "requires"}
            for d in n.decorator_list
        )
    }


# ---- the detectors catch what they claim to --------------------------------------------------------


def test_the_detectors_catch_synthetic_violations() -> None:
    assert unverified_provider_calls("def f(p):\n    p.generate(r)\n") == [2]
    assert unverified_provider_calls("def f(p, v):\n    p.generate(r)\n    v.verify(t)\n") == [2]
    assert (
        unverified_provider_calls(
            "def f(p, v):\n    v.verify(t)\n    while True:\n        try:\n            return p.generate(r)\n        except E:\n            pass\n"
        )
        == []
    )
    assert unverified_provider_calls(
        "def f(p, v):\n    if x:\n        v.verify(t)\n    else:\n        p.generate(r)\n"
    ) == [5]
    assert imports_prefix(
        "from lda.modules.access_control.api import X", "lda.modules.access_control"
    )
    assert imports_prefix("import lda.bootstrap.audit", "lda.bootstrap")
    assert not imports_prefix(
        "from lda.modules.access_control_x import X", "lda.modules.access_control"
    )
    assert roots("import openai") == {"openai"} and roots("from anthropic import X") == {
        "anthropic"
    }
    assert "tools" in identifiers("def f(tools): pass") and "tool_choice" in identifiers(
        "f(tool_choice=1)"
    )
    assert "tools" in identifiers("class A:\n    tools: list = []")
    assert "lru_cache" in identifiers("from functools import lru_cache")
    assert swallowing_handlers("try:\n    x()\nexcept Exception:\n    pass\n") == [3]
    assert swallowing_handlers("try:\n    x()\nexcept:\n    pass\n") == [3]
    assert swallowing_handlers("try:\n    x()\nexcept ProviderFailure:\n    pass\n") == []
    assert content_in_errors("raise E(f'bad {x}')") == [1]
    assert content_in_errors("raise E('bad %s' % x)") == [1]
    assert content_in_errors("raise E(text)") == [1]
    assert content_in_errors("raise E(CODE, detail=command.text)") == [1]
    assert content_in_errors("raise E(CODE, detail=t.reveal())") == [1]
    assert content_in_errors("raise E(codes.INVALID_REQUEST)") == []
    assert reveal_calls("x = s.reveal()") == [1]
    assert use_case_classes("@public(reason='x')\nclass A: pass\nclass B: pass") == {"A"}


def test_verification_must_dominate_every_provider_call() -> None:
    """Textual order is not proof: a verify that can be skipped, swallowed or deferred is rejected."""

    def bad(body: str) -> list[int]:
        return unverified_provider_calls("def f(p, v):\n" + body)

    # The reported defect: verification is conditional, the provider call is not.
    assert bad("    if c:\n        v.verify(t, e)\n    p.generate(r)\n") == [4]
    assert bad(
        "    if c:\n        v.verify(t, e)\n    else:\n        pass\n    p.generate(r)\n"
    ) == [6]
    assert bad("    for _ in xs:\n        v.verify(t, e)\n    p.generate(r)\n") == [4]
    assert bad("    while c:\n        v.verify(t, e)\n    p.generate(r)\n") == [4]
    assert bad("    with m:\n        v.verify(t, e)\n    p.generate(r)\n") == [4]
    # A failed verification that is swallowed (or only logged) must not unlock the provider.
    assert bad(
        "    try:\n        v.verify(t, e)\n    except E:\n        pass\n    p.generate(r)\n"
    ) == [6]
    assert bad("    try:\n        v.verify(t, e)\n    except E:\n        p.generate(r)\n") == [5]
    assert (
        bad("    try:\n        v.verify(t, e)\n    finally:\n        pass\n    p.generate(r)\n")
        == []
    )
    # A verify that is not a bare statement proves nothing.
    assert bad("    ok = c and v.verify(t, e)\n    p.generate(r)\n") == [3]
    assert bad("    x = v.verify(t, e) if c else None\n    p.generate(r)\n") == [3]
    assert bad("    assert v.verify(t, e)\n    p.generate(r)\n") == [3]
    assert bad("    p.generate(v.verify(t, e))\n") == [2]
    assert bad(
        "    match c:\n        case 1:\n            v.verify(t, e)\n    p.generate(r)\n"
    ) == [5]
    # Verify after, inside a deferred callable, or in another scope does not count.
    assert bad("    p.generate(r)\n    v.verify(t, e)\n") == [2]
    assert bad("    v.verify(t, e)\n    g = lambda: p.generate(r)\n") == [3]
    assert bad("    v.verify(t, e)\n    def inner():\n        return p.generate(r)\n") == [4]
    assert unverified_provider_calls("v.verify(t, e)\ndef f(p):\n    p.generate(r)\n") == [3]
    # Aliasing or reflection around the call is the provider call too.
    assert bad("    g = p.generate\n    v.verify(t, e)\n    g(r)\n") == [2]
    assert bad("    getattr(p, 'generate')(r)\n") == [2]
    # Valid patterns are preserved.
    assert bad("    v.verify(t, e)\n    p.generate(r)\n") == []
    assert bad("    if c:\n        raise E\n    v.verify(t, e)\n    return p.generate(r)\n") == []
    assert (
        bad(
            "    if c:\n        v.verify(t, e)\n    else:\n        v.verify(t, e)\n    p.generate(r)\n"
        )
        == []
    )
    assert (
        bad(
            "    if not c:\n        raise E\n    else:\n        v.verify(t, e)\n    p.generate(r)\n"
        )
        == []
    )
    assert (
        bad(
            "    try:\n        v.verify(t, e)\n    except E:\n        raise F from None\n    p.generate(r)\n"
        )
        == []
    )
    assert (
        bad(
            "    while True:\n        v.verify(t, e)\n        try:\n            return p.generate(r)\n        except E:\n            continue\n"
        )
        == []
    )
    assert bad("    with m:\n        v.verify(t, e)\n        p.generate(r)\n") == []
    assert (
        unverified_provider_calls(
            "async def f(p, v):\n    await v.verify(t, e)\n    return await p.generate(r)\n"
        )
        == []
    )


# ---- dependencies -------------------------------------------------------------------------------------


def test_the_dag_and_the_manifest_declare_no_dependencies() -> None:
    dag, _ = load_dag(ROOT)
    manifests, _ = load_manifests(ROOT)
    assert list(dag["ai_gateway"]) == []
    assert list(manifests["ai_gateway"].depends_on) == []
    assert manifests["ai_gateway"].owns_schema == ""  # no schema, so no persistence


def test_the_dag_file_still_gives_ai_gateway_no_edges_and_adds_no_module() -> None:
    raw = tomllib.loads((ROOT / "docs/architecture/module-dag.toml").read_text(encoding="utf-8"))
    assert raw["modules"]["ai_gateway"]["depends_on"] == []
    assert len(raw["modules"]) == 15


def test_no_file_in_the_module_imports_access_control_bootstrap_or_another_module() -> None:
    for path in module_files():
        source = path.read_text(encoding="utf-8")
        assert not imports_prefix(source, "lda.bootstrap"), rel(path)
        assert not imports_prefix(source, "lda.modules.access_control"), rel(path)
        others = {
            t for t in imports(source) if t.startswith("lda.modules.") and not t.startswith(PACKAGE)
        }
        assert not others, (rel(path), others)


def test_the_bootstrap_receives_the_verifier_as_the_shared_port_only() -> None:
    source = BOOTSTRAP_FILE.read_text(encoding="utf-8")
    assert not imports_prefix(source, "lda.modules.access_control")
    assert not imports_prefix(source, "lda.bootstrap.access_control")
    assert "lda.shared.ports.authorization.ProofVerifier" in imports(source)


def test_only_bootstrap_imports_the_gateway_from_production_code() -> None:
    for path in py_files(SRC):
        parts = set(path.relative_to(SRC).parts)
        if path.is_relative_to(GW) or {"tests", "testing"} & parts or path == BOOTSTRAP_FILE:
            continue
        source = path.read_text(encoding="utf-8")
        if path.is_relative_to(SRC / "modules/grounded_generation") or path == (
            SRC / "bootstrap/grounded_generation.py"
        ):
            # Step 14 (ADR-029) is the sanctioned consumer: the public api only, never internals.
            assert all(
                t == f"{PACKAGE}.api" or t.startswith(f"{PACKAGE}.api.")
                for t in imports(source)
                if t.startswith(PACKAGE)
            ), rel(path)
            continue
        assert not imports_prefix(source, PACKAGE), rel(path)  # later steps widen this on purpose


def test_only_the_gateway_and_its_bootstrap_name_the_provider_port() -> None:
    for path in py_files(SRC):
        parts = set(path.relative_to(SRC).parts)
        if path.is_relative_to(GW) or {"tests", "testing"} & parts or path == BOOTSTRAP_FILE:
            continue
        assert not PORT_NAMES & identifiers(path.read_text(encoding="utf-8")), rel(path)


# ---- the egress chokepoint ------------------------------------------------------------------------------


def test_every_provider_call_is_preceded_by_a_proof_verification() -> None:
    assert unverified_provider_calls(USE_CASE_FILE.read_text(encoding="utf-8")) == []


def test_only_the_egress_use_case_calls_the_provider_port() -> None:
    callers = [
        rel(p) for p in production_files() if call_lines(p.read_text(encoding="utf-8"), "generate")
    ]
    assert callers == [rel(USE_CASE_FILE)]
    assert len(call_lines(USE_CASE_FILE.read_text(encoding="utf-8"), "generate")) == 1


def test_there_is_exactly_one_use_case_and_it_is_the_egress() -> None:
    found: set[str] = set()
    for path in production_files():
        found |= use_case_classes(path.read_text(encoding="utf-8"))
    assert found == {"GenerateText"}


def test_the_order_is_validate_then_verify_then_rate_limit_then_provider() -> None:
    source = USE_CASE_FILE.read_text(encoding="utf-8")
    verify, acquire, generate = (call_lines(source, a) for a in ("verify", "_acquire", "generate"))
    validate = call_lines(source, "_validate")
    assert len(verify) == len(acquire) == len(generate) == len(validate) == 1
    assert validate[0] < verify[0] < acquire[0] < generate[0]


def test_only_pre_response_failures_can_be_retried() -> None:
    source = USE_CASE_FILE.read_text(encoding="utf-8")
    assert "retryable" in identifiers(source)
    kinds = (GW / "domain/failures.py").read_text(encoding="utf-8")
    assert "return self is FailureKind.UNREACHABLE" in kinds


# ---- no SDK, no tools, no persistence, no cache, no logging -----------------------------------------------


def test_no_provider_sdk_or_network_library_is_imported_anywhere_in_the_module() -> None:
    for path in module_files():
        assert not roots(path.read_text(encoding="utf-8")) & SDK_ROOTS, rel(path)


def test_no_sdk_is_declared_and_the_adapters_package_is_empty() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    for sdk in ("openai", "anthropic", "cohere", "voyageai", "mistralai", "google-genai"):
        assert f'"{sdk}' not in pyproject, sdk
    adapters = GW / "infrastructure/adapters"
    assert [p.name for p in adapters.iterdir() if p.name != "__pycache__"] == ["__init__.py"]


def test_the_model_has_no_tools() -> None:
    for path in module_files():
        names = identifiers(path.read_text(encoding="utf-8"))
        assert not names & TOOL_NAMES, (rel(path), names & TOOL_NAMES)


def test_nothing_is_persisted_cached_or_stored() -> None:
    for path in module_files():
        source = path.read_text(encoding="utf-8")
        assert not roots(source) & PERSISTENCE_ROOTS, rel(path)
        assert not identifiers(source) & CACHE_NAMES, rel(path)
        for banned in ("lda.shared.ports.object_store", "lda.shared.ports.unit_of_work"):
            assert not imports_prefix(source, banned), (rel(path), banned)
        assert not imports_prefix(source, "lda.platform.database"), rel(path)
    assert not (GW / "infrastructure/persistence").exists()
    assert not list(GW.rglob("migrations"))


def test_no_logging_audit_clock_or_threads_in_production_code() -> None:
    for path in production_files():
        source = path.read_text(encoding="utf-8")
        assert not roots(source) & (LOGGING_ROOTS | RUNTIME_ROOTS), rel(path)
        assert "print" not in identifiers(source), rel(path)
        assert not imports_prefix(source, "lda.shared.ports.audit"), rel(path)
        assert not imports_prefix(source, "lda.platform"), rel(path)


def test_no_production_code_swallows_exceptions_broadly() -> None:
    for path in production_files():
        assert swallowing_handlers(path.read_text(encoding="utf-8")) == [], rel(path)


def test_no_error_is_built_from_content_and_reveal_is_confined() -> None:
    for path in module_files():
        source = path.read_text(encoding="utf-8")
        if "tests" not in path.relative_to(GW).parts:
            assert content_in_errors(source) == [], rel(path)
        if reveal_calls(source):
            assert rel(path).endswith(REVEAL_ALLOWED) or "tests" in path.relative_to(GW).parts, rel(
                path
            )
