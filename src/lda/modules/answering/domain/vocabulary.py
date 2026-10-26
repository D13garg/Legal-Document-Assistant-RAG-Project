"""Values this module must know without importing the modules that define them.

The DAG gives `answering` no `documents` or `citations` edge, so these are literal copies, the same
pattern `citations` uses. `tests/architecture/test_answering_boundaries.py` pins each one to its
source of truth; if one drifts, that test fails.
"""

from __future__ import annotations

import re
from enum import StrEnum

DOCUMENT_KIND = "document"
"""Same value as `documents.api.DOCUMENT_KIND`; a pin in tests/architecture checks equality."""
READ_DOCUMENT = "documents.read_document"
"""Same value as `documents.api.READ_DOCUMENT`; a pin in tests/architecture checks equality."""
CONFIG_ID = re.compile(r"rag-[0-9a-f]{64}")
"""Same shape as the grounded-generation `config_id` (the evaluation `RagConfiguration` id)."""


class AnswerOutcome(StrEnum):
    """Same values as `citations.api.GenerationOutcome`; a pin in tests/architecture checks it."""

    ANSWERED = "answered"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    REJECTED = "rejected"
