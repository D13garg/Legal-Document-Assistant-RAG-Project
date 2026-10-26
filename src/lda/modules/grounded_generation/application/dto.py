"""Request and result of grounded generation (ADR-029).

The command carries untrusted claims, never evidence and never text: the evidence is built by this
module after authorization. Statement text and quotes are content, so they are `Sensitive`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from lda.modules.citations.api import Citation, GenerationOutcome, SourceClaim
from lda.modules.grounded_generation.domain import codes
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.sensitive import Sensitive


@dataclass(frozen=True, slots=True)
class GenerateGroundedAnswerCommand:
    question: Sensitive[str]
    claims: tuple[SourceClaim, ...]
    """Chunk references as retrieval ranked them (rank order becomes marker order). Untrusted:
    every claim is checked against the chunk store, and the caller's access to every cited document
    is decided here, not assumed from the search that produced the claims."""
    config_id: str
    """The evaluation `RagConfiguration.config_id` the answer is produced under (`rag-<sha256>`)."""


@dataclass(frozen=True, slots=True)
class AnswerStatement:
    """One verified statement: its quote occurs in the cited source (not an entailment proof)."""

    text: Sensitive[str]
    quote: Sensitive[str]
    citation: Citation
    """Identifiers and pages of the cited chunk. Showing the source needs a fresh authorization."""


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    """`ANSWERED` carries statements; every other outcome carries none.

    `INSUFFICIENT_EVIDENCE` is either the model's explicit refusal (recorded) or the caller supplied
    no sources at all (`record_id` is None: nothing was generated, so nothing is recorded).
    `REJECTED` means the provider's reply failed the answer contract and was discarded; `rejection`
    is the stable reason code and the reply is not available anywhere.
    """

    outcome: GenerationOutcome
    statements: tuple[AnswerStatement, ...] = field(repr=False)
    record_id: UUID | None
    rejection: str | None = None

    def __post_init__(self) -> None:
        answered = self.outcome is GenerationOutcome.ANSWERED
        rejected = self.outcome is GenerationOutcome.REJECTED
        if (
            not isinstance(self.outcome, GenerationOutcome)
            or answered != bool(self.statements)
            or rejected != (self.rejection is not None)
            or (
                self.record_id is None
                and self.outcome is not GenerationOutcome.INSUFFICIENT_EVIDENCE
            )
        ):
            raise ValidationError(codes.INVALID_INPUT, detail="answer")
