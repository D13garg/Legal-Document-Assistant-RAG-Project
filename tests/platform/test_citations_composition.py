"""Citations wiring against real Postgres: the migration branch, the production repository built by
bootstrap, and the recorder writing through it (ADR-016)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text

from lda.bootstrap.citations import (
    CITATIONS_MIGRATIONS,
    build_evidence,
    build_generation_records,
)
from lda.bootstrap.container import PlatformContainer
from lda.modules.chunking.api import ReadChunks
from lda.modules.citations.api import GenerationOutcome, RecordGenerationCommand
from lda.modules.citations.config import CitationsSettings
from lda.modules.citations.testing import RecordingVerifier
from lda.modules.citations.tests.support import Scenario
from lda.platform.database import migrate
from lda.platform.testing.doubles import FakeClock, SequentialIdGenerator
from lda.shared.kernel.ids import RequestId
from platform_support import Database, make_wiring, provisioned_database

ROLE = "lda_app_citations_boot"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def database() -> Iterator[Database]:
    with provisioned_database("lda_test_citations_boot", ROLE, [CITATIONS_MIGRATIONS]) as db:
        yield db


def test_the_bootstrap_builds_the_postgres_repository_and_the_recorder_writes_through_it(
    database: Database,
) -> None:
    engine = create_engine(database.app_url)
    try:
        wiring = make_wiring(
            engine,
            FakeClock(NOW),
            SequentialIdGenerator(),
        )
        platform = cast(PlatformContainer, type("P", (), {"units_of_work": wiring.factory})())
        records = build_generation_records(platform)
        scenario = Scenario()
        container = build_evidence(
            CitationsSettings(),
            cast(ReadChunks, None),  # the builder is not exercised here; the recorder is
            RecordingVerifier("t"),
            records,
            FakeClock(NOW),
            SequentialIdGenerator(),
        )
        evidence = scenario.evidence()
        done = container.generation_recorder.record(
            scenario.ctx,
            RecordGenerationCommand(
                request_id=RequestId(uuid4()),
                evidence=evidence,
                citations=evidence.resolve(["S1"], max_markers=container.max_markers),
                outcome=GenerationOutcome.ANSWERED,
                config_id="rag-" + "a" * 64,
                template_id="grounded_answer",
                template_version="v1",
                model_id="fake-model:1",
                request_sha256="b" * 64,
            ),
        )
        stored = records.get(scenario.ctx.tenant_id, done.record_id)
        assert stored is not None and stored.evidence_fingerprint == evidence.fingerprint()
    finally:
        engine.dispose()


def test_the_citations_branch_downgrades_and_upgrades_cleanly(database: Database) -> None:
    migrate.downgrade(
        database.admin_url,
        target="citations@base",
        app_role=ROLE,
        extra_locations=[CITATIONS_MIGRATIONS],
    )
    admin: Engine = create_engine(database.admin_url)
    with admin.connect() as conn:
        assert (
            conn.execute(text("SELECT to_regclass('citations.generation_records')")).scalar_one()
            is None
        )
    migrate.upgrade(database.admin_url, app_role=ROLE, extra_locations=[CITATIONS_MIGRATIONS])
    with admin.connect() as conn:
        assert (
            conn.execute(text("SELECT to_regclass('citations.generation_records')")).scalar_one()
            is not None
        )
    admin.dispose()
