from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError as PydanticError

from lda.modules.ingestion.application.jobs import ingestion_job_spec, watchdog_job_spec
from lda.modules.ingestion.application.outcomes import outcome_event
from lda.modules.ingestion.application.pipeline import Pipeline, ScanStage
from lda.modules.ingestion.application.ports import (
    ScanVerdict,
    StageQuarantined,
    StageRequest,
    StageRetryable,
    StageSucceeded,
)
from lda.modules.ingestion.config import IngestionSettings
from lda.modules.ingestion.domain.errors import InvalidTransition
from lda.modules.ingestion.domain.events import (
    IngestionCompleted,
    IngestionFailed,
    IngestionQuarantined,
)
from lda.modules.ingestion.domain.run import IngestionRun, run_id_for
from lda.modules.ingestion.testing import ScriptedScanner, ScriptedStage
from lda.modules.ingestion.tests.support import D1, START, TENANT_A, V1
from lda.shared.kernel.errors import ConfigurationError
from lda.shared.kernel.ids import EventId, JobId


def request() -> StageRequest:
    return StageRequest(
        TENANT_A, D1, V1, run_id_for(TENANT_A, V1, 1), "scan", 1, JobId(UUID(int=1)), 1
    )


# ---- plan --------------------------------------------------------------------------------------


def test_the_plan_always_starts_with_scan_and_keeps_the_configured_order() -> None:
    pipeline = Pipeline(ScriptedScanner(), [ScriptedStage("b_stage"), ScriptedStage("a_stage")])
    assert pipeline.names == ("scan", "b_stage", "a_stage")
    assert pipeline.get("scan") is not None and pipeline.get("a_stage") is not None
    assert pipeline.get("nope") is None


def test_an_empty_plan_is_rejected_at_composition() -> None:
    with pytest.raises(ConfigurationError, match="ingestion_pipeline_empty"):
        Pipeline(ScriptedScanner(), [])


@pytest.mark.parametrize(
    "names",
    [["a", "a"], ["scan"], ["Bad"], ["x", "9y"], [""]],
)
def test_stage_names_must_be_unique_valid_and_not_scan(names: list[str]) -> None:
    with pytest.raises(ConfigurationError, match="ingestion_pipeline_names_invalid"):
        Pipeline(ScriptedScanner(), [ScriptedStage(n) for n in names])


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (ScanVerdict.CLEAN, StageSucceeded()),
        (ScanVerdict.INFECTED, StageQuarantined("malware_detected")),
        (ScanVerdict.ERROR, StageRetryable("scan_error")),
    ],
)
def test_the_scan_stage_maps_each_verdict(verdict: ScanVerdict, expected: object) -> None:
    scanner = ScriptedScanner([verdict])
    assert ScanStage(scanner).run(request()) == expected
    assert len(scanner.requests) == 1
    seen = scanner.requests[0]
    assert (seen.tenant_id, seen.document, seen.version) == (TENANT_A, D1, V1)


def test_an_unknown_verdict_never_lets_the_run_continue() -> None:
    class Odd(ScriptedScanner):
        def scan(self, request: object) -> ScanVerdict:
            return "weird"  # type: ignore[return-value]

    assert ScanStage(Odd()).run(request()) == StageRetryable("scan_error")


def test_the_scan_stage_is_named_scan() -> None:
    assert ScanStage(ScriptedScanner()).name == "scan"


# ---- job specs ---------------------------------------------------------------------------------


def test_the_ingestion_job_carries_two_ids_and_a_generation_but_never_a_tenant() -> None:
    d, v = UUID(int=0xD), UUID(int=0xE)
    spec = ingestion_job_spec(d, v)
    assert (spec.queue, spec.job_type) == ("ingestion", "ingestion:run")
    assert dict(spec.payload) == {"document_id": str(d), "version_id": str(v), "generation": 1}
    assert spec.unique_key == f"ingestion:run:{v}:1"
    assert spec.max_attempts == 5  # the JobSpec default: no new retry budget is invented
    assert "tenant" not in str(spec.payload)


