"""D5/D6 in one place: for every way a generation can fail, what is stored and what is raised.

This table is the executable source of ADR-029's failure matrix. The question each row answers:
(1) was anything RELEASED (statements)? never, for any row; (2) did a generation record and an audit
fact get written? Rows differ on (2) on purpose: a generation that never produced an output leaves
no record; one that produced an output the contract refuses is recorded as `REJECTED`; one that
produced a valid output but was not released (revocation at the last check) is recorded too.
No row stores or exposes a reply.
"""

from __future__ import annotations

import pytest

from lda.modules.ai_gateway.api import FailureKind, ProviderFailure
from lda.modules.citations.api import GenerationOutcome
from lda.modules.grounded_generation.domain import codes
from lda.modules.grounded_generation.tests.application.test_generate_grounded_answer import (
    committed_records,
    good_reply,
)
from lda.modules.grounded_generation.tests.support import Rig, ctx, reply
from lda.shared.kernel.errors import (
    AuthorizationError,
    InfrastructureError,
    NotFoundError,
)


def counts(rig: Rig) -> tuple[int, int]:
    return len(committed_records(rig)), len(rig.units.audit)


# ---- nothing recorded: no output exists, or the answer could not be persisted ---------------


def test_no_sources_is_insufficient_evidence_with_no_generation_and_no_record() -> None:
    rig = Rig()
    answer = rig.use_case.execute(ctx(), rig.command(claims=()))
    assert answer.outcome is GenerationOutcome.INSUFFICIENT_EVIDENCE
    assert answer.statements == () and answer.record_id is None
    assert counts(rig) == (0, 0) and rig.provider.requests == []  # nothing was generated


@pytest.mark.parametrize("kind", list(FailureKind))
def test_a_provider_failure_raises_and_records_nothing(kind: FailureKind) -> None:
    rig = Rig()
    rig.provider.failures.extend([kind] * 3)
    with pytest.raises(ProviderFailure):
        rig.use_case.execute(ctx(), rig.command())
    assert counts(rig) == (0, 0)


def test_an_adapter_bug_raises_and_records_nothing() -> None:
    rig = Rig()
    rig.provider.crash = RuntimeError("adapter bug")
    with pytest.raises(RuntimeError):
        rig.use_case.execute(ctx(), rig.command())
    assert counts(rig) == (0, 0)


def test_a_valid_answer_that_cannot_be_persisted_is_never_returned() -> None:
    rig = Rig()
    rig.units.fail_commit = True
    rig.provider.replies.append(good_reply())
    with pytest.raises(InfrastructureError):
        rig.use_case.execute(ctx(), rig.command())
    assert counts(rig) == (0, 0)  # the transaction rolled back as a unit


def test_evidence_authorization_denied_records_nothing_and_sends_nothing() -> None:
    rig = Rig()
    rig.access.deny_from_call = 1
    with pytest.raises(AuthorizationError):
        rig.use_case.execute(ctx(), rig.command())
    assert counts(rig) == (0, 0) and rig.provider.requests == []


def test_an_unavailable_source_records_nothing_and_sends_nothing() -> None:
    rig = Rig()
    claims = tuple(rig.claim(i) for i in range(3))
    rig.store.versions.clear()  # the chunks are gone
    with pytest.raises(NotFoundError):
        rig.use_case.execute(ctx(), rig.command(claims=claims))
    assert counts(rig) == (0, 0) and rig.provider.requests == []


# ---- recorded as REJECTED: the provider answered, the contract refused it ---------------------

REFUSED = {
    "empty reply": ("", codes.OUTPUT_MALFORMED),
    "whitespace reply": ("   \n", codes.OUTPUT_MALFORMED),
    "not json": ("free text", codes.OUTPUT_MALFORMED),
    "fenced json": ("```json\n{}\n```", codes.OUTPUT_MALFORMED),
    "extra field": ('{"outcome":"answered","statements":[],"x":1}', codes.OUTPUT_SCHEMA),
    "unsupported quote": (
        reply(("t", "S1", "this quote is nowhere in the source")),
        codes.OUTPUT_QUOTE_UNSUPPORTED,
    ),
    "unknown marker": (
        reply(("t", "S9", "shall give ninety days written notice")),
        codes.OUTPUT_CITATION_INVALID,
    ),
    "unsafe character": (
        reply(("a\u200bb", "S1", "shall give ninety days written notice")),
        codes.OUTPUT_UNSAFE_TEXT,
    ),
}


@pytest.mark.parametrize(("raw", "code"), REFUSED.values(), ids=REFUSED.keys())
def test_a_reply_the_contract_refuses_is_recorded_and_audited_but_never_released(
    raw: str, code: str
) -> None:
    rig = Rig()
    rig.provider.replies.append(raw)
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.outcome is GenerationOutcome.REJECTED and answer.rejection == code
    assert answer.statements == () and answer.record_id is not None
    assert counts(rig) == (1, 1)
    (record,) = rig.units.committed_records()
    assert record.outcome is GenerationOutcome.REJECTED and record.citations == ()
    (fact,) = rig.units.audit
    assert fact.attributes["rejection"] == code
    assert raw.strip() == "" or raw not in repr(record) + repr(fact) + repr(answer)


# ---- recorded, not released: an output exists but was not (or need not be) shown --------------


def test_the_model_declining_is_a_recorded_insufficient_evidence_without_statements() -> None:
    rig = Rig()
    rig.provider.replies.append(reply(outcome="insufficient_evidence"))
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.outcome is GenerationOutcome.INSUFFICIENT_EVIDENCE
    assert answer.statements == () and answer.rejection is None
    assert counts(rig) == (1, 1)  # a generation happened and it is auditable


def test_a_revocation_at_the_release_check_keeps_the_record_and_withholds_the_answer() -> None:
    rig = Rig()
    rig.provider.replies.append(good_reply())
    rig.access.deny_from_call = 3
    with pytest.raises(AuthorizationError):
        rig.use_case.execute(ctx(), rig.command())
    assert counts(rig) == (1, 1)  # it was generated and is provable; it was never shown
