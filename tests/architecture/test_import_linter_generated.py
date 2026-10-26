from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from repo_builder import MakeRepo, Repo

LINT = shutil.which("lint-imports", path=str(Path(sys.executable).parent))
pytestmark = pytest.mark.skipif(LINT is None, reason="import-linter not installed")


def run(repo: Repo) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(repo.root / "src")}
    return subprocess.run(
        [str(LINT)], cwd=repo.root, env=env, capture_output=True, text=True, check=False
    )


def test_generated_contracts_pass_on_clean_scaffold(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.set_deps("b", ["a"])
    repo.module_file("b", "application/x.py", "from lda.modules.a.api import thing\n")
    repo.regen()
    result = run(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_contracts_catch_layer_violation(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file("a", "domain/x.py", "from lda.modules.a.infrastructure import y\n")
    repo.module_file("a", "infrastructure/y.py", "")
    assert run(repo).returncode != 0


def test_generated_contracts_catch_dag_order_violation(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file("a", "application/x.py", "from lda.modules.b.api import thing\n")  # a below b
    repo.regen()
    assert run(repo).returncode != 0


def _with_platform(repo: Repo) -> None:
    for rel in ("testing", "database"):
        repo.write(f"src/lda/platform/{rel}/__init__.py")
    repo.write("src/lda/platform/testing/doubles.py")
    repo.write("src/lda/platform/database/engine.py")


def test_generated_text_has_no_ignore_imports(make_repo: MakeRepo) -> None:
    repo = make_repo()
    _with_platform(repo)
    assert "ignore_imports" not in repo.regen_text()


def test_testing_code_may_use_platform_testing(make_repo: MakeRepo) -> None:
    repo = make_repo()
    _with_platform(repo)
    repo.module_file("a", "testing/fakes.py", "from lda.platform.testing import doubles\n")
    repo.regen()
    result = run(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_testing_code_may_not_use_the_rest_of_the_platform(make_repo: MakeRepo) -> None:
    repo = make_repo()
    _with_platform(repo)
    repo.module_file("a", "testing/fakes.py", "from lda.platform.database import engine\n")
    repo.regen()
    assert run(repo).returncode != 0


def test_a_new_platform_part_is_covered_after_regeneration(make_repo: MakeRepo) -> None:
    repo = make_repo()
    _with_platform(repo)
    repo.write("src/lda/platform/storage/__init__.py")
    repo.write("src/lda/platform/storage/client.py")
    repo.module_file("a", "testing/fakes.py", "from lda.platform.storage import client\n")
    repo.regen()
    assert run(repo).returncode != 0


@pytest.mark.parametrize("layer", ["domain", "application", "api"])
def test_other_layers_may_not_use_platform_testing(make_repo: MakeRepo, layer: str) -> None:
    repo = make_repo()
    _with_platform(repo)
    repo.module_file("a", f"{layer}/x.py", "from lda.platform.testing import doubles\n")
    repo.regen()
    assert run(repo).returncode != 0


@pytest.mark.parametrize("layer", ["domain", "application", "api"])
def test_other_layers_may_not_use_the_platform(make_repo: MakeRepo, layer: str) -> None:
    repo = make_repo()
    _with_platform(repo)
    repo.module_file("a", f"{layer}/x.py", "from lda.platform.database import engine\n")
    repo.regen()
    assert run(repo).returncode != 0
