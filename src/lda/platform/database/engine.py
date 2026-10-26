from __future__ import annotations

from sqlalchemy import Engine, create_engine

from lda.platform.configuration.settings import DatabaseSettings


def create_runtime_engine(settings: DatabaseSettings) -> Engine:
    """Engine for the *runtime* role. That role must not own tables nor have BYPASSRLS."""
    return create_engine(
        settings.url.get_secret_value(),
        pool_size=settings.pool_size,
        pool_timeout=settings.pool_timeout_seconds,
        pool_pre_ping=True,
        connect_args={"options": f"-c statement_timeout={settings.statement_timeout_ms}"},
    )
