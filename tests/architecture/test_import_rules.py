from __future__ import annotations

import pytest

from repo_builder import MakeRepo

# (rule, file inside module a, content)
CASES = [
    ("AG020", "domain/x.py", "from lda.modules.a.infrastructure import y\n"),
    ("AG020", "domain/x.py", "from lda.shared.ports import y\n"),
    ("AG020", "application/x.py", "from lda.platform import y\n"),
    ("AG020", "presentation/x.py", "from lda.modules.a.domain import y\n"),
    ("AG020", "domain/x.py", "from lda.bootstrap import y\n"),
    ("AG030", "domain/x.py", "import requests\n"),
    ("AG030", "domain/x.py", "import pydantic\n"),
    ("AG030", "application/x.py", "import sqlalchemy\n"),
    ("AG030", "application/x.py", "from fastapi import APIRouter\n"),
    ("AG031", "infrastructure/adapters/x.py", "import sqlalchemy\n"),
    ("AG032", "infrastructure/adapters/x.py", "import openai\n"),
    ("AG033", "infrastructure/x.py", "import celery\n"),
    ("AG033", "infrastructure/x.py", "import boto3\n"),
    ("AG033", "infrastructure/x.py", "import redis\n"),
    ("AG033", "infrastructure/x.py", "from langchain_core import x\n"),
    ("AG040", "infrastructure/x.py", "import os\nx = os.getenv('A')\n"),
    ("AG040", "infrastructure/x.py", "import os as o\nx = o.environ['A']\n"),
    ("AG040", "infrastructure/x.py", "from os import environ\n"),
    ("AG040", "infrastructure/x.py", "from pydantic_settings import BaseSettings\n"),
    ("AG090", "application/x.py", "x = AuthorizedContext(items=())\n"),
    ("AG090", "application/x.py", "class Fake(AuthorizationProof): ...\n"),
    ("AG090", "application/x.py", "x = Authorized[int](1)\n"),
    ("AG090", "application/x.py", "x = AccessScope(tenant_id=t)\n"),
    ("AG090", "application/x.py", "class Fake(AccessScope): ...\n"),
    ("AG090", "domain/x.py", "x = AuthorizationProof(token='t')\n"),
]

OK_CASES = [
    (
        "domain/x.py",
        "import dataclasses\nfrom lda.shared.kernel import y\nfrom lda.modules.a.domain import z\n",
    ),
    (
        "application/x.py",
        "import pydantic\nfrom lda.shared.ports import p\nfrom lda.modules.a.domain import z\n",
    ),
    ("infrastructure/persistence/x.py", "import sqlalchemy\nfrom lda.platform.database import s\n"),
    ("infrastructure/x.py", "import httpx\n"),
    (
        "tests/unit/test_x.py",
        "import sqlalchemy, os\nfrom lda.modules.a.domain import z\nx = os.environ\n",
    ),
    ("config.py", "import pydantic\nfrom lda.shared.kernel import y\n"),
]


@pytest.mark.parametrize(("rule", "rel", "content"), CASES)
def test_violation_detected(make_repo: MakeRepo, rule: str, rel: str, content: str) -> None:
    repo = make_repo()
    repo.module_file("a", rel, content)
    assert rule in repo.violations()


@pytest.mark.parametrize(("rel", "content"), OK_CASES)
def test_compliant_code_passes(make_repo: MakeRepo, rel: str, content: str) -> None:
    repo = make_repo()
    repo.module_file("a", rel, content)
    assert repo.violations() == []


def test_cross_module_deep_import_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.set_deps("b", ["a"])
    repo.module_file("b", "application/x.py", "from lda.modules.a.domain.entities import E\n")
    assert "AG010" in repo.violations()


def test_cross_module_package_root_import_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.set_deps("b", ["a"])
    repo.module_file("b", "application/x.py", "from lda.modules.a import Thing\n")
    assert "AG010" in repo.violations()


def test_cross_module_api_import_with_declared_dependency_passes(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.set_deps("b", ["a"])
    repo.module_file(
        "b",
        "application/x.py",
        "from lda.modules.a.api import Thing\nfrom lda.modules.a import api\n",
    )
    assert repo.violations() == []


def test_testing_surface_only_from_tests(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.set_deps("b", ["a"])
    repo.module_file("b", "application/x.py", "from lda.modules.a.testing import Fake\n")
    assert "AG020" in repo.violations()
    repo.module_file("b", "application/x.py", "")
    repo.module_file("b", "tests/unit/test_y.py", "from lda.modules.a.testing import Fake\n")
    assert repo.violations() == []


def test_domain_cannot_import_other_modules(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.set_deps("b", ["a"])
    repo.module_file("b", "domain/x.py", "from lda.modules.a.api import Thing\n")
    assert "AG020" in repo.violations()


def test_undeclared_dependency_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file("a", "application/x.py", "from lda.modules.c.api import Thing\n")
    assert "AG011" in repo.violations()


def test_unused_declared_dependency_rejected(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.set_deps("b", ["a"])
    assert "AG012" in repo.violations()


def test_relative_imports_are_resolved(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file("a", "domain/x.py", "from ..infrastructure import y\n")
    assert "AG020" in repo.violations()


def test_kernel_ports_platform_isolation(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.write("src/lda/shared/kernel/ids.py", "from lda.modules.a.api import x\n")
    repo.write("src/lda/shared/ports/p.py", "from lda.platform import x\n")
    repo.write("src/lda/platform/p.py", "from lda.modules.a.api import x\n")
    repo.write("src/lda/shared/kernel/e.py", "import attrs\n")
    found = repo.violations()
    assert found.count("AG020") == 3 and "AG030" in found


def test_configuration_package_may_read_environment(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.write(
        "src/lda/platform/configuration/loader.py",
        "import os\nx = os.environ\nfrom pydantic_settings import BaseSettings\n",
    )
    assert repo.violations() == []


def test_authorization_types_creatable_in_access_control(make_repo: MakeRepo) -> None:
    repo = make_repo(dag="[modules.access_control]\ndepends_on=[]\n", modules=("access_control",))
    repo.module_file("access_control", "application/x.py", "x = AuthorizedContext(items=())\n")
    assert repo.violations() == []
