"""Names and builders for ingestion jobs (ADR-010 §2, §11). The payload carries opaque ids and a
generation, never a tenant: the tenant comes from the job row (ADR-012/013)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from lda.modules.ingestion.domain.ids import IngestionRunId
from lda.shared.ports.jobs import JobSpec

INGESTION_QUEUE = "ingestion"
RUN_JOB = "ingestion:run"
WATCHDOG_JOB = "ingestion:watchdog"


def run_unique_key(version_id: UUID, generation: int) -> str:
    return f"{RUN_JOB}:{version_id}:{generation}"


def ingestion_job_spec(document_id: UUID, version_id: UUID, generation: int = 1) -> JobSpec:
    """The job that starts (generation 1) or replays (generation n) ingestion of one version.

    Exactly one job exists per (version, generation): the unique key makes duplicate delivery of
    the triggering event a no-op."""
    if generation < 1:
        raise ValueError("generation must be at least 1")
    return JobSpec(
        queue=INGESTION_QUEUE,
        job_type=RUN_JOB,
        payload={
            "document_id": str(document_id),
            "version_id": str(version_id),
            "generation": generation,
        },
        unique_key=run_unique_key(version_id, generation),
    )


def watchdog_job_spec(run_id: IngestionRunId, run_at: datetime) -> JobSpec:
    return JobSpec(
        queue=INGESTION_QUEUE,
        job_type=WATCHDOG_JOB,
        payload={"run_id": str(run_id)},
        unique_key=f"{WATCHDOG_JOB}:{run_id}",
        run_at=run_at,
    )
