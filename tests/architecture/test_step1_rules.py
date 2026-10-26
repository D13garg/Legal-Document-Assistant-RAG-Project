from __future__ import annotations

import pytest

from repo_builder import MakeRepo

SCHEMA_CASES = [
    ("schema='other'", 'Table("t", md, schema="other")\n'),
    ("fk", 'ForeignKey("other.things.id")\n'),
    ("sql read", 'text("SELECT * FROM platform.jobs")\n'),
    ("sql join", 'text("SELECT 1 FROM a.x JOIN other.y ON true")\n'),
]


@pytest.mark.parametrize(("_", "content"), SCHEMA_CASES)
def test_cross_schema_access_in_persistence_is_rejected(
    make_repo: MakeRepo, _: str, content: str
) -> None:
    repo = make_repo()
    repo.module_file("a", "infrastructure/persistence/models.py", content)
    assert "AG034" in repo.violations()


def test_own_schema_and_aliases_are_fine(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file(
        "a",
        "infrastructure/persistence/models.py",
        'Table("t", md, schema="a")\nForeignKey("a.things.id")\ntext("SELECT x.id FROM a.t x JOIN a.u y ON x.id = y.id")\n',
    )
    assert repo.violations() == []


def test_schema_rule_applies_only_to_persistence(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file("a", "application/x.py", 'MSG = "see platform.jobs"\n')
    assert "AG034" not in repo.violations()


def test_repository_instantiation_only_in_unit_of_work(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.module_file("a", "infrastructure/adapters.py", "r = DocumentRepository(session)\n")
    assert "AG055" in repo.violations()
    repo.module_file("a", "infrastructure/adapters.py", "")
    repo.module_file("a", "infrastructure/unit_of_work.py", "r = DocumentRepository(session)\n")
    assert repo.violations() == []


def test_platform_testing_doubles_are_test_only(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.write("src/lda/platform/testing/d.py", "")
    repo.module_file("a", "application/x.py", "from lda.platform.testing import d\n")
    assert "AG020" in repo.violations()
    repo.module_file("a", "application/x.py", "")
    repo.write("src/lda/bootstrap/c.py", "from lda.platform.testing import d\n")
    assert "AG020" in repo.violations()
    repo.write("src/lda/bootstrap/c.py", "")
    repo.module_file("a", "tests/unit/test_x.py", "from lda.platform.testing import d\n")
    repo.module_file("a", "testing/__init__.py", "from lda.platform.testing import d\n")
    assert repo.violations() == []


def test_sdk_construction_cannot_live_in_bootstrap(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.write(
        "src/lda/bootstrap/c.py", "import boto3\nimport redis\nfrom sqlalchemy import Engine\n"
    )
    found = repo.violations()
    assert "AG033" in found and "AG031" in found


# ---- platform/testing and zone-boundary imports, in every spelling ---------------------------------

PLATFORM_FILE = "src/lda/platform/clockx.py"
DOUBLES = "src/lda/platform/testing/doubles.py"

TESTING_BYPASS_CASES = [
    ("src/lda/bootstrap/c.py", "from lda.platform import testing\n"),
    ("src/lda/bootstrap/c.py", "from lda.platform import testing as t\n"),
    ("src/lda/bootstrap/c.py", "import lda.platform.testing.doubles as d\n"),
    ("src/lda/bootstrap/c.py", "from lda.platform.testing import doubles\n"),
    ("src/lda/bootstrap/c.py", "from lda.platform.testing.doubles import FakeClock\n"),
    (PLATFORM_FILE, "from lda.platform import testing\n"),
    (PLATFORM_FILE, "from lda.platform.testing.doubles import FakeClock\n"),
    ("src/lda/platform/events/p.py", "from lda.platform import testing\n"),
    ("src/lda/shared/kernel/k.py", "from lda.platform.testing.doubles import FakeClock\n"),
    ("src/lda/shared/ports/p.py", "from lda.platform import testing\n"),
]


@pytest.mark.parametrize(("path", "content"), TESTING_BYPASS_CASES)
def test_platform_testing_is_unreachable_from_production_code(
    make_repo: MakeRepo, path: str, content: str
) -> None:
    repo = make_repo()
    repo.write(DOUBLES, "")
    repo.write(path, content)
    assert "AG020" in repo.violations()


@pytest.mark.parametrize("zone", ["application", "infrastructure", "domain", "presentation"])
@pytest.mark.parametrize(
    "content", ["from lda.platform import testing\n", "from lda.platform.testing import doubles\n"]
)
def test_module_production_layers_cannot_import_platform_testing(
    make_repo: MakeRepo, zone: str, content: str
) -> None:
    repo = make_repo()
    repo.write(DOUBLES, "")
    repo.module_file("a", f"{zone}/x.py", content)
    assert "AG020" in repo.violations()


def test_platform_testing_remains_importable_where_it_is_meant_to_be(make_repo: MakeRepo) -> None:
    repo = make_repo()
    repo.write(DOUBLES, "")
    repo.write("src/lda/platform/testing/more.py", "from lda.platform.testing import doubles\n")
    repo.module_file("a", "tests/unit/test_x.py", "from lda.platform import testing\n")
    repo.module_file("a", "testing/__init__.py", "from lda.platform import testing\n")
    repo.write("tests/platform/test_x.py", "from lda.platform import testing\n")
    assert repo.violations() == []


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("src/lda/modules/a/domain/x.py", "from lda import platform\n"),
        ("src/lda/modules/a/domain/x.py", "from lda.shared import ports\n"),
        ("src/lda/modules/a/application/x.py", "from lda import bootstrap\n"),
        ("src/lda/modules/a/infrastructure/x.py", "from lda import bootstrap\n"),
        ("src/lda/platform/events/p.py", "from lda import bootstrap\n"),
        ("src/lda/platform/events/p.py", "from lda.bootstrap import container\n"),
        ("src/lda/platform/events/p.py", "from lda.modules.a import api\n"),
        ("src/lda/platform/events/p.py", "from lda.modules.a.api import Thing\n"),
    ],
)
def test_zone_boundaries_hold_for_submodule_style_imports(
    make_repo: MakeRepo, path: str, content: str
) -> None:
    repo = make_repo()
    repo.write(path, content)
    assert {"AG010", "AG020"} & set(repo.violations())


# ---- provider SDKs / env access stay behind their boundaries ---------------------------------------

SDK_ELSEWHERE = [
    ("boto3", "AG033", "ratelimit"),
    ("boto3", "AG033", "database"),
    ("boto3", "AG033", "events"),
    ("boto3", "AG033", "telemetry"),
    ("boto3", "AG033", "configuration"),
    ("botocore.exceptions", "AG033", "events"),
    ("redis", "AG033", "storage"),
    ("redis.exceptions", "AG033", "database"),
    ("redis", "AG033", "events"),
    ("sqlalchemy", "AG031", "storage"),
    ("sqlalchemy.orm", "AG031", "ratelimit"),
    ("sqlalchemy", "AG031", "events"),
    ("sqlalchemy", "AG031", "telemetry"),
    ("alembic", "AG031", "events"),
    ("psycopg", "AG031", "configuration"),
    ("openai", "AG032", "events"),
    ("anthropic", "AG032", "storage"),
    ("celery", "AG033", "database"),
]


@pytest.mark.parametrize(("sdk", "rule", "package"), SDK_ELSEWHERE)
@pytest.mark.parametrize("spelling", ["import {sdk}\n", "from {sdk} import x\n"])
def test_sdks_are_rejected_in_every_other_platform_package(
    make_repo: MakeRepo, sdk: str, rule: str, package: str, spelling: str
) -> None:
    repo = make_repo()
    repo.write(f"src/lda/platform/{package}/x.py", spelling.format(sdk=sdk))
    assert rule in repo.violations()


@pytest.mark.parametrize(
    "sdk", ["boto3", "botocore.config", "redis", "sqlalchemy", "alembic", "psycopg"]
)
def test_sdks_are_rejected_inside_function_bodies_too(make_repo: MakeRepo, sdk: str) -> None:
    repo = make_repo()
    repo.write("src/lda/platform/events/x.py", f"def f():\n    import {sdk}\n")
    assert {"AG031", "AG033"} & set(repo.violations())


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("src/lda/platform/storage/x.py", "import boto3\nfrom botocore.config import Config\n"),
        (
            "src/lda/platform/ratelimit/x.py",
            "import redis\nfrom redis.exceptions import RedisError\n",
        ),
        (
            "src/lda/platform/database/x.py",
            "import sqlalchemy\nfrom alembic import op\nimport psycopg\n",
        ),
        ("src/lda/platform/database/jobs/x.py", "from sqlalchemy import text\n"),
        ("src/lda/platform/configuration/x.py", "import os\nx = os.environ\n"),
    ],
)
def test_sdks_are_allowed_exactly_where_the_policy_says(
    make_repo: MakeRepo, path: str, content: str
) -> None:
    repo = make_repo()
    repo.write(path, content)
    assert repo.violations() == []


@pytest.mark.parametrize(
    ("path", "content"),
    [
        ("src/lda/platform/telemetry/x.py", "import os\nx = os.environ['A']\n"),
        ("src/lda/platform/storage/x.py", "from os import getenv\n"),
        ("src/lda/platform/database/x.py", "import os\nx = os.getenv('A')\n"),
        ("src/lda/bootstrap/c.py", "import os as o\nx = o.environ\n"),
        ("src/lda/shared/kernel/k.py", "import os\nx = os.putenv('A', 'b')\n"),
    ],
)
def test_environment_is_readable_only_through_platform_configuration(
    make_repo: MakeRepo, path: str, content: str
) -> None:
    repo = make_repo()
    repo.write(path, content)
    assert "AG040" in repo.violations()
