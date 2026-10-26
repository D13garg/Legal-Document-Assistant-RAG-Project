"""What a system under test reports for one case (ADR-018): an outcome and source identifiers,
never text.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.modules.evaluation.domain.codes import INVALID_INPUT
from lda.modules.evaluation.domain.dataset import MAX_SOURCES_PER_CASE
from lda.modules.evaluation.domain.outcome import Outcome
from lda.modules.evaluation.domain.values import label_set
from lda.shared.kernel.errors import ValidationError


@dataclass(frozen=True, slots=True)
class Observation:
    """`retrieved_source_ids` is what retrieval returned; `cited_source_ids` what the answer cited.

    Answer text is deliberately absent: Step 12 scores structure and provenance only.
    Judging text needs
    a gateway call and is a later step (ADR-018 section 6).
    """

    outcome: Outcome
    retrieved_source_ids: tuple[str, ...] = ()
    cited_source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, Outcome):
            raise ValidationError(INVALID_INPUT, detail="outcome")
        retrieved = label_set(
            self.retrieved_source_ids, "retrieved_source_ids", max_items=MAX_SOURCES_PER_CASE
        )
        cited = label_set(self.cited_source_ids, "cited_source_ids", max_items=MAX_SOURCES_PER_CASE)
        if self.outcome is not Outcome.ANSWERED and cited:
            raise ValidationError(
                INVALID_INPUT, detail="cited_source_ids"
            )  # citing without answering
        object.__setattr__(self, "retrieved_source_ids", retrieved)
        object.__setattr__(self, "cited_source_ids", cited)

    @classmethod
    def errored(cls) -> Observation:
        return cls(Outcome.ERROR)
