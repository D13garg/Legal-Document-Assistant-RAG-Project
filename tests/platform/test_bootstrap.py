from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from lda.bootstrap.container import build_platform
from lda.platform.configuration.loader import ConfigurationLoader
from lda.platform.testing.doubles import InMemoryAuditRecorder
from lda.shared.kernel.context import SystemContext
from lda.shared.ports.jobs import JobSpec
from platform_support import Database, tenant


def test_container_wires_a_working_tenant_unit_of_work(
    database: Database, admin_engine: object
) -> None:
    config = ConfigurationLoader(
        {
            "LDA_DATABASE__URL": database.app_url,
            "LDA_OBJECT_STORAGE__BUCKET": "bucket",
            "LDA_JOBS__QUEUES": '{"events": 1}',
        }
    )
    container = build_platform(config, audit_factory=lambda tx: InMemoryAuditRecorder())
    try:
        with container.units_of_work.begin(SystemContext(tenant(1), "test")) as uow:
            assert uow.jobs.enqueue(JobSpec(queue="events", job_type="x")) is not None
            uow.commit()
        assert [m.count for m in container.job_maintenance.metrics()] == [1]
        assert container.clock.now() > datetime(2026, 1, 1, tzinfo=UTC)
        assert isinstance(container.ids.new_uuid(), UUID)
    finally:
        container.dispose()
