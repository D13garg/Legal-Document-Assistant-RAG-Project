"""AG034 schema isolation in module persistence code; AG055 repositories only via the UnitOfWork."""

from __future__ import annotations

import ast
import re
from fnmatch import fnmatch

from archguard import policy
from archguard.model import Violation
from archguard.rules.design import _base_name
from archguard.source import SourceFile

_SQL = re.compile(policy.SCHEMA_SQL_PATTERN)


def check_schema_isolation(sf: SourceFile) -> list[Violation]:
    if sf.zone.kind != "module" or not fnmatch(sf.rel, policy.PERSISTENCE_PATH):
        return []
    own = sf.zone.module
    out: list[Violation] = []
    for node in ast.walk(sf.tree):
        if isinstance(node, ast.keyword) and node.arg == "schema":
            value = node.value
            if (
                isinstance(value, ast.Constant)
                and isinstance(value.value, str)
                and value.value != own
            ):
                out.append(
                    Violation(
                        "AG034",
                        sf.rel,
                        value.lineno,
                        f"schema '{value.value}' is not this module's schema '{own}'",
                    )
                )
        elif isinstance(node, ast.Call) and _base_name(node.func) == "ForeignKey" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                parts = arg.value.split(".")
                if len(parts) == 3 and parts[0] != own:
                    out.append(
                        Violation(
                            "AG034",
                            sf.rel,
                            arg.lineno,
                            f"foreign key into schema '{parts[0]}': no cross-schema FKs",
                        )
                    )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _SQL.finditer(node.value):
                if match.group(1) != own:
                    out.append(
                        Violation(
                            "AG034",
                            sf.rel,
                            node.lineno,
                            f"SQL references schema '{match.group(1)}': no cross-schema reads",
                        )
                    )
    return out


def check_repository_instantiation(sf: SourceFile) -> list[Violation]:
    if sf.zone.is_test or any(fnmatch(sf.rel, g) for g in policy.REPOSITORY_INSTANTIATION_ALLOWED):
        return []
    out: list[Violation] = []
    for node in ast.walk(sf.tree):
        if isinstance(node, ast.Call):
            name = _base_name(node.func)
            if name and name.endswith("Repository"):
                out.append(
                    Violation(
                        "AG055",
                        sf.rel,
                        node.lineno,
                        f"{name} may be instantiated only by the module's UnitOfWork adapter",
                    )
                )
    return out
