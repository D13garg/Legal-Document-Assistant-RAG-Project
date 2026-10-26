"""Neutral input and output shapes of prompt rendering (ADR-017).

These are prompts' own types. They are not gateway DTOs and carry no authorization claim: a
`ProofResource` here is provenance the caller states, never proof of access. The later composition
layer maps `RenderedPrompt` into the gateway command; the gateway's injected `ProofVerifier`
remains the egress gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from lda.shared.kernel.sensitive import Sensitive
from lda.shared.ports.authorization import ProofResource


@dataclass(frozen=True, slots=True)
class PromptContextInput:
    """One piece of untrusted document text and the resource it came from."""

    resource: ProofResource
    text: Sensitive[str]


@dataclass(frozen=True, slots=True)
class PromptRequest:
    """Explicit template selection, the untrusted user question and the ordered context."""

    template_id: str
    template_version: int
    question: Sensitive[str]
    context: tuple[PromptContextInput, ...]


@dataclass(frozen=True, slots=True)
class RenderedContextPart:
    """Rendered (wrapped and escaped) context text with the provenance of its source."""

    resource: ProofResource
    text: Sensitive[str]


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """The renderer's whole output. `context` keeps the caller's order, one part per input."""

    template_id: str
    template_version: int
    instructions: Sensitive[str]
    context: tuple[RenderedContextPart, ...]
