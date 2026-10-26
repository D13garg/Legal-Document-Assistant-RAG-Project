"""ADR-041 D3/D4: canonical records and the fail-closed verifier, including tampering."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from lda.modules.evaluation import api
from lda.modules.evaluation.api import (
    ELIGIBILITY_CONFIG_MISMATCH,
    ELIGIBILITY_DATASET_MISMATCH,
    ELIGIBILITY_EVIDENCE_FUTURE,
    ELIGIBILITY_EVIDENCE_STALE,
    ELIGIBILITY_INCOMPLETE,
    ELIGIBILITY_INCONSISTENT,
    ELIGIBILITY_POLICY_MISMATCH,
    ELIGIBILITY_RECORD_CORRUPT,
    ELIGIBILITY_RUN_FAILED,
    RECORD_CORRUPT,
    RELEASE_DATASET,
    RELEASE_GATE_POLICY,
    EligibilityDecision,
    EvaluationRecord,
    GatePolicy,
    MetricThreshold,
    Observation,
    Outcome,
    ReleaseCriteria,
    RunEvaluation,
    default_metrics,
    verify_run,
)
from lda.modules.evaluation.application.dto import RunEvaluationCommand
from lda.modules.evaluation.testing import (
    PerfectTarget,
    ScriptedTarget,
    build_effective,
    default_limits,
)
from lda.shared.kernel.errors import ValidationError

RUN_ID = "00000000-0000-0000-0000-000000000001"
T0 = 1_000_000
MAX_AGE = 3_600
CRITERIA = ReleaseCriteria.from_dataset(RELEASE_DATASET, RELEASE_GATE_POLICY, MAX_AGE)


def make_record(target: object | None = None, at: int = T0, effective=None) -> EvaluationRecord:  # type: ignore[no-untyped-def]
    effective = effective or build_effective()
    runner = RunEvaluation(target or PerfectTarget(), default_metrics(), default_limits())  # type: ignore[arg-type]
    report = runner.execute(
        RunEvaluationCommand(RELEASE_DATASET, effective.configuration(), RELEASE_GATE_POLICY)
    )
    return EvaluationRecord(
        RUN_ID,
        at,
        effective.configuration(),
        RELEASE_DATASET.dataset_id,
        RELEASE_DATASET.version,
        RELEASE_DATASET.fingerprint(),
        RELEASE_GATE_POLICY,
        report,
    )


def check(record: EvaluationRecord, now: int = T0, text: str | None = None, sha: str | None = None):  # type: ignore[no-untyped-def]
    text = record.canonical() if text is None else text
    return verify_run(
        text, sha or api.EvaluationRecord.sha256(record), CRITERIA, build_effective().config_id, now
    )


def edit(record: EvaluationRecord, mutate) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """The canonical text after `mutate(dict)`, re-serialised canonically, and its OWN hash: the
    strongest attacker, one who fixes the hash too."""
    body = json.loads(record.canonical())
    mutate(body)
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return text, api.verify_run.__globals__["sha256_text"](text)


def test_a_valid_perfect_run_verifies_with_no_failure() -> None:
    parsed, failures = check(make_record())
    assert failures == [] and parsed is not None


def test_the_canonical_text_round_trips_and_is_ascii_and_content_free() -> None:
    record = make_record()
    text = record.canonical()
    assert EvaluationRecord.from_canonical(text) == record
    assert text.isascii()
    assert all(c.question.reveal() not in text for c in RELEASE_DATASET.cases)


@pytest.mark.parametrize("age", [0, 1, MAX_AGE - 1, MAX_AGE])
def test_evidence_is_valid_up_to_and_including_the_maximum_age(age: int) -> None:
    assert check(make_record(), now=T0 + age)[1] == []


def test_evidence_one_second_past_the_maximum_age_is_stale() -> None:
    assert check(make_record(), now=T0 + MAX_AGE + 1)[1] == [ELIGIBILITY_EVIDENCE_STALE]


def test_evidence_from_the_future_is_refused() -> None:
    assert check(make_record(), now=T0 - 1)[1] == [ELIGIBILITY_EVIDENCE_FUTURE]


def test_a_failing_run_is_never_eligible() -> None:
    script = {c.case_id: Observation(Outcome.INSUFFICIENT_EVIDENCE) for c in RELEASE_DATASET.cases}
    record = make_record(ScriptedTarget(script))
    assert not record.passed
    assert ELIGIBILITY_RUN_FAILED in check(record)[1]


def test_an_errored_case_blocks_even_when_every_mean_is_high() -> None:
    script = {c.case_id: PerfectTarget().observe(c) for c in RELEASE_DATASET.cases} | {
        "supported-01": RuntimeError("boom")
    }
    record = make_record(ScriptedTarget(script))
    assert not record.passed and record.report.error_count == 1
    assert ELIGIBILITY_RUN_FAILED in check(record)[1]


def test_the_wrong_effective_configuration_is_a_mismatch() -> None:
    record = make_record()
    other = build_effective(generation__temperature=0.5).config_id
    _, failures = verify_run(record.canonical(), record.sha256(), CRITERIA, other, T0)
    assert failures == [ELIGIBILITY_CONFIG_MISMATCH]


def test_a_stale_configuration_change_cannot_reuse_prior_evidence() -> None:
    old = make_record()
    changed = build_effective(retrieval__max_results=19)
    _, failures = verify_run(old.canonical(), old.sha256(), CRITERIA, changed.config_id, T0)
    assert ELIGIBILITY_CONFIG_MISMATCH in failures


def test_a_changed_dataset_version_or_fingerprint_is_a_mismatch() -> None:
    record = make_record()
    other = replace(CRITERIA, dataset_fingerprint="f" * 64)
    _, failures = verify_run(
        record.canonical(), record.sha256(), other, build_effective().config_id, T0
    )
    assert failures == [ELIGIBILITY_DATASET_MISMATCH]
    other = replace(CRITERIA, dataset_version=2)
    assert (
        ELIGIBILITY_DATASET_MISMATCH
        in verify_run(record.canonical(), record.sha256(), other, build_effective().config_id, T0)[
            1
        ]
    )


def test_a_run_judged_against_a_lenient_policy_is_refused() -> None:
    lenient = GatePolicy((MetricThreshold("outcome_accuracy", 0.1),), 14, 0.0)
    runner = RunEvaluation(PerfectTarget(), default_metrics(), default_limits())
    effective = build_effective()
    report = runner.execute(
        RunEvaluationCommand(RELEASE_DATASET, effective.configuration(), lenient)
    )
    record = EvaluationRecord(
        RUN_ID, T0, effective.configuration(), RELEASE_DATASET.dataset_id,
        RELEASE_DATASET.version, RELEASE_DATASET.fingerprint(), lenient, report,
    )  # fmt: skip
    assert check(record)[1] == [ELIGIBILITY_POLICY_MISMATCH]


def test_a_text_that_does_not_match_its_hash_is_corrupt_and_not_parsed() -> None:
    record = make_record()
    parsed, failures = verify_run(
        record.canonical() + " ", record.sha256(), CRITERIA, build_effective().config_id, T0
    )
    assert (parsed, failures) == (None, [ELIGIBILITY_RECORD_CORRUPT])
    assert verify_run(record.canonical(), "0" * 64, CRITERIA, build_effective().config_id, T0)[
        1
    ] == [ELIGIBILITY_RECORD_CORRUPT]


def test_a_hand_edited_failure_to_pass_is_caught_even_with_a_recomputed_hash() -> None:
    script = {c.case_id: Observation(Outcome.INSUFFICIENT_EVIDENCE) for c in RELEASE_DATASET.cases}
    record = make_record(ScriptedTarget(script))

    def flip(body: dict) -> None:  # type: ignore[type-arg]
        body["report"]["passed"] = True
        body["report"]["failures"] = []

    text, sha = edit(record, flip)
    assert verify_run(text, sha, CRITERIA, build_effective().config_id, T0)[1] == [
        ELIGIBILITY_INCONSISTENT
    ]


def test_a_hand_edited_metric_mean_is_caught_even_with_a_recomputed_hash() -> None:
    record = make_record()

    def raise_mean(body: dict) -> None:  # type: ignore[type-arg]
        body["report"]["metrics"][0][2] = 1.0
        body["report"]["metrics"][1][2] = 0.5

    text, sha = edit(record, raise_mean)
    assert (
        ELIGIBILITY_INCONSISTENT
        in verify_run(text, sha, CRITERIA, build_effective().config_id, T0)[1]
    )


def test_a_hand_edited_configuration_id_or_components_is_corrupt() -> None:
    record = make_record()
    other = "rag-" + "0" * 64

    def forge_id(body: dict) -> None:  # type: ignore[type-arg]
        body["configuration"]["config_id"] = other

    def forge_component(body: dict) -> None:  # type: ignore[type-arg]
        body["configuration"]["components"][0][1][0][1] = 999

    for mutate in (forge_id, forge_component):
        text, sha = edit(record, mutate)
        assert verify_run(text, sha, CRITERIA, other, T0)[1] == [ELIGIBILITY_RECORD_CORRUPT]


def test_a_partial_run_is_incomplete() -> None:
    record = make_record()

    def drop_case(body: dict) -> None:  # type: ignore[type-arg]
        body["report"]["cases"].pop()
        body["report"]["case_count"] -= 1

    text, sha = edit(record, drop_case)
    failures = verify_run(text, sha, CRITERIA, build_effective().config_id, T0)[1]
    assert ELIGIBILITY_INCOMPLETE in failures


def test_a_missing_required_metric_is_incomplete() -> None:
    record = make_record()

    def drop_metric(body: dict) -> None:  # type: ignore[type-arg]
        body["report"]["metrics"] = [m for m in body["report"]["metrics"] if m[0] != "isolation"]
        for case in body["report"]["cases"]:
            case[2] = [s for s in case[2] if s[0] != "isolation"]

    text, sha = edit(record, drop_metric)
    assert (
        ELIGIBILITY_INCOMPLETE
        in verify_run(text, sha, CRITERIA, build_effective().config_id, T0)[1]
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "null",
        "[]",
        "{}",
        '{"schema":"other"}',
        "NaN",
        '{"a":1,"a":2}',
    ],
)
def test_garbage_is_a_content_free_corruption_error(text: str) -> None:
    with pytest.raises(ValidationError) as error:
        EvaluationRecord.from_canonical(text)
    assert error.value.args[0] == RECORD_CORRUPT or RECORD_CORRUPT in str(error.value)


def test_non_canonical_but_equivalent_text_is_refused() -> None:
    text = make_record().canonical()
    spaced = json.dumps(json.loads(text), sort_keys=True, indent=1)
    with pytest.raises(ValidationError):
        EvaluationRecord.from_canonical(spaced)


def test_nan_cannot_be_hidden_in_a_score() -> None:
    text = (
        make_record().canonical().replace('"outcome_accuracy",14,1.0', '"outcome_accuracy",14,NaN')
    )
    assert "NaN" in text
    with pytest.raises(ValidationError):
        EvaluationRecord.from_canonical(text)


def test_eligibility_decision_invariants() -> None:
    assert EligibilityDecision.of([]).eligible
    assert not EligibilityDecision.of(["x", "x"]).eligible
    assert EligibilityDecision.of(["x", "x"]).failures == ("x",)
    with pytest.raises(ValidationError):
        EligibilityDecision(True, ("x",))
    with pytest.raises(ValidationError):
        EligibilityDecision(False, ())


def test_criteria_refuse_a_gate_that_tolerates_partial_runs_or_errors() -> None:
    with pytest.raises(ValidationError):
        replace(CRITERIA, policy=replace(RELEASE_GATE_POLICY, min_cases=13))
    with pytest.raises(ValidationError):
        replace(CRITERIA, policy=replace(RELEASE_GATE_POLICY, max_error_rate=0.05))
    with pytest.raises(ValidationError):
        replace(CRITERIA, max_age_seconds=0)
    with pytest.raises(ValidationError):
        replace(CRITERIA, max_age_seconds=2_592_001)


def test_a_record_must_agree_with_its_own_report() -> None:
    record = make_record()
    with pytest.raises(ValidationError):
        replace(record, dataset_fingerprint="e" * 64)
    with pytest.raises(ValidationError):
        replace(record, run_id="not-a-uuid")
    with pytest.raises(ValidationError):
        replace(record, evaluated_at=-1)
