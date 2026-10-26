"""AG010-AG012 module surface/edges, AG020 direction, AG030-AG033 isolation, AG040 config, AG090."""

from __future__ import annotations

import ast
import sys
from fnmatch import fnmatch

from archguard import policy
from archguard.manifest import Manifest
from archguard.model import Violation
from archguard.source import ImportRef, SourceFile, Zone, classify_dotted

_STDLIB = sys.stdlib_module_names


def _expand(tgt: Zone, ref: ImportRef) -> list[Zone]:
    """`from lda.modules.m import api` imports the layer `api`; expand names into layers."""
    if tgt.kind in ("lda_other", "platform") and not tgt.layer and ref.names:
        # `from lda.platform import testing`, `from lda.shared import ports`, `from lda import platform`
        # import a submodule through its parent: classify the submodule, not the parent.
        return [classify_dotted(f"{ref.module}.{n}") or tgt for n in ref.names]
    if tgt.kind == "module" and tgt.layer == "root" and ref.names:
        return [
            Zone(
                "module",
                tgt.module,
                n
                if n in ("api", "testing", *policy.MODULE_LAYERS, "tests", "config")
                else "unknown",
            )
            for n in ref.names
        ]
    return [tgt]


def _direction(src: Zone, tgt: Zone) -> tuple[str, str] | None:
    """Return (rule, message) when src may not import tgt, else None."""
    k = tgt.kind
    if k in ("lda_other", "other"):
        return None
    if src.kind == "tests_top":
        return None
    if k == "modules_pkg":
        return "AG010", "import module packages only via lda.modules.<name>.api"
    if k == "platform" and tgt.layer == "testing":
        is_double_consumer = (src.kind == "module" and src.layer in ("tests", "testing")) or (
            src.kind == "platform" and src.layer == "testing"
        )
        if not is_double_consumer:
            return (
                "AG020",
                "lda.platform.testing (in-memory doubles) may be imported only by tests and testing/",
            )
        return None
    ok = False
    if src.kind == "kernel":
        ok = k == "kernel"
    elif src.kind == "ports":
        ok = k in ("kernel", "ports")
    elif src.kind == "platform":
        ok = k in ("kernel", "ports", "platform")
    elif src.kind == "bootstrap":
        ok = k in ("kernel", "ports", "platform", "bootstrap") or (
            k == "module" and tgt.layer in policy.BOOTSTRAP_MODULE_LAYERS
        )
    elif src.kind == "apps":
        ok = k in ("kernel", "ports", "platform", "bootstrap", "apps") or (
            k == "module" and tgt.layer in policy.APPS_MODULE_LAYERS
        )
    elif src.kind == "module":
        if k == "kernel":
            ok = True
        elif k == "ports":
            ok = src.layer in policy.PORTS_ALLOWED_FROM
        elif k == "platform":
            ok = src.layer in policy.PLATFORM_ALLOWED_FROM
        elif k == "module" and tgt.module == src.module:
            ok = src.layer == "tests" or tgt.layer in policy.SAME_MODULE_ALLOWED.get(
                src.layer, frozenset()
            )
        elif k == "module":
            if tgt.layer == "api":
                ok = src.layer in policy.CROSS_MODULE_API_FROM
            elif tgt.layer == "testing":
                ok = src.layer in policy.CROSS_MODULE_TESTING_FROM
            else:
                return (
                    "AG010",
                    f"cross-module import of {tgt.module}.{tgt.layer or 'root'}: only <module>.api (and .testing from tests) is public",
                )
            if not ok:
                return "AG020", f"layer '{src.layer}' may not import {tgt.module}.{tgt.layer}"
            return None
    if ok:
        return None
    where = f"{src.kind}{('/' + src.layer) if src.layer else ''}"
    what = f"{k}{('/' + tgt.layer) if tgt.layer else ''}"
    return "AG020", f"{where} may not import {what}"


