"""The code-defined template catalog (ADR-017).

`PRODUCTION_TEMPLATES` holds exactly the templates whose wording has been explicitly approved:
currently only `grounded_answer` version 1 (approved by the project owner on 2026-10-16 for one
exact SHA-256; ADR-029). Add or change one only through a new explicit approval of the exact
wording.
Test fixtures live in `sample_template.py` and are reachable only through `prompts.testing`.
"""

from __future__ import annotations

from lda.modules.prompts.domain.catalog.proposed_grounded_answer import PROPOSED_GROUNDED_ANSWER
from lda.modules.prompts.domain.template import PromptTemplate

PRODUCTION_TEMPLATES: tuple[PromptTemplate, ...] = (PROPOSED_GROUNDED_ANSWER,)
