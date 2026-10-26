"""Public surface of grounded_generation (ADR-029): one use case, its command and result.

`GenerateGroundedAnswer` takes untrusted source claims and a question and returns a validated,
cited answer, or an explicit refusal (`INSUFFICIENT_EVIDENCE`) or rejection (`REJECTED`). It never
returns provider text that failed the answer contract. Adapters (the Access Control bridge and the
PostgreSQL unit of work) are composed by bootstrap and are not part of this surface.
"""

from lda.modules.grounded_generation.application.answer_drafter import AnswerDrafter
from lda.modules.grounded_generation.application.claim_reference import (
    ClaimReference,
    claims_from_references,
)
from lda.modules.grounded_generation.application.dto import (
    AnswerStatement,
    GenerateGroundedAnswerCommand,
    GroundedAnswer,
)
from lda.modules.grounded_generation.application.evidence_assembler import EvidenceAssembler
from lda.modules.grounded_generation.application.policy import GroundingPolicy
from lda.modules.grounded_generation.application.ports import (
    DocumentAccess,
    GroundedUnitOfWork,
    GroundedUnitOfWorkFactory,
)
from lda.modules.grounded_generation.application.use_cases.generate_grounded_answer import (
    GenerateGroundedAnswer,
)
from lda.modules.grounded_generation.domain.answer_contract import ContractLimits
from lda.modules.grounded_generation.domain.codes import (
    INVALID_INPUT,
    LIMIT_EXCEEDED,
    NOT_AUTHORIZED,
    OUTPUT_CITATION_INVALID,
    OUTPUT_MALFORMED,
    OUTPUT_QUOTE_UNSUPPORTED,
    OUTPUT_SCHEMA,
    OUTPUT_TOO_LARGE,
    OUTPUT_UNSAFE_TEXT,
    PERSISTENCE_FAILED,
    REQUIRES_PRINCIPAL,
    TEMPLATE_UNAVAILABLE,
)

__all__ = [
    "INVALID_INPUT",
    "LIMIT_EXCEEDED",
    "NOT_AUTHORIZED",
    "OUTPUT_CITATION_INVALID",
    "OUTPUT_MALFORMED",
    "OUTPUT_QUOTE_UNSUPPORTED",
    "OUTPUT_SCHEMA",
    "OUTPUT_TOO_LARGE",
    "OUTPUT_UNSAFE_TEXT",
    "PERSISTENCE_FAILED",
    "REQUIRES_PRINCIPAL",
    "TEMPLATE_UNAVAILABLE",
    "AnswerDrafter",
    "AnswerStatement",
    "ClaimReference",
    "ContractLimits",
    "DocumentAccess",
    "EvidenceAssembler",
    "GenerateGroundedAnswer",
    "GenerateGroundedAnswerCommand",
    "GroundedAnswer",
    "GroundedUnitOfWork",
    "GroundedUnitOfWorkFactory",
    "GroundingPolicy",
    "claims_from_references",
]
