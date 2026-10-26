"""An ingestion job that is claimed AFTER its document was purged converges (ADR-010 section 10).

ADR-010 documents the limitation: such a job can recreate a run for a version that no longer
exists, and Documents' outcome handler then finds no version and calls `PurgeIngestionRecords`
again. Until Step 18 that convergence was argued in text and exercised only for a forged
other-tenant job. This pins the same-tenant, post-purge case on real PostgreSQL.
"""

from __future__ import annotations

from datetime import timedelta

import test_ingestion_end_to_end as base
from sqlalchemy import Engine

from lda.modules.documents.api import DocumentId, DocumentReference

database = base.database
oracle = base.oracle
stack = base.stack


def test_a_run_job_claimed_after_the_purge_leaves_no_state_and_no_false_outcome(
    stack: base.Stack, oracle: Engine
) -> None:
    document_id, _ = stack.upload()
    stack.worker.run_once()  # events queue: the start-pipeline job runs; ingestion:run is queued
    assert (
        base.scalar(oracle, "SELECT count(*) FROM platform.jobs WHERE job_type = 'ingestion:run'")
        == 1
    )
    stack.docs.delete_document.execute(stack.ctx(), DocumentReference(DocumentId(document_id)))

    # Only the documents queue is served: the purge runs, the ingestion run job is still queued.
    stack.worker = stack.make_worker("purge-only", {"documents": 1})
    base.drain(stack, oracle)
    assert base.scalar(oracle, "SELECT count(*) FROM documents.documents") == 0
    assert base.scalar(oracle, "SELECT count(*) FROM documents.versions") == 0
    assert (
        base.scalar(
            oracle,
            "SELECT count(*) FROM platform.jobs WHERE job_type = 'ingestion:run' AND status = 'queued'",
        )
        == 1
    )
    receipts_before = base.scalar(oracle, "SELECT count(*) FROM documents.purge_receipts")
    assert receipts_before == 1

    # Now the straggler is claimed after the purge.
    stack.worker = stack.make_worker("all")
    base.drain(stack, oracle)

    assert base.scalar(oracle, "SELECT count(*) FROM ingestion.runs") == 0
    assert base.scalar(oracle, "SELECT count(*) FROM ingestion.stage_runs") == 0
    assert base.scalar(oracle, "SELECT count(*) FROM documents.versions") == 0  # nothing revived
    assert base.scalar(oracle, "SELECT count(*) FROM documents.documents") == 0
    assert base.scalar(oracle, "SELECT count(*) FROM documents.purge_receipts") == receipts_before
    assert "documents.version_outcome_recorded" not in base.audit_actions(oracle)
    assert base.job_statuses(oracle).get("dead", 0) == 0  # converged, did not dead-letter
    # The only job left is the recreated run's delayed watchdog (not yet due on the test clock).
    pending = base.fetch(oracle, "SELECT job_type FROM platform.jobs WHERE status = 'queued'")
    assert pending == [("ingestion:watchdog",)]
    stack.clock.advance(timedelta(days=2))  # past any watchdog delay
    base.drain(stack, oracle)
    assert base.job_statuses(oracle).get("queued", 0) == 0  # the watchdog found nothing to do
    assert base.job_statuses(oracle).get("running", 0) == 0
    assert base.scalar(oracle, "SELECT count(*) FROM ingestion.runs") == 0  # not recreated
