"""AG050 naming, AG051 size/arity, AG052 use-case shape, AG053 declarations, AG054 inheritance,
AG056 audited use cases emit their declared action before commit (ADR-019)."""

from __future__ import annotations

import ast
import re
from fnmatch import fnmatch

from archguard import policy
from archguard.model import Violation
from archguard.source import SourceFile


def _base_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _classes(sf: SourceFile) -> list[ast.ClassDef]:
    return [n for n in ast.walk(sf.tree) if isinstance(n, ast.ClassDef)]


def _methods(cls: ast.ClassDef) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [n for n in cls.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]


def _decorator_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        n = _base_name(target)
        if n:
            names.add(n)
    return names


def check_naming(sf: SourceFile) -> list[Violation]:
    out: list[Violation] = []
    for part in sf.rel.split("/"):
        stem = part.removesuffix(".py").lower()
        if stem in policy.BANNED_PATH_NAMES:
            out.append(
                Violation(
                    "AG050",
                    sf.rel,
                    0,
                    f"'{part}' is a banned dumping-ground name; name it for what it does",
                )
            )
    for cls in _classes(sf):
        if cls.name.endswith(policy.BANNED_CLASS_SUFFIXES):
            out.append(
                Violation(
                    "AG050",
                    sf.rel,
                    cls.lineno,
                    f"class '{cls.name}' uses a banned catch-all suffix; use a use-case/role name",
                    cls.name,
                )
            )
    return out


def check_class_metrics(sf: SourceFile) -> list[Violation]:
    out: list[Violation] = []
    for cls in _classes(sf):
        loc = (cls.end_lineno or cls.lineno) - cls.lineno + 1
        if loc > policy.MAX_CLASS_LOC:
            out.append(
                Violation(
                    "AG051",
                    sf.rel,
                    cls.lineno,
                    f"class has {loc} lines (max {policy.MAX_CLASS_LOC})",
                    cls.name,
                )
            )
        for m in _methods(cls):
            if m.name == "__init__":
                a = m.args
                n = (
                    len(a.posonlyargs)
                    + len(a.args)
                    + len(a.kwonlyargs)
                    - 1
                    + bool(a.vararg)
                    + bool(a.kwarg)
                )
                if n > policy.MAX_CTOR_PARAMS:
                    out.append(
                        Violation(
                            "AG051",
                            sf.rel,
                            m.lineno,
                            f"constructor has {n} parameters (max {policy.MAX_CTOR_PARAMS}); the class does too much",
                            cls.name,
                        )
                    )
        if any(_base_name(b) == "Protocol" for b in cls.bases):
            count = sum(1 for m in _methods(cls) if not m.name.startswith("__"))
            if count > policy.MAX_PORT_METHODS:
                out.append(
                    Violation(
                        "AG051",
                        sf.rel,
                        cls.lineno,
                        f"port has {count} methods (max {policy.MAX_PORT_METHODS}); segregate it",
                        cls.name,
                    )
                )
    return out


_MARKERS = policy.AUTHZ_DECORATORS | policy.AUDIT_DECORATORS
_IDENTIFIER = re.compile(policy.DECLARATION_IDENTIFIER_PATTERN)
_FuncOrClass = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def _marker_imports(tree: ast.Module) -> tuple[dict[str, str], set[str]]:
    """Local names bound to kernel markers, and local aliases of the declarations module."""
    names: dict[str, str] = {}
    modules: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        if node.module == policy.DECLARATIONS_MODULE:
            for a in node.names:
                if a.name in _MARKERS:
                    names[a.asname or a.name] = a.name
        elif node.module == policy.DECLARATIONS_PARENT:
            modules.update(a.asname or a.name for a in node.names if a.name == "declarations")
    return names, modules


class _Marker:
    """One decorator that claims to be a marker; ``kind`` is None if it is not the kernel's."""

    def __init__(self, dec: ast.expr, kind: str | None) -> None:
        self.dec = dec
        self.kind = kind


def _markers_on(node: _FuncOrClass, names: dict[str, str], modules: set[str]) -> list[_Marker]:
    found: list[_Marker] = []
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        kind: str | None = None
        if isinstance(target, ast.Name):
            kind = names.get(target.id)
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id in modules
            and target.attr in _MARKERS
        ):
            kind = target.attr
        if kind is not None or _base_name(target) in _MARKERS:
            found.append(_Marker(dec, kind))
    return found


