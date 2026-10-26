"""D8 stays a real gate: the review request names the exact shipped wording, and nothing is approved.

The review request is what `@legal-rag/ai-quality` is asked to sign. If the template wording changes
without the request changing (or the reverse), an approval of the document's hash would not be an
approval of what ships, so this pin fails. It also pins that the document records no approval, so a
passing test run can never be mistaken for one.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from lda.modules.prompts.domain.catalog import PRODUCTION_TEMPLATES
from lda.modules.prompts.domain.catalog.proposed_grounded_answer import PROPOSED_GROUNDED_ANSWER

ROOT = Path(__file__).resolve().parents[2]
REQUEST = ROOT / "docs/ai/grounded-answer-v1-review-request.md"

# The wording the project owner approved on 2026-10-16. Changing the template's wording changes this
# hash: that needs a NEW explicit owner approval (a new template version and a new hash), never an
# edit of this constant to make the pin pass.
APPROVED_SHA256 = "2e3a3ce3b2b0a36bf212a4ed7ec3d07ec2f4ebee4f3882694071e48cd64cb94a"


def request_text() -> str:
    return REQUEST.read_text(encoding="utf-8")


def test_the_review_request_embeds_the_exact_shipped_wording() -> None:
    blocks = re.findall(r"```text\n(.*?)\n```", request_text(), flags=re.DOTALL)
    assert blocks == [PROPOSED_GROUNDED_ANSWER.instructions]


def test_the_review_request_names_the_hash_of_the_shipped_wording() -> None:
    digest = hashlib.sha256(PROPOSED_GROUNDED_ANSWER.instructions.encode("utf-8")).hexdigest()
    assert f"`{digest}`" in request_text()
    assert f"`{PROPOSED_GROUNDED_ANSWER.template_id}`" in request_text()
    assert f"| Version | `{PROPOSED_GROUNDED_ANSWER.version}` |" in request_text()


def test_the_shipped_wording_is_exactly_the_approved_hash() -> None:
    digest = hashlib.sha256(PROPOSED_GROUNDED_ANSWER.instructions.encode("utf-8")).hexdigest()
    assert digest == APPROVED_SHA256, "wording changed: a new explicit owner approval is required"
    assert len(PROPOSED_GROUNDED_ANSWER.instructions) == 1552
    assert PROPOSED_GROUNDED_ANSWER.template_id == "grounded_answer"
    assert PROPOSED_GROUNDED_ANSWER.version == 1


def test_the_approved_template_is_the_only_production_template() -> None:
    assert PROPOSED_GROUNDED_ANSWER.test_only is False
    assert PRODUCTION_TEMPLATES == (PROPOSED_GROUNDED_ANSWER,)


def test_the_review_request_records_the_project_owner_approval_and_no_team_approval() -> None:
    text = request_text()
    assert "Status: APPROVED by the project owner on 2026-10-16." in text
    assert "No approval by `@legal-rag/ai-quality` or any other person or team is claimed" in text
    assert "_not yet recorded_" not in text and "No approval exists" not in text
    row = [ln for ln in text.splitlines() if ln.startswith("| Project owner")]
    assert len(row) == 1
    assert "2026-10-16" in row[0] and f"`{APPROVED_SHA256}`" in row[0] and "Approved" in row[0]
    # no line may attribute the approval to the named team
    for line in text.splitlines():
        if "approved by `@legal-rag/ai-quality`" in line.lower() or "ai-quality approved" in line:
            raise AssertionError(line)
