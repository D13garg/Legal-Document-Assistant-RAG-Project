from __future__ import annotations

from archguard import apilock
from repo_builder import MakeRepo, Repo

API = """from lda.modules.a.domain.thing import Thing, make

__all__ = ["Thing", "make"]
"""
THING = """from dataclasses import dataclass

@dataclass(frozen=True)
class Thing:
    value: int

    def double(self) -> int:
        return self.value * 2

    def _private(self) -> None: ...

def make(v: int) -> Thing:
    return Thing(v)
"""


def publish(repo: Repo) -> None:
    repo.module_file("a", "domain/thing.py", THING)
    repo.module_file("a", "api/__init__.py", API)


def test_api_change_requires_lock_update(make_repo: MakeRepo) -> None:
    repo = make_repo()
    publish(repo)
    assert "AG080" in repo.violations()
    repo.regen()
    assert repo.violations() == []


def test_signature_change_behind_the_api_is_detected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    publish(repo)
    repo.regen()
    repo.module_file("a", "domain/thing.py", THING.replace("def make(v: int)", "def make(v: str)"))
    assert "AG080" in repo.violations()


def test_private_changes_are_not_api_changes(make_repo: MakeRepo) -> None:
    repo = make_repo()
    publish(repo)
    repo.regen()
    repo.module_file(
        "a",
        "domain/thing.py",
        THING.replace("def _private(self) -> None: ...", "def _private(self) -> int: ..."),
    )
    assert repo.violations() == []


def test_all_must_be_literal_and_resolvable(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file("a", "api/__init__.py", "__all__ = compute()\n")
    assert "AG080" in repo.violations()
    repo.module_file("a", "api/__init__.py", '__all__ = ["Ghost"]\n')
    repo.regen()
    assert "AG080" in repo.violations()


def test_snapshot_lists_resolved_signatures(make_repo: MakeRepo) -> None:
    repo = make_repo()
    publish(repo)
    text, err = apilock.build_snapshot(repo.root, "a")
    assert (
        err is None
        and "def make(v: int) -> Thing: ..." in text
        and "def double(self) -> int: ..." in text
    )
