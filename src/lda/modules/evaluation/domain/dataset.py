"""Evaluation cases and datasets (ADR-018). Immutable, validated, content-free except the question.

A dataset is synthetic or otherwise non-customer material: Step 12 has no tenant, no persistence
and no
authorization context, so tenant data must never be loaded into one. The question is `Sensitive`
so it
cannot leak through `repr`, logs or errors; everything else is an opaque label.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from lda.modules.evaluation.domain.codes import INVALID_INPUT
from lda.modules.evaluation.domain.outcome import Outcome
from lda.modules.evaluation.domain.values import label, label_set, positive_int
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.sensitive import Sensitive

MAX_SOURCES_PER_CASE = 100
MAX_CASES = 10_000
FINGERPRINT_SCHEMA = "evaluation.dataset.v1"


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """One question with its expected outcome.

    `expected_source_ids` are the sources a correct ANSWER must cite (non-empty exactly when
    an answer is
    expected: no citation without a source, invariant 4). `forbidden_source_ids` are sources
    the asking
    principal must never receive; seeing one in retrieval or citations is an isolation failure.
    """

    case_id: str
    question: Sensitive[str]
    expected_outcome: Outcome
    expected_source_ids: tuple[str, ...] = ()
    forbidden_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", label(self.case_id, "case_id"))
        if not isinstance(self.question, Sensitive) or not _is_text(self.question.reveal()):
            raise ValidationError(INVALID_INPUT, detail="question")
        if self.expected_outcome not in (Outcome.ANSWERED, Outcome.INSUFFICIENT_EVIDENCE):
            raise ValidationError(INVALID_INPUT, detail="expected_outcome")
        expected = label_set(
            self.expected_source_ids, "expected_source_ids", max_items=MAX_SOURCES_PER_CASE
        )
        forbidden = label_set(
            self.forbidden_source_ids, "forbidden_source_ids", max_items=MAX_SOURCES_PER_CASE
        )
        if bool(expected) != (self.expected_outcome is Outcome.ANSWERED) or set(expected) & set(
            forbidden
        ):
            raise ValidationError(INVALID_INPUT, detail="expected_source_ids")
        object.__setattr__(self, "expected_source_ids", expected)
        object.__setattr__(self, "forbidden_source_ids", forbidden)


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    """A named, versioned, ordered set of cases with unique ids."""

    dataset_id: str
    version: int
    cases: tuple[EvaluationCase, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", label(self.dataset_id, "dataset_id"))
        positive_int(self.version, "version")
        if not isinstance(self.cases, tuple) or not 1 <= len(self.cases) <= MAX_CASES:
            raise ValidationError(INVALID_INPUT, detail="cases")
        if not all(isinstance(c, EvaluationCase) for c in self.cases):
            raise ValidationError(INVALID_INPUT, detail="cases")
        if len({c.case_id for c in self.cases}) != len(self.cases):
            raise ValidationError(INVALID_INPUT, detail="case_id")

    def fingerprint(self) -> str:
        """SHA-256 of the canonical dataset (`evaluation.dataset.v1`), so a report names
        exactly what was run.

        It covers the question text, so it is content-derived: it identifies a dataset
        in a report, and
        it must stay out of logs and client responses like any content hash (ADR-006 section 8).
        """
        canonical = json.dumps(
            {
                "schema": FINGERPRINT_SCHEMA,
                "dataset_id": self.dataset_id,
                "version": self.version,
                "cases": [
                    [
                        c.case_id,
                        c.question.reveal(),
                        c.expected_outcome.value,
                        list(c.expected_source_ids),
                        list(c.forbidden_source_ids),
                    ]
                    for c in self.cases
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\x00" not in value
