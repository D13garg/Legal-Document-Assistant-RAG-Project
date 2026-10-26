"""Request and result shapes of the egress use case (ADR-006).

Tenant, principal and authorization claims are NOT fields here: they come from the execution
context and the proof. Every piece of text is `Sensitive`, so `repr`, `str`, formatting and
pickling cannot leak it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lda.modules.ai_gateway.domain.parameters import GenerationParameters
from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofResource


@dataclass(frozen=True, slots=True)
class ContextSegment:
    """One piece of context text and the resource it came from. The source is mandatory: the exact
    resource set the proof must bind is derived from the segments, never supplied separately."""

    resource: ProofResource
    text: Sensitive[str]


@dataclass(frozen=True, slots=True)
class GenerateTextCommand:
    proof_token: Sensitive[str]
    action: str
    model: str
    instructions: Sensitive[str]
    segments: tuple[ContextSegment, ...]
    parameters: GenerationParameters = field(default_factory=GenerationParameters)


@dataclass(frozen=True, slots=True)
class CallEvidence:
    """Content-free evidence of one egress, for the later provenance record. Identifiers, counts and
    a digest only; no prompt, context, completion, token or credential."""

    provider_id: str
    model: str
    parameters: GenerationParameters
    request_sha256: str
    resources: tuple[ProofResource, ...]
    """Sorted by `(kind, id)`: the exact set the proof was verified against."""
    attempts: int
    """Provider calls made, including retries of pre-response transport failures."""


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: Sensitive[str]
    evidence: CallEvidence
