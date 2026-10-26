"""`ClaimReference` / `claims_from_references`: the neutral claim input (ADR-040 Q1).

Strictly additive to the frozen Step 14 API. A reference is validated at construction and converted
inside this module; it is never proof of authorization, so the same provenance and proof checks
still apply downstream (the last two tests drive the real use case).
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any
from uuid import uuid4

import pytest

from lda.modules.citations.api import GenerationOutcome, SourceClaim
from lda.modules.grounded_generation import api
from lda.modules.grounded_generation.api import (
    INVALID_INPUT,
    ClaimReference,
    GenerateGroundedAnswerCommand,
    claims_from_references,
)
from lda.modules.grounded_generation.tests.support import CONFIG_ID, QUESTION, Rig, ctx, reply
from lda.shared.kernel.errors import ValidationError
from lda.shared.kernel.sensitive import Sensitive

HASH = "a" * 64


def reference(**overrides: Any) -> ClaimReference:
    values: dict[str, Any] = {
        "document_id": uuid4(),
        "version_id": uuid4(),
        "chunk_id": uuid4(),
        "ordinal": 0,
        "text_hash": HASH,
    }
    return ClaimReference(**{**values, **overrides})


def test_the_public_surface_gained_exactly_the_neutral_type_and_its_converter() -> None:
    assert {"ClaimReference", "claims_from_references"} <= set(api.__all__)
    assert [f.name for f in fields(ClaimReference)] == [
        "document_id",
        "version_id",
        "chunk_id",
        "ordinal",
        "text_hash",
    ]


def test_a_reference_converts_to_the_equal_source_claim() -> None:
    ref = reference(ordinal=7)
    (claim,) = claims_from_references((ref,))
    assert isinstance(claim, SourceClaim)
    assert (claim.document_id.value, claim.version_id.value) == (ref.document_id, ref.version_id)
    assert (claim.chunk_id, claim.ordinal, claim.text_hash) == (ref.chunk_id, 7, HASH)


def test_order_is_kept_and_an_empty_tuple_stays_empty() -> None:
    refs = tuple(reference(ordinal=i) for i in range(4))
    assert [c.ordinal for c in claims_from_references(refs)] == [0, 1, 2, 3]
    assert claims_from_references(()) == ()


@pytest.mark.parametrize(
    "overrides",
    [
        {"document_id": str(uuid4())},
        {"version_id": None},
        {"chunk_id": uuid4().int},
        {"ordinal": -1},
        {"ordinal": 1_000_001},
        {"ordinal": True},
        {"ordinal": 1.0},
        {"text_hash": "A" * 64},
        {"text_hash": "a" * 63},
        {"text_hash": "g" * 64},
        {"text_hash": None},
    ],
)
def test_an_invalid_field_is_refused_at_construction(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        reference(**overrides)
    assert caught.value.code == INVALID_INPUT


def test_the_boundary_ordinals_are_accepted() -> None:
    assert reference(ordinal=0).ordinal == 0 and reference(ordinal=1_000_000).ordinal == 1_000_000


@pytest.mark.parametrize("bad", [[reference()], (reference(), "x"), None, (object(),)])
def test_a_non_tuple_or_foreign_element_is_refused(bad: Any) -> None:
    with pytest.raises(ValidationError) as caught:
        claims_from_references(bad)
    assert caught.value.code == INVALID_INPUT


def test_an_error_never_carries_a_value() -> None:
    marker = "SECRET-HASH-VALUE"
    with pytest.raises(ValidationError) as caught:
        reference(text_hash=marker)
    assert marker not in str(caught.value) + caught.value.detail + repr(caught.value)


def references_for(rig: Rig) -> tuple[ClaimReference, ...]:
    return tuple(
        ClaimReference(d.value, v.value, c.chunk_id, c.ordinal, c.text_hash) for d, v, c in rig.docs
    )


def test_converted_references_drive_the_use_case_like_hand_built_claims() -> None:
    """Regression: the same evidence yields the same validated, recorded answer both ways."""
    quote = "shall give ninety days written notice"
    outcomes = []
    for build in (lambda rig: rig.claims, lambda rig: claims_from_references(references_for(rig))):
        rig = Rig()
        rig.provider.replies.append(reply(("The notice is ninety days.", "S1", quote)))
        answer = rig.use_case.execute(
            ctx(), GenerateGroundedAnswerCommand(Sensitive(QUESTION), build(rig), CONFIG_ID)
        )
        assert answer.outcome is GenerationOutcome.ANSWERED
        assert answer.statements[0].citation.source.chunk_id == rig.docs[0][2].chunk_id
        outcomes.append(
            (answer.outcome, len(answer.statements), len(rig.units.committed_records()))
        )
    assert outcomes[0] == outcomes[1]


def test_a_forged_reference_is_still_refused_by_provenance_checks() -> None:
    """A reference is untrusted: a wrong hash passes the shape check and fails the chunk store."""
    rig = Rig()
    rig.provider.replies.append(reply(("x", "S1", "shall give ninety days written notice")))
    forged = (
        ClaimReference(
            rig.docs[0][0].value, rig.docs[0][1].value, rig.docs[0][2].chunk_id, 0, "0" * 64
        ),
    )
    with pytest.raises(ValidationError) as caught:
        rig.use_case.execute(
            ctx(),
            GenerateGroundedAnswerCommand(
                Sensitive(QUESTION), claims_from_references(forged), CONFIG_ID
            ),
        )
    assert caught.value.code == "citations_provenance_mismatch"
    assert rig.provider.requests == [] and rig.units.committed_records() == []
