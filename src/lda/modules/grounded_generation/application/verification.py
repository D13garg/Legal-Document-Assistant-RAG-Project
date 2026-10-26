"""Check a parsed answer against the evidence it may cite (ADR-029).

Citations must resolve to supplied evidence, and every statement's quote must occur, word for
word (whitespace-insensitive), in the passage it cites. The passage is the stored chunk text the
evidence was built from, never the prompt (which adds trust-boundary lines) and never anything the
model wrote. A failure rejects the whole answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lda.modules.citations.api import Citation, TextEvidence
from lda.modules.grounded_generation.domain import codes
from lda.modules.grounded_generation.domain.answer_contract import (
    ContractViolation,
    ParsedAnswer,
    normalize_span,
)
from lda.shared.kernel.errors import ValidationError


@dataclass(frozen=True, slots=True)
class VerifiedStatement:
    text: str = field(repr=False)
    quote: str = field(repr=False)
    citation: Citation


def verify_statements(
    parsed: ParsedAnswer, evidence: TextEvidence, max_markers: int
) -> tuple[VerifiedStatement, ...]:
    verified: list[VerifiedStatement] = []
    for statement in parsed.statements:
        try:
            (citation,) = evidence.evidence.resolve([statement.citation], max_markers=max_markers)
            passage = evidence.passage(statement.citation).text
        except ValidationError:
            raise ContractViolation(codes.OUTPUT_CITATION_INVALID) from None
        if normalize_span(statement.quote) not in normalize_span(passage):
            raise ContractViolation(codes.OUTPUT_QUOTE_UNSUPPORTED)
        verified.append(VerifiedStatement(statement.text, statement.quote, citation))
    return tuple(verified)
