"""Test-only in-memory `EvaluationStore` and builders for release gating (never imported by
production code). It mirrors the semantics of the Postgres store (insert-only, replay-safe
eligibility, one consistent snapshot); the real-database behaviour is proven by the PostgreSQL
tests, not by this double.
"""

from __future__ import annotations

from dataclasses import replace

from lda.modules.evaluation.application.release_ports import (
    EvidenceSnapshot,
    StoredEligibility,
    StoredRun,
)
from lda.modules.evaluation.domain.codes import RUN_EXISTS
from lda.modules.evaluation.domain.effective_configuration import (
    AnsweringIdentity,
    ChunkingIdentity,
    CitationsIdentity,
    EffectiveConfiguration,
    GenerationIdentity,
    PromptIdentity,
    RetrievalIdentity,
)
from lda.modules.evaluation.domain.release import EvaluationRecord
from lda.shared.kernel.errors import ConflictError


class InMemoryEvaluationStore:
    def __init__(self) -> None:
        self.runs: list[StoredRun] = []
        self.eligibility: list[StoredEligibility] = []
        self._meta: dict[str, tuple[str, str, int, bool]] = {}  # run -> (config, ds, ver, passed)
        self._times: dict[str, int] = {}

    def append_run(self, record: EvaluationRecord) -> StoredRun:
        if any(r.run_id == record.run_id for r in self.runs):
            raise ConflictError(RUN_EXISTS)
        stored = StoredRun(len(self.runs) + 1, record.run_id, record.canonical(), record.sha256())
        self.runs.append(stored)
        self._meta[record.run_id] = (
            record.config_id, record.dataset_id, record.dataset_version, record.passed,
        )  # fmt: skip
        self._times[record.run_id] = record.evaluated_at
        return stored

    def snapshot(
        self, config_id: str, dataset_id: str, dataset_version: int, since: int
    ) -> EvidenceSnapshot:
        matching = [
            r
            for r in self.runs
            if self._meta[r.run_id][:3] == (config_id, dataset_id, dataset_version)
        ]
        failed = sum(
            1 for r in matching if not self._meta[r.run_id][3] and self._times[r.run_id] >= since
        )
        entries = [e for e in self.eligibility if e.config_id == config_id]
        return EvidenceSnapshot(
            matching[-1] if matching else None, failed, entries[-1] if entries else None
        )

    def record_eligibility(
        self, eligibility_id: str, run: StoredRun, recorded_at: int
    ) -> tuple[StoredEligibility, bool]:
        for existing in self.eligibility:
            if existing.run_id == run.run_id:
                return existing, True
        config_id, _, _, passed = self._meta[run.run_id]
        if not passed or run.record_sha256 != next(
            r.record_sha256 for r in self.runs if r.run_id == run.run_id
        ):
            raise ConflictError("evaluation_eligibility_refused")
        entry = StoredEligibility(
            len(self.eligibility) + 1,
            eligibility_id,
            run.run_id,
            run.record_sha256,
            config_id,
            recorded_at,
        )
        self.eligibility.append(entry)
        return entry, False

    def tamper(self, index: int, **changes: object) -> None:
        """Replace a stored run's fields, as a corrupted or hand-edited store would."""
        self.runs[index] = replace(self.runs[index], **changes)  # type: ignore[arg-type]


def build_effective(**changes: object) -> EffectiveConfiguration:
    """A valid reference configuration. `changes` replace whole components or single parameters
    given as `component__parameter=value` (test-only convenience)."""
    parts: dict[str, object] = {
        "prompt": PromptIdentity("grounded_answer", 1, "2e3a3ce3" + "0" * 56),
        "generation": GenerationIdentity("fake-model:1", 1024, 0.0, 20, 24_000, 20, 1_500, 12, 400),
        "retrieval": RetrievalIdentity(20, 2_000, 200, 2_000),
        "chunking": ChunkingIdentity(2_000, 200, 50_000),
        "citations": CitationsIdentity(50, 200),
        "answering": AnsweringIdentity(100, 2_000, 8),
    }
    for key, value in changes.items():
        if "__" in key:
            component, parameter = key.split("__")
            parts[component] = replace(parts[component], **{parameter: value})  # type: ignore[type-var]
        else:
            parts[key] = value
    return EffectiveConfiguration(**parts)  # type: ignore[arg-type]