def check_imports(files: list[SourceFile], manifests: dict[str, Manifest]) -> list[Violation]:
    out: list[Violation] = []
    used_edges: dict[str, set[str]] = {}
    for sf in files:
        for ref in sf.imports:
            if not ref.module:
                continue
            top = ref.module.split(".")[0]
            if top in policy.INTERNAL_TOPS:
                tgt0 = classify_dotted(ref.module)
                if tgt0 is None:
                    continue
                if tgt0.kind in ("apps", "tests_top") and sf.zone.kind != tgt0.kind:
                    out.append(
                        Violation(
                            "AG020", sf.rel, ref.line, f"{sf.zone.kind} may not import {tgt0.kind}"
                        )
                    )
                    continue
                for tgt in _expand(tgt0, ref):
                    bad = _direction(sf.zone, tgt)
                    if bad:
                        out.append(Violation(bad[0], sf.rel, ref.line, bad[1]))
                    if (
                        sf.zone.kind == "module"
                        and tgt.kind == "module"
                        and tgt.module != sf.zone.module
                    ):
                        used_edges.setdefault(sf.zone.module, set()).add(tgt.module)
                        declared = manifests.get(sf.zone.module)
                        if declared is not None and tgt.module not in declared.depends_on:
                            out.append(
                                Violation(
                                    "AG011",
                                    sf.rel,
                                    ref.line,
                                    f"{sf.zone.module} imports {tgt.module} but module.toml does not declare it",
                                )
                            )
            elif top not in _STDLIB:
                out.extend(_check_external(sf, ref, top))
    for name, m in manifests.items():
        for dep in m.depends_on:
            if dep not in used_edges.get(name, set()):
                out.append(
                    Violation(
                        "AG012",
                        m.path,
                        0,
                        f"declared dependency '{dep}' is never imported; remove it",
                    )
                )
    return out


def _purity_key(zone: Zone) -> str | None:
    if zone.kind in ("kernel", "ports"):
        return zone.kind
    if zone.kind == "module" and zone.layer in policy.EXTERNAL_ALLOWLIST:
        return zone.layer
    return None


def _check_external(sf: SourceFile, ref: ImportRef, top: str) -> list[Violation]:
    if sf.zone.is_test:
        return []
    key = _purity_key(sf.zone)
    if key is not None and top not in policy.EXTERNAL_ALLOWLIST[key]:
        return [
            Violation(
                "AG030",
                sf.rel,
                ref.line,
                f"'{ref.module}' is third-party; {key} code may use only the standard library{' and ' + ', '.join(sorted(policy.EXTERNAL_ALLOWLIST[key])) if policy.EXTERNAL_ALLOWLIST[key] else ''}",
            )
        ]
    out: list[Violation] = []
    for r in policy.RESTRICTIONS:
        if any(fnmatch(ref.module, p) for p in r.patterns) and not any(
            fnmatch(sf.rel, g) for g in r.allowed_paths
        ):
            out.append(Violation(r.rule, sf.rel, ref.line, f"'{ref.module}': {r.why}"))
    return out


def check_config_access(sf: SourceFile) -> list[Violation]:
    if sf.zone.is_test or sf.rel.startswith(policy.CONFIG_PATH_PREFIX):
        return []
    out: list[Violation] = []
    aliases: set[str] = set()
    for node in ast.walk(sf.tree):
        if isinstance(node, ast.Import):
            aliases.update(a.asname or a.name for a in node.names if a.name == "os")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "os" and any(a.name in policy.ENV_ATTRS for a in node.names):
                out.append(
                    Violation(
                        "AG040",
                        sf.rel,
                        node.lineno,
                        "raw environment access; use the module config slice",
                    )
                )
            if any(a.name == "BaseSettings" for a in node.names):
                out.append(
                    Violation(
                        "AG040",
                        sf.rel,
                        node.lineno,
                        "BaseSettings is allowed only in platform/configuration",
                    )
                )
    for node in ast.walk(sf.tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
            and node.attr in policy.ENV_ATTRS
        ):
            out.append(
                Violation(
                    "AG040",
                    sf.rel,
                    node.lineno,
                    f"os.{node.attr}: raw environment access; use the module config slice",
                )
            )
    return out


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def check_restricted_constructors(sf: SourceFile) -> list[Violation]:
    out: list[Violation] = []
    for node in ast.walk(sf.tree):
        names: list[str | None]
        if isinstance(node, ast.Call):
            names = [_callee_name(node.func)]
        elif isinstance(node, ast.ClassDef):
            names = [_callee_name(b) for b in node.bases]
        else:
            continue
        for name in names:
            allowed = policy.RESTRICTED_CONSTRUCTORS.get(name or "")
            if allowed is not None and not any(sf.rel.startswith(p) for p in allowed):
                out.append(
                    Violation(
                        "AG090",
                        sf.rel,
                        node.lineno,
                        f"{name} may only be created/extended in {', '.join(allowed)} (use its testing builders)",
                    )
                )
    return out
