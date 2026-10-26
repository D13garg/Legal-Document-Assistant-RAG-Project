from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.modules.evaluation.infrastructure.persistence.locations import MIGRATIONS_VERSIONS
from lda.modules.evaluation.infrastructure.persistence.store import SqlEvaluationStore
from platform_support import Database, provisioned_database


@pytest.fixture(scope="session")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_evaluation", "lda_app_evaluation", [MIGRATIONS_VERSIONS]
    ) as db:
        yield db


@pytest.fixture
def admin_engine(database: Database) -> Iterator[Engine]:
    """Owner/superuser oracle. Cleanup uses replica mode (superuser-only) to bypass the
    insert-only triggers; the runtime role can never do this."""
    engine = create_engine(database.admin_url)
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(text("DELETE FROM evaluation.release_eligibility"))
        conn.execute(text("DELETE FROM evaluation.evaluation_runs"))
    yield engine
    engine.dispose()


@pytest.fixture
def app_engine(database: Database, admin_engine: Engine) -> Iterator[Engine]:
    engine = create_engine(database.app_url, pool_size=20)
    yield engine
    engine.dispose()


@pytest.fixture
def store(app_engine: Engine) -> SqlEvaluationStore:
    return SqlEvaluationStore(app_engine)
