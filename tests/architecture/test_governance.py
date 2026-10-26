from __future__ import annotations

import datetime as dt

from archguard import adr, runner
from repo_builder import MakeRepo, Repo

GOOD_ADR = """# ADR-001: Title

## Status
Accepted

""" + "\n".join(
    f"## {h}\nx\n"
    for h in [
        "Context",
        "Problem",
        "Decision",
        "Rationale",
        "Alternatives considered",
        "Consequences",
        "Security implications",
        "Operational implications",
        "Migration implications",
    ]
)

LOG = "| ADR | Title | Status | Step |\n|---|---|---|---|\n| [ADR-001](ADR/ADR-001-t.md) | T | Accepted | 0 |\n"


def with_adr(repo: Repo, adr_text: str = GOOD_ADR, log: str = LOG) -> None:
    repo.write("docs/architecture/ADR/ADR-001-t.md", adr_text)
    repo.write("docs/architecture/decisions-log.md", log)


def test_valid_adr_and_log_pass(make_repo: MakeRepo) -> None:
    repo = make_repo()
    with_adr(repo)
    assert repo.violations() == []


def test_adr_missing_section_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    with_adr(repo, GOOD_ADR.replace("## Rationale\nx\n", ""))
    assert "AG070" in repo.violations()


def test_adr_invalid_status_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    with_adr(repo, GOOD_ADR.replace("Accepted", "Maybe", 1))
    assert "AG070" in repo.violations()


def test_unindexed_adr_and_status_mismatch_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    with_adr(repo, log="| ADR | T | S | X |\n|---|---|---|---|\n")
    assert "AG071" in repo.violations()
    with_adr(repo, log=LOG.replace("Accepted", "Proposed"))
    assert "AG071" in repo.violations()


def test_log_row_without_file_rejected_unless_planned(make_repo: MakeRepo) -> None:
    repo = make_repo()
    with_adr(repo, log=LOG + "| ADR-002 | X | Accepted | 1 |\n")
    assert "AG071" in repo.violations()
    with_adr(repo, log=LOG + "| ADR-002 | X | Planned | 1 |\n")
    assert repo.violations() == []


def test_adr_gate() -> None:
    governed = "src/lda/modules/a/module.toml"
    assert [v.rule for v in adr.adr_gate([governed])] == ["AG072"]
    assert [v.rule for v in adr.adr_gate([governed, "docs/architecture/ADR/ADR-003-x.md"])] == [
        "AG072"
    ]
    assert (
        adr.adr_gate(
            [governed, "docs/architecture/ADR/ADR-003-x.md", "docs/architecture/decisions-log.md"]
        )
        == []
    )
    assert adr.adr_gate(["src/lda/modules/a/domain/x.py", "README.md"]) == []
    assert adr.adr_gate(["tools/archguard/policy.py"])[0].rule == "AG072"


def waiver(
    rule: str = "AG050",
    path: str = "src/lda/modules/a/domain/x.py",
    symbol: str = "XManager",
    expires: str = "2999-01-01",
    adr_id: str = "ADR-001",
) -> str:
    return (
        f'[[exception]]\nrule="{rule}"\npath="{path}"\nsymbol="{symbol}"\nowner="t"\nadr="{adr_id}"\n'
        f'expires={expires}\nreason="r"\n'
    )


def violating(repo: Repo) -> None:
    repo.module_file("a", "domain/x.py", "class XManager: ...\n")


def test_valid_exception_suppresses_violation(make_repo: MakeRepo) -> None:
    repo = make_repo()
    with_adr(repo)
    violating(repo)
    repo.write("architecture-exceptions.toml", waiver())
    assert repo.violations() == []


def test_expired_exception_fails(make_repo: MakeRepo) -> None:
    repo = make_repo()
    with_adr(repo)
    violating(repo)
    repo.write("architecture-exceptions.toml", waiver(expires="2020-01-01"))
    found = repo.violations()
    assert "AG060" in found and "AG050" in found


def test_stale_exception_fails(make_repo: MakeRepo) -> None:
    repo = make_repo()
    with_adr(repo)
    repo.write("architecture-exceptions.toml", waiver())
    assert "AG061" in repo.violations()


def test_exception_needs_existing_adr_and_waivable_rule(make_repo: MakeRepo) -> None:
    repo = make_repo()
    with_adr(repo)
    repo.write("architecture-exceptions.toml", waiver(adr_id="ADR-999") + waiver(rule="AG090"))
    assert repo.violations().count("AG060") == 2


def test_expiry_uses_injected_clock(make_repo: MakeRepo) -> None:
    repo = make_repo()
    with_adr(repo)
    violating(repo)
    repo.write("architecture-exceptions.toml", waiver(expires="2030-01-01"))
    assert [v.rule for v in runner.run_all(repo.root, today=dt.date(2031, 1, 1))].count(
        "AG060"
    ) == 1
