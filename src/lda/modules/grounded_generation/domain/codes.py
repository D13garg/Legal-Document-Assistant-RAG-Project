"""Stable, content-free codes of grounded generation (ADR-029). No code embeds a value."""

from __future__ import annotations

INVALID_INPUT = "grounded_invalid_input"
LIMIT_EXCEEDED = "grounded_limit_exceeded"
REQUIRES_PRINCIPAL = "grounded_requires_principal"
NOT_AUTHORIZED = "grounded_not_authorized"
TEMPLATE_UNAVAILABLE = "grounded_template_unavailable"
PERSISTENCE_FAILED = "grounded_persistence_failed"

# Why a provider reply was rejected (recorded as outcome `rejected`; the reply is never shown).
OUTPUT_TOO_LARGE = "grounded_output_too_large"
OUTPUT_MALFORMED = "grounded_output_malformed"
OUTPUT_SCHEMA = "grounded_output_schema"
OUTPUT_UNSAFE_TEXT = "grounded_output_unsafe_text"
OUTPUT_CITATION_INVALID = "grounded_output_citation_invalid"
OUTPUT_QUOTE_UNSUPPORTED = "grounded_output_quote_unsupported"

REJECTION_CODES = frozenset(
    {
        OUTPUT_TOO_LARGE,
        OUTPUT_MALFORMED,
        OUTPUT_SCHEMA,
        OUTPUT_UNSAFE_TEXT,
        OUTPUT_CITATION_INVALID,
        OUTPUT_QUOTE_UNSUPPORTED,
    }
)
