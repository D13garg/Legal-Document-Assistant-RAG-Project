from pathlib import Path

import pytest

from repo_builder import MakeRepo, build_factory


@pytest.fixture
def make_repo(tmp_path: Path) -> MakeRepo:
    return build_factory(tmp_path)
