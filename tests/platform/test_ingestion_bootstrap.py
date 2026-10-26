"""build_ingestion fails fast and closed (ADR-010 §5, §11; ADR-011)."""

from __future__ import annotations

import base64
from collections.abc import Iterator

import pytest

from lda.bootstrap.audit import AUDIT_MIGRATIONS
from lda.bootstrap.container import PlatformContainer, build_platform
from lda.bootstrap.ingestion import (
    INGESTION_MIGRATIONS,
    build_ingestion,
    load_ingestion_settings,
)
from lda.modules.audit.infrastructure.persistence.sql_recorder import SqlAuditRecorder
from lda.modules.ingestion.testing import ScriptedScanner, ScriptedStage
from lda.platform.configuration.loader import ConfigurationLoader
from lda.shared.kernel.errors import ConfigurationError
from platform_support import Database, provisioned_database

_ = base64


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database(
        "lda_test_ingestion_boot",
        "lda_app_ingestion_boot",
        [AUDIT_MIGRATIONS, INGESTION_MIGRATIONS],
    ) as db:
        yield db


def platform_for(
    database: Database, queues: str, **extra: str
) -> tuple[PlatformContainer, ConfigurationLoader]:
    env = {
        "LDA_DATABASE__URL": database.app_url,
        "LDA_OBJECT_STORAGE__BUCKET": "bucket",
        "LDA_JOBS__QUEUES": queues,
        **extra,
    }
    config = ConfigurationLoader(env)
    return build_platform(config, audit_factory=SqlAuditRecorder), config


GOOD = '{"events": 1, "documents": 1, "ingestion": 1}'


def test_a_valid_composition_exposes_handlers_for_both_job_types(database: Database) -> None:
    platform, config = platform_for(database, GOOD)
    try:
        container = build_ingestion(
            platform,
            load_ingestion_settings(config),
            scanner=ScriptedScanner(),
            stages=[ScriptedStage("fake_one")],
        )
        assert set(container.job_handlers) == {"ingestion:run", "ingestion:watchdog"}
    finally:
        platform.dispose()


def test_an_empty_pipeline_is_rejected(database: Database) -> None:
    platform, config = platform_for(database, GOOD)
    try:
        with pytest.raises(ConfigurationError, match="ingestion_pipeline_empty"):
            build_ingestion(
                platform, load_ingestion_settings(config), scanner=ScriptedScanner(), stages=[]
            )
    finally:
        platform.dispose()


def test_a_missing_ingestion_queue_is_rejected(database: Database) -> None:
    platform, config = platform_for(database, '{"events": 1, "documents": 1}')
    try:
        with pytest.raises(ConfigurationError, match="ingestion_queue_not_configured"):
            build_ingestion(
                platform,
                load_ingestion_settings(config),
                scanner=ScriptedScanner(),
                stages=[ScriptedStage("fake_one")],
            )
    finally:
        platform.dispose()


@pytest.mark.parametrize(
    ("extra", "ok"),
    [
        # default lease 60 s + backoff cap 900 s = 960 s
        ({"LDA_INGESTION__WATCHDOG_DELAY_SECONDS": "960"}, False),
        ({"LDA_INGESTION__WATCHDOG_DELAY_SECONDS": "961"}, True),
        ({"LDA_INGESTION__WATCHDOG_DELAY_SECONDS": "300"}, False),
        (
            {
                "LDA_INGESTION__WATCHDOG_DELAY_SECONDS": "100",
                "LDA_JOBS__LEASE_SECONDS": "30",
                "LDA_JOBS__BACKOFF_CAP_SECONDS": "60",
            },
            True,
        ),
    ],
)
def test_the_watchdog_must_outlast_a_lease_plus_a_backoff(
    database: Database, extra: dict[str, str], ok: bool
) -> None:
    platform, config = platform_for(database, GOOD, **extra)
    try:
        args = {
            "scanner": ScriptedScanner(),
            "stages": [ScriptedStage("fake_one")],
        }
        if ok:
            build_ingestion(platform, load_ingestion_settings(config), **args)  # type: ignore[arg-type]
        else:
            with pytest.raises(ConfigurationError, match="ingestion_watchdog_delay_too_short"):
                build_ingestion(platform, load_ingestion_settings(config), **args)  # type: ignore[arg-type]
    finally:
        platform.dispose()
