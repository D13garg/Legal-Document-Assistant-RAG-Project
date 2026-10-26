"""Ports of release gating (ADR-041). The store returns what was stored, verbatim, and never
decides anything: every judgement is made by the domain verifier on the returned text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from lda.modules.evaluation.domain.release import EvaluationRecord


@dataclass(frozen=True, slots=True)
class StoredRun:
    sequence: int
    run_id: str
    record_text: str
    record_sha256: str


@dataclass(frozen=True, slots=True)
class StoredEligibility:
    sequence: int
    eligibility_id: str
    run_id: str
    run_sha256: str
    config_id: str
    recorded_at: int


@dataclass(frozen=True, slots=True)
class EvidenceSnapshot:
    """One consistent read (a single repeatable-read transaction).

    `latest_run` is the highest-sequence run for the configuration and dataset;
    `failed_since` counts failing runs for them with `evaluated_at >= since`;
    `latest_eligibility` is the highest-sequence eligibility record for the configuration.
    """

    latest_run: StoredRun | None
    failed_since: int
    latest_eligibility: StoredEligibility | None


class EvaluationStore(Protocol):
    def append_run(self, record: EvaluationRecord) -> StoredRun:
        """Insert-only. A repeated `run_id` raises `ConflictError(evaluation_run_exists)`."""
        ...

    def snapshot(
        self, config_id: str, dataset_id: str, dataset_version: int, since: int
    ) -> EvidenceSnapshot: ...

    def record_eligibility(
        self, eligibility_id: str, run: StoredRun, recorded_at: int
    ) -> tuple[StoredEligibility, bool]:
        """Insert-only and idempotent per run: returns the stored record and whether it already
        existed (`replayed`)."""
        ...
