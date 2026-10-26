"""The real repository must satisfy every architecture rule (no baseline of tolerated violations)."""

from pathlib import Path

from archguard import runner

ROOT = Path(__file__).resolve().parents[2]


def test_repository_has_no_architecture_violations() -> None:
    assert [v.render() for v in runner.run_all(ROOT)] == []
