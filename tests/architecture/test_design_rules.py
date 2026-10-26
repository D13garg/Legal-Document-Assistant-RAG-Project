from __future__ import annotations

import pytest

from repo_builder import MakeRepo

IMPORTS = "from lda.shared.kernel.declarations import audited, not_audited, public, requires\n"

CASES = [
    ("AG050", "domain/utils.py", "x = 1\n"),
    ("AG050", "domain/x.py", "class DocumentManager: ...\n"),
    ("AG050", "domain/x.py", "class AuthService: ...\n"),
    ("AG050", "application/x.py", "class Orchestrator: ...\n"),
    ("AG051", "domain/x.py", "class X:\n    def __init__(self, a, b, c, d, e, f, g, h): ...\n"),
    (
        "AG051",
        "domain/x.py",
        "class X:\n" + "".join(f"    def m{i}(self): ...\n" for i in range(401)),
    ),
    (
        "AG051",
        "application/x.py",
        "from typing import Protocol\nclass P(Protocol):\n"
        + "".join(f"    def m{i}(self) -> None: ...\n" for i in range(13)),
    ),
    (
        "AG052",
        "application/use_cases/x.py",
        "class Do:\n    def execute(self): ...\n    def other(self): ...\n",
    ),
    ("AG052", "application/use_cases/x.py", "class Do:\n    def run(self): ...\n"),
    ("AG053", "application/use_cases/x.py", "class Do:\n    def execute(self): ...\n"),
    (
        "AG053",
        "application/queries/x.py",
        IMPORTS + "@public(reason='r')\nclass Do:\n    def execute(self): ...\n",
    ),
    ("AG054", "domain/x.py", "class A: ...\nclass B(A): ...\nclass C(B): ...\nclass D(C): ...\n"),
]


@pytest.mark.parametrize(("rule", "rel", "content"), CASES)
def test_violation_detected(make_repo: MakeRepo, rule: str, rel: str, content: str) -> None:
    repo = make_repo()
    repo.module_file("a", rel, content)
    assert rule in repo.violations()


def test_compliant_use_case_passes(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file(
        "a",
        "application/use_cases/x.py",
        IMPORTS
        + "@requires('doc.read')\n@audited('doc.read')\nclass Do:\n    def __init__(self, a, b): ...\n\n"
        + "    def execute(self, uow):\n"
        + "        uow.audit.record(AuditFact(action='doc.read'))\n"
        + "        uow.commit()\n",
    )
    assert repo.violations() == []


def test_markers_on_class_are_accepted(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file(
        "a",
        "application/use_cases/x.py",
        IMPORTS
        + "@public(reason='health')\n@not_audited(reason='noise')\nclass Do:\n    def execute(self): ...\n",
    )
    assert repo.violations() == []


def test_depth_two_inheritance_and_exceptions_are_fine(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file(
        "a",
        "domain/x.py",
        "class A: ...\nclass B(A): ...\nclass C(B): ...\nclass E1(Exception): ...\nclass E2(E1): ...\nclass E3(E2): ...\n",
    )
    assert repo.violations() == []
