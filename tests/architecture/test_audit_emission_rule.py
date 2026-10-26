"""AG056 (ADR-019 D1/D9): an audited use case emits its declared action before commit.

The markers stay pure metadata (ADR-034); this rule checks the code that the declaration promises.
"""

from __future__ import annotations

import pytest

from repo_builder import MakeRepo

UC = "application/use_cases/x.py"
IMPORTS = (
    "from lda.shared.kernel.declarations import audited, not_audited, public, requires\n"
    "from lda.shared.ports.audit import AuditFact\n"
)


def audited_case(body: str, action: str = "doc.read") -> str:
    return IMPORTS + f"@requires('doc.read')\n@audited('{action}')\nclass Do:\n" + body


def exempt_case(body: str) -> str:
    return IMPORTS + f"@public(reason='r')\n@not_audited(reason='noise')\nclass Do:\n{body}"


def ag056(files: dict[str, str], make_repo: MakeRepo) -> list[str]:
    repo = make_repo()
    for rel, content in files.items():
        repo.module_file("a", rel, content)
    return [v for v in repo.violations() if v == "AG056"]


EMIT = "        uow.audit.record(AuditFact(action='doc.read', tenant_id=t))\n"
VALID = {
    "emit then commit": audited_case(f"    def execute(self, uow):\n{EMIT}        uow.commit()\n"),
    "conditional emit": audited_case(
        "    def execute(self, uow):\n        if changed:\n    " + EMIT + "        uow.commit()\n"
    ),
    "emit inside with": audited_case(
        "    def execute(self, uow):\n        with self.u.begin(c) as uow:\n    "
        + EMIT.replace("        ", "        ", 1)
        + "            uow.commit()\n"
    ),
    "queries directory": audited_case(f"    def execute(self, uow):\n{EMIT}"),
    "emit without commit": audited_case(f"    def execute(self, uow):\n{EMIT}"),
    "not audited without emission": exempt_case(
        "    def execute(self, uow):\n        uow.commit()\n"
    ),
    "keyword action label": audited_case(
        "    def execute(self, uow):\n"
        "        uow.audit.record(AuditFact(action='doc.read'))\n        uow.commit()\n"
    ),
}


@pytest.mark.parametrize("name", list(VALID))
def test_compliant_use_cases_pass(make_repo: MakeRepo, name: str) -> None:
    assert ag056({UC: VALID[name]}, make_repo) == [], name


INVALID = {
    "audited but never emits": audited_case("    def execute(self, uow):\n        uow.commit()\n"),
    "audited, no emission, no commit": audited_case("    def execute(self, uow): ...\n"),
    "emits after commit": audited_case(
        f"    def execute(self, uow):\n        uow.commit()\n{EMIT}"
    ),
    "wrong action": audited_case(
        "    def execute(self, uow):\n"
        "        uow.audit.record(AuditFact(action='doc.write'))\n        uow.commit()\n"
    ),
    "action not a literal": audited_case(
        "    def execute(self, uow):\n"
        "        uow.audit.record(AuditFact(action=LABEL))\n        uow.commit()\n"
    ),
    "action missing": audited_case(
        "    def execute(self, uow):\n"
        "        uow.audit.record(AuditFact(tenant_id=t))\n        uow.commit()\n"
    ),
    "fact built elsewhere": audited_case(
        "    def execute(self, uow):\n        uow.audit.record(fact)\n        uow.commit()\n"
    ),
    "fact via star kwargs": audited_case(
        "    def execute(self, uow):\n"
        "        uow.audit.record(AuditFact(action='doc.read', **extra))\n        uow.commit()\n"
    ),
    "record with keyword": audited_case(
        "    def execute(self, uow):\n"
        "        uow.audit.record(fact=AuditFact(action='doc.read'))\n        uow.commit()\n"
    ),
    "emit in one method commit in another": audited_case(
        f"    def execute(self, uow):\n        self._emit(uow)\n        uow.commit()\n\n"
        f"    def _emit(self, uow):\n{EMIT}"
    ),
    "not_audited emits": exempt_case(f"    def execute(self, uow):\n{EMIT}        uow.commit()\n"),
}


@pytest.mark.parametrize("name", list(INVALID))
def test_non_compliant_use_cases_are_rejected(make_repo: MakeRepo, name: str) -> None:
    assert ag056({UC: INVALID[name]}, make_repo), name


def test_second_emission_with_another_action_is_rejected(make_repo: MakeRepo) -> None:
    body = (
        "    def execute(self, uow):\n"
        "        uow.audit.record(AuditFact(action='doc.read'))\n"
        "        uow.audit.record(AuditFact(action='doc.other'))\n"
        "        uow.commit()\n"
    )
    assert ag056({UC: audited_case(body)}, make_repo)


def test_queries_directory_is_checked_too(make_repo: MakeRepo) -> None:
    body = "    def execute(self, uow):\n        uow.commit()\n"
    assert ag056({"application/queries/x.py": audited_case(body)}, make_repo)


@pytest.mark.parametrize("rel", ["domain/x.py", "infrastructure/x.py", "application/x.py"])
def test_files_outside_use_cases_are_not_checked(make_repo: MakeRepo, rel: str) -> None:
    code = "class Sink:\n    def go(self, uow):\n        uow.audit.record(anything)\n"
    assert ag056({rel: code}, make_repo) == []


def test_malformed_declaration_is_reported_by_ag053_not_ag056(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file("a", UC, audited_case("    def execute(self, uow): ...\n", "Bad Label"))
    rules = repo.violations()
    assert "AG053" in rules and "AG056" not in rules


def test_ag056_is_waivable_like_other_design_rules() -> None:
    from archguard import policy

    assert "AG056" not in policy.NON_WAIVABLE
