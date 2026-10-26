from __future__ import annotations

from sqlalchemy import create_engine, text

from lda.platform.database import migrate
from platform_support import APP_ROLE, Database


def test_platform_branch_downgrades_and_upgrades_cleanly(database: Database) -> None:
    migrate.downgrade(database.admin_url, target="platform@base", app_role=APP_ROLE)
    engine = create_engine(database.admin_url)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('platform.jobs')")).scalar_one() is None
    migrate.upgrade(database.admin_url, app_role=APP_ROLE)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT to_regclass('platform.jobs')")).scalar_one() is not None
    engine.dispose()
