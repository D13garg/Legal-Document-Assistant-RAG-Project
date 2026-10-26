"""Stable, content-free codes of answering (ADR-040). No code embeds a question, id or answer.

Errors raised by the modules this one calls (`conversation_*`, `search_*`, `grounded_*`,
`citations_*`) pass through unchanged: they are already stable and sanitized.
"""

from __future__ import annotations

REQUIRES_PRINCIPAL = "answering_requires_principal"
INVALID_COMMAND = "answering_invalid_command"
INVALID_QUESTION = "answering_question_invalid"
INVALID_CANDIDATES = "answering_candidates_invalid"
NOT_AUTHORIZED = "answering_not_authorized"
TURN_NOT_RECORDED = "answering_turn_not_recorded"
"""The answer was generated and a generation record exists, but it could not be linked to the
conversation: the answer is withheld (ADR-040 Q4.2). `detail` holds the cause's code only."""
LIMITS_INVALID = "answering_limits_invalid"