def test_each_generation_has_its_own_unique_key() -> None:
    d, v = UUID(int=0xD), UUID(int=0xE)
    keys = {ingestion_job_spec(d, v, g).unique_key for g in (1, 2, 3)}
    assert keys == {f"ingestion:run:{v}:{g}" for g in (1, 2, 3)}


def test_generation_must_be_positive() -> None:
    with pytest.raises(ValueError, match="generation"):
        ingestion_job_spec(UUID(int=1), UUID(int=2), 0)


def test_the_watchdog_job_is_delayed_and_unique_per_run() -> None:
    run_id = run_id_for(TENANT_A, V1, 1)
    at = START + timedelta(minutes=30)
    spec = watchdog_job_spec(run_id, at)
    assert (spec.queue, spec.job_type, spec.run_at) == ("ingestion", "ingestion:watchdog", at)
    assert dict(spec.payload) == {"run_id": str(run_id)}
    assert spec.unique_key == f"ingestion:watchdog:{run_id}"


# ---- settings ----------------------------------------------------------------------------------


def test_settings_defaults_and_bounds() -> None:
    s = IngestionSettings()
    assert (s.watchdog_delay_seconds, s.max_outcome_redeliveries) == (1800, 5)
    assert IngestionSettings(watchdog_delay_seconds=60).watchdog_delay_seconds == 60
    assert IngestionSettings(max_outcome_redeliveries=0).max_outcome_redeliveries == 0
    for bad in ({"watchdog_delay_seconds": 59}, {"max_outcome_redeliveries": -1}, {"unknown": 1}):
        with pytest.raises(PydanticError):
            IngestionSettings(**bad)


# ---- outcome events ----------------------------------------------------------------------------


def finished(kind: str) -> IngestionRun:
    run = IngestionRun.start(TENANT_A, D1, V1, 1, ("scan", "x"), START)
    run.begin_stage("scan", START)
    if kind == "quarantined":
        run.quarantine_stage("scan", "malware_detected", START)
    elif kind == "failed":
        run.succeed_stage("scan", START)
        run.begin_stage("x", START)
        run.fail_stage("x", "unsupported", START)
    elif kind == "completed":
        run.succeed_stage("scan", START)
        run.begin_stage("x", START)
        run.succeed_stage("x", START)
        run.complete(START)
    return run


def test_each_outcome_maps_to_exactly_its_event_and_nothing_else() -> None:
    eid = EventId(UUID(int=99))
    done = outcome_event(finished("completed"), eid, START)
    assert isinstance(done, IngestionCompleted) and done.stage_count == 2
    failed = outcome_event(finished("failed"), eid, START)
    assert isinstance(failed, IngestionFailed) and failed.error_code == "unsupported"
    quarantined = outcome_event(finished("quarantined"), eid, START)
    assert isinstance(quarantined, IngestionQuarantined)
    assert quarantined.error_code == "malware_detected"
    for event, run in ((done, "completed"), (failed, "failed"), (quarantined, "quarantined")):
        assert (event.event_id, event.tenant_id, event.occurred_at) == (eid, TENANT_A, START)
        assert (event.document, event.version) == (D1, V1)
        assert event.run_id == finished(run).id


@pytest.mark.parametrize("kind", ["running", "dead_lettered", "cancelled"])
def test_a_run_without_an_outcome_has_no_event(kind: str) -> None:
    run = IngestionRun.start(TENANT_A, D1, V1, 1, ("scan",), START)
    if kind == "dead_lettered":
        run.dead_letter("x", START)
    if kind == "cancelled":
        run.cancel(START)
    with pytest.raises(InvalidTransition):
        outcome_event(run, EventId(UUID(int=1)), START)


def test_events_carry_identifiers_counters_and_codes_only() -> None:
    import dataclasses

    allowed = {"event_id", "tenant_id", "occurred_at", "document", "version", "run_id"}
    allowed |= {"stage_count", "error_code"}
    for event_type in (IngestionCompleted, IngestionFailed, IngestionQuarantined):
        assert {f.name for f in dataclasses.fields(event_type)} <= allowed
    assert IngestionCompleted.type_name == "ingestion.IngestionCompleted"
    assert IngestionFailed.type_name == "ingestion.IngestionFailed"
    assert IngestionQuarantined.type_name == "ingestion.IngestionQuarantined"