def _literal(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _identifier_problem(value: str | None, what: str) -> str | None:
    if value is None:
        return f"{what} must be a string literal"
    if len(value) > policy.DECLARATION_MAX_IDENTIFIER_LENGTH or not _IDENTIFIER.fullmatch(value):
        return f"{what} {value!r} must be a lowercase dotted identifier (max {policy.DECLARATION_MAX_IDENTIFIER_LENGTH})"
    return None


def _reason_problem(value: str | None) -> str | None:
    if value is None:
        return "reason must be a string literal"
    if (
        not value.strip()
        or len(value) > policy.DECLARATION_MAX_REASON_LENGTH
        or not value.isprintable()
    ):
        return f"reason must be non-empty single-line text (max {policy.DECLARATION_MAX_REASON_LENGTH})"
    return None


def _argument_problem(kind: str, dec: ast.expr) -> str | None:
    if not isinstance(dec, ast.Call):
        return f"@{kind} must be called"
    args, kws = dec.args, dec.keywords
    if any(isinstance(a, ast.Starred) for a in args) or any(k.arg is None for k in kws):
        return f"@{kind} takes no * or ** arguments"
    if kind == "requires":
        if kws or not args:
            return "@requires takes one or more positional string literals"
        values = [_literal(a) for a in args]
        for v in values:
            if (problem := _identifier_problem(v, "requirement")) is not None:
                return problem
        if len(set(values)) != len(values):
            return "@requires lists a requirement twice"
        return None
    if kind in ("public", "not_audited"):
        if args or [k.arg for k in kws] != ["reason"]:
            return f"@{kind} takes only reason='...'"
        return _reason_problem(_literal(kws[0].value))
    if len(args) + len(kws) != 1 or (kws and kws[0].arg != "action"):
        return "@audited takes exactly one action"
    return _identifier_problem(_literal(args[0] if args else kws[0].value), "action")


def _stray_markers(sf: SourceFile, allowed: set[ast.ClassDef]) -> list[Violation]:
    """Markers anywhere except on a use-case class (methods, functions, private/nested classes)."""
    names, modules = _marker_imports(sf.tree)
    out: list[Violation] = []
    for node in ast.walk(sf.tree):
        if isinstance(node, _FuncOrClass) and not (
            isinstance(node, ast.ClassDef) and node in allowed
        ):
            out.extend(
                Violation(
                    "AG053",
                    sf.rel,
                    m.dec.lineno,
                    "declaration markers are valid only on use-case classes (class level)",
                    node.name,
                )
                for m in _markers_on(node, names, modules)
            )
    return out


def check_marker_placement(sf: SourceFile) -> list[Violation]:
    """Markers outside use-case files are rejected (kernel definitions and tests excepted)."""
    if any(fnmatch(sf.rel, p) for p in policy.USE_CASE_PATH_PATTERNS):
        return []
    if sf.zone.kind == "kernel" or sf.zone.is_test:
        return []
    return _stray_markers(sf, set())


def check_use_cases(sf: SourceFile) -> list[Violation]:
    if not any(fnmatch(sf.rel, p) for p in policy.USE_CASE_PATH_PATTERNS):
        return []
    names, modules = _marker_imports(sf.tree)
    out: list[Violation] = []
    use_cases = [
        c for c in sf.tree.body if isinstance(c, ast.ClassDef) and not c.name.startswith("_")
    ]
    out.extend(_stray_markers(sf, set(use_cases)))
    for cls in use_cases:
        public = [m for m in _methods(cls) if not m.name.startswith("_")]
        if [m.name for m in public] != ["execute"]:
            out.append(
                Violation(
                    "AG052",
                    sf.rel,
                    cls.lineno,
                    "a use case has exactly one public method, named 'execute'",
                    cls.name,
                )
            )
            continue
        marks = _markers_on(cls, names, modules)
        for m in marks:
            if m.kind is None:
                out.append(
                    Violation(
                        "AG053",
                        sf.rel,
                        m.dec.lineno,
                        f"marker must be imported from {policy.DECLARATIONS_MODULE}",
                        cls.name,
                    )
                )
            elif (problem := _argument_problem(m.kind, m.dec)) is not None:
                out.append(Violation("AG053", sf.rel, m.dec.lineno, problem, cls.name))
        for group, hint in (
            (
                policy.AUTHZ_DECORATORS,
                "declare exactly one of @requires(...) or @public(reason=...)",
            ),
            (
                policy.AUDIT_DECORATORS,
                "declare exactly one of @audited(...) or @not_audited(reason=...)",
            ),
        ):
            if sum(1 for m in marks if m.kind in group) != 1:
                out.append(Violation("AG053", sf.rel, cls.lineno, hint, cls.name))
    return out


def _audit_chain(call: ast.Call, method: str) -> bool:
    """``<anything>.audit.<method>(...)``"""
    func = call.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == method
        and isinstance(func.value, ast.Attribute)
        and func.value.attr == "audit"
    )


