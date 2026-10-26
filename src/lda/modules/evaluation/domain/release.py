"""Run records, release criteria and the pure eligibility verifier (ADR-041 D3, D4).

An `EvaluationRecord` is one evaluation run bound to everything that makes it meaningful: the
exact configuration (its components, not just an id), the dataset identity and fingerprint, the
gate policy it was judged against, the result and the time. It has one canonical text form
(`evaluation.run_record.v1`: sorted keys, compact separators, ASCII, no NaN) whose SHA-256 is the
run's integrity hash. The record holds identifiers, counts and scores only: no question, answer,
passage or secret.

`verify_run` is pure and fails closed: it returns every failed condition, and a run is eligible
only when the list is empty. It trusts nothing it is given: it recomputes the hash, re-parses the
text, requires the text to be canonical, recomputes the configuration id from the stored
components, recomputes every metric mean from the per-case scores and re-applies the gate to the
stored policy. Time is an integer number of seconds since the epoch, supplied by the caller.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass

from lda.modules.evaluation.domain import codes
from lda.modules.evaluation.domain.configuration import ConfigComponent, RagConfiguration
from lda.modules.evaluation.domain.dataset import EvaluationDataset
from lda.modules.evaluation.domain.gate import (
    GateDecision,
    GateFailure,
    GatePolicy,
    MetricThreshold,
    decide,
)
from lda.modules.evaluation.domain.outcome import Outcome
from lda.modules.evaluation.domain.report import CaseResult, EvaluationReport, MetricResult
from lda.modules.evaluation.domain.values import label, positive_int
from lda.shared.kernel.errors import ValidationError

RECORD_SCHEMA = "evaluation.run_record.v1"
MAX_AGE_SECONDS_CEILING = 2_592_000  # 30 days
_RUN_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _invalid(field: str) -> ValidationError:
    return ValidationError(codes.INVALID_INPUT, detail=field)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ReleaseCriteria:
    """What counts as evidence for a release: the approved dataset (identity, fingerprint and
    case ids), the gate policy and the maximum evidence age. Built from the real dataset, so a
    changed dataset changes the fingerprint and cannot satisfy criteria built before the change."""

    dataset_id: str
    dataset_version: int
    dataset_fingerprint: str
    case_ids: tuple[str, ...]
    policy: GatePolicy
    max_age_seconds: int

    def __post_init__(self) -> None:
        label(self.dataset_id, "dataset_id")
        positive_int(self.dataset_version, "dataset_version")
        if type(self.dataset_fingerprint) is not str or not _SHA256.fullmatch(
            self.dataset_fingerprint
        ):
            raise _invalid("dataset_fingerprint")
        if not self.case_ids or len(set(self.case_ids)) != len(self.case_ids):
            raise _invalid("case_ids")
        if not isinstance(self.policy, GatePolicy):
            raise _invalid("policy")
        if self.policy.min_cases < len(self.case_ids):
            raise _invalid("min_cases")  # a gate that accepts a partial run is not a gate
        if self.policy.max_error_rate != 0.0:
            raise _invalid("max_error_rate")  # release evidence tolerates no errored case
        if (
            type(self.max_age_seconds) is not int
            or not 1 <= self.max_age_seconds <= MAX_AGE_SECONDS_CEILING
        ):
            raise _invalid("max_age_seconds")

    @classmethod
    def from_dataset(
        cls, dataset: EvaluationDataset, policy: GatePolicy, max_age_seconds: int
    ) -> ReleaseCriteria:
        return cls(
            dataset.dataset_id,
            dataset.version,
            dataset.fingerprint(),
            tuple(c.case_id for c in dataset.cases),
            policy,
            max_age_seconds,
        )


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    run_id: str
    evaluated_at: int
    configuration: RagConfiguration
    dataset_id: str
    dataset_version: int
    dataset_fingerprint: str
    policy: GatePolicy
    report: EvaluationReport

    def __post_init__(self) -> None:
        if type(self.run_id) is not str or not _RUN_ID.fullmatch(self.run_id):
            raise _invalid("run_id")
        if type(self.evaluated_at) is not int or self.evaluated_at < 0:
            raise _invalid("evaluated_at")
        if not isinstance(self.configuration, RagConfiguration) or not isinstance(
            self.policy, GatePolicy
        ):
            raise _invalid("configuration")
        r = self.report
        if (
            r.config_id != self.configuration.config_id
            or r.dataset_id != self.dataset_id
            or r.dataset_version != self.dataset_version
            or r.dataset_fingerprint != self.dataset_fingerprint
        ):
            raise _invalid("report")

    @property
    def config_id(self) -> str:
        return self.report.config_id

    @property
    def passed(self) -> bool:
        return self.report.decision.passed

    def canonical(self) -> str:
        r = self.report
        body: dict[str, object] = {
            "schema": RECORD_SCHEMA,
            "run_id": self.run_id,
            "evaluated_at": self.evaluated_at,
            "configuration": {
                "config_id": self.configuration.config_id,
                "components": [
                    [c.name, [list(p) for p in c.parameters]] for c in self.configuration.components
                ],
            },
            "dataset": {
                "id": self.dataset_id,
                "version": self.dataset_version,
                "fingerprint": self.dataset_fingerprint,
            },
            "policy": {
                "thresholds": [[t.metric, t.minimum] for t in self.policy.thresholds],
                "min_cases": self.policy.min_cases,
                "max_error_rate": self.policy.max_error_rate,
            },
            "report": {
                "case_count": r.case_count,
                "error_count": r.error_count,
                "metrics": [[m.metric, m.cases_measured, m.mean] for m in r.metrics],
                "cases": [
                    [c.case_id, c.outcome.value, [list(s) for s in c.scores], c.error_code]
                    for c in r.cases
                ],
                "passed": r.decision.passed,
                "failures": [[f.code, f.subject] for f in r.decision.failures],
            },
        }
        return json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )

    def sha256(self) -> str:
        return sha256_text(self.canonical())

    @classmethod
    def from_canonical(cls, text: str) -> EvaluationRecord:
        """Strict inverse of `canonical()`. Anything else, including a re-spelled but equivalent
        text, is `evaluation_record_corrupt`: the hash covers exact text, so only exact text
        is accepted."""
        try:
            record = _parse(text)
            if record.canonical() != text:
                raise ValueError
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, ValidationError):
            raise ValidationError(codes.RECORD_CORRUPT) from None
        return record


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out = dict(pairs)
    if len(out) != len(pairs):
        raise ValueError
    return out


def _reject_constant(_: str) -> object:
    raise ValueError


def _parse(text: str) -> EvaluationRecord:
    raw = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    if set(raw) != {
        "schema",
        "run_id",
        "evaluated_at",
        "configuration",
        "dataset",
        "policy",
        "report",
    }:
        raise ValueError
    if raw["schema"] != RECORD_SCHEMA:
        raise ValueError
    config = raw["configuration"]
    configuration = RagConfiguration(
        tuple(
            ConfigComponent(name, tuple((k, v) for k, v in params))
            for name, params in config["components"]
        )
    )
    if config["config_id"] != configuration.config_id:
        raise ValueError
    dataset, policy, rep = raw["dataset"], raw["policy"], raw["report"]
    gate = GatePolicy(
        tuple(MetricThreshold(m, v) for m, v in policy["thresholds"]),
        policy["min_cases"],
        policy["max_error_rate"],
    )
    report = EvaluationReport(
        config_id=config["config_id"],
        dataset_id=dataset["id"],
        dataset_version=dataset["version"],
        dataset_fingerprint=dataset["fingerprint"],
        case_count=rep["case_count"],
        error_count=rep["error_count"],
        metrics=tuple(MetricResult(m, n, mean) for m, n, mean in rep["metrics"]),
        cases=tuple(
            CaseResult(cid, Outcome(outcome), tuple((m, s) for m, s in scores), code)
            for cid, outcome, scores, code in rep["cases"]
        ),
        decision=GateDecision(
            rep["passed"], tuple(GateFailure(code, subject) for code, subject in rep["failures"])
        ),
    )
    return EvaluationRecord(
        raw["run_id"], raw["evaluated_at"], configuration, dataset["id"], dataset["version"],
        dataset["fingerprint"], gate, report,
    )  # fmt: skip


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    """`eligible` is true exactly when `failures` is empty. It records that evidence verified;
    it deploys nothing and says nothing about production readiness or legal correctness."""

    eligible: bool
    failures: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.eligible != (not self.failures) or len(set(self.failures)) != len(self.failures):
            raise _invalid("eligibility")

    @classmethod
    def of(cls, failures: list[str]) -> EligibilityDecision:
        unique = tuple(dict.fromkeys(failures))
        return cls(not unique, unique)


def _scores_consistent(report: EvaluationReport) -> bool:
    per_metric: dict[str, list[float]] = {}
    for case in report.cases:
        for metric, score in case.scores:
            per_metric.setdefault(metric, []).append(score)
    stored = {m.metric: m for m in report.metrics}
    if set(per_metric) != set(stored) or len(stored) != len(report.metrics):
        return False
    return all(
        stored[name].cases_measured == len(values)
        and stored[name].mean == math.fsum(values) / len(values)
        for name, values in per_metric.items()
    )


def verify_run(
    text: str,
    stored_sha256: str,
    criteria: ReleaseCriteria,
    effective_config_id: str,
    now: int,
) -> tuple[EvaluationRecord | None, list[str]]:
    """Every condition of D4 for one stored run. Returns the parsed record (None when the text is
    not trustworthy) and the failed conditions; empty means the run is valid evidence now."""
    if sha256_text(text) != stored_sha256:
        return None, [codes.ELIGIBILITY_RECORD_CORRUPT]
    try:
        record = EvaluationRecord.from_canonical(text)
    except ValidationError:
        return None, [codes.ELIGIBILITY_RECORD_CORRUPT]
    failures: list[str] = []
    if record.config_id != effective_config_id:
        failures.append(codes.ELIGIBILITY_CONFIG_MISMATCH)
    if (
        record.dataset_id != criteria.dataset_id
        or record.dataset_version != criteria.dataset_version
        or record.dataset_fingerprint != criteria.dataset_fingerprint
    ):
        failures.append(codes.ELIGIBILITY_DATASET_MISMATCH)
    if record.policy != criteria.policy:
        failures.append(codes.ELIGIBILITY_POLICY_MISMATCH)
    age = now - record.evaluated_at
    if age < 0:
        failures.append(codes.ELIGIBILITY_EVIDENCE_FUTURE)
    elif age > criteria.max_age_seconds:  # inclusive: age == max_age_seconds is still valid
        failures.append(codes.ELIGIBILITY_EVIDENCE_STALE)
    report = record.report
    complete = (
        tuple(c.case_id for c in report.cases) == criteria.case_ids
        and report.case_count == len(criteria.case_ids)
        and {m.metric for m in report.metrics} >= {t.metric for t in criteria.policy.thresholds}
    )
    if not complete:
        failures.append(codes.ELIGIBILITY_INCOMPLETE)
    errors = sum(c.outcome is Outcome.ERROR for c in report.cases)
    consistent = report.error_count == errors and _scores_consistent(report)
    if consistent:
        recomputed = decide(
            record.policy, report.case_count, errors, {m.metric: m.mean for m in report.metrics}
        )
        consistent = recomputed == report.decision
    if not consistent:
        failures.append(codes.ELIGIBILITY_INCONSISTENT)
    if not report.decision.passed:
        failures.append(codes.ELIGIBILITY_RUN_FAILED)
    return record, failures