def _is_commit(call: ast.Call) -> bool:
    return isinstance(call.func, ast.Attribute) and call.func.attr == "commit"


def _calls(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _emitted_action_problem(call: ast.Call, declared: str) -> str | None:
    """The argument of ``audit.record`` must be an inline ``AuditFact(action=<declared literal>)``."""
    if len(call.args) != 1 or call.keywords:
        return "audit.record takes exactly one inline AuditFact(...)"
    fact = call.args[0]
    if not (isinstance(fact, ast.Call) and _base_name(fact.func) == policy.AUDIT_FACT_TYPE):
        return f"audit.record must be given an inline {policy.AUDIT_FACT_TYPE}(...)"
    if any(k.arg is None for k in fact.keywords):
        return f"{policy.AUDIT_FACT_TYPE} takes no ** arguments"
    actions = [k.value for k in fact.keywords if k.arg == "action"]
    if len(actions) != 1 or _literal(actions[0]) != declared:
        return f"the emitted action must be the declared label {declared!r} as a string literal"
    return None


def check_audit_emission(sf: SourceFile) -> list[Violation]:
    """ADR-019 D1/D9: ``@audited(x)`` => emit ``AuditFact(action=x)`` before ``commit()``;
    ``@not_audited`` => never emit. Declarations stay metadata (ADR-034); this rule checks the code."""
    if not any(fnmatch(sf.rel, p) for p in policy.USE_CASE_PATH_PATTERNS):
        return []
    names, modules = _marker_imports(sf.tree)
    out: list[Violation] = []
    for cls in (c for c in sf.tree.body if isinstance(c, ast.ClassDef) and c.name[0] != "_"):
        marks = [
            m
            for m in _markers_on(cls, names, modules)
            if m.kind in policy.AUDIT_DECORATORS and _argument_problem(m.kind, m.dec) is None
        ]
        if len(marks) != 1:
            continue  # missing, doubled or malformed: AG053 reports it
        mark = marks[0]
        assert isinstance(mark.dec, ast.Call)  # noqa: S101 - _argument_problem accepted a call
        records = [c for c in _calls(cls) if _audit_chain(c, "record")]

        def bad(msg: str, line: int, cls: ast.ClassDef = cls) -> None:
            out.append(Violation("AG056", sf.rel, line, msg, cls.name))

        if mark.kind == "not_audited":
            out.extend(
                Violation(
                    "AG056",
                    sf.rel,
                    c.lineno,
                    "a @not_audited use case must not emit audit facts",
                    cls.name,
                )
                for c in records
            )
            continue
        declared = _literal(mark.dec.args[0] if mark.dec.args else mark.dec.keywords[0].value)
        if declared is None or not records:
            bad(
                f"@audited({declared!r}) but the use case never calls audit.record(...)", cls.lineno
            )
            continue
        for rec in records:
            if (problem := _emitted_action_problem(rec, declared)) is not None:
                bad(problem, rec.lineno)
        for method in _methods(cls):
            commits = [c.lineno for c in _calls(method) if _is_commit(c)]
            if commits and not any(r.lineno < min(commits) for r in records if r in _calls(method)):
                bad(
                    "audit.record(...) must run before commit() in the method that commits",
                    min(commits),
                )
    return out


def check_inheritance(files: list[SourceFile]) -> list[Violation]:
    known: dict[str, list[list[str]]] = {}
    located: list[tuple[SourceFile, ast.ClassDef]] = []
    for sf in files:
        for cls in _classes(sf):
            bases = [b for b in (_base_name(x) for x in cls.bases) if b]
            known.setdefault(cls.name, []).append(bases)
            located.append((sf, cls))

    def excluded(b: str) -> bool:
        return b in policy.INHERITANCE_EXCLUDED_BASES or b.endswith(("Error", "Exception"))

    cache: dict[str, int] = {}

    def depth(name: str, trail: frozenset[str] = frozenset()) -> int:
        if name in cache:
            return cache[name]
        if name in trail:
            return 0
        best = 0
        for bases in known.get(name, []):
            parents = [b for b in bases if not excluded(b) and b in known]
            if parents:
                best = max(best, 1 + max(depth(p, trail | {name}) for p in parents))
        cache[name] = best
        return best

    out: list[Violation] = []
    for sf, cls in located:
        d = depth(cls.name)
        if d > policy.MAX_INHERITANCE_DEPTH:
            out.append(
                Violation(
                    "AG054",
                    sf.rel,
                    cls.lineno,
                    f"inheritance depth {d} (max {policy.MAX_INHERITANCE_DEPTH}); prefer composition",
                    cls.name,
                )
            )
    return out
