"""Independent limits must fail closed, and the release decision must hold on every path (ADR-029).

Part 1. `max_sources` (policy), the renderer's `max_parts` and the gateway's `max_segments` are
three separate settings. When they disagree the request must fail where the smaller limit bites,
before any text reaches the provider and without a recorded generation, never with a truncated
context or a partial answer. Part 2. The last authorization decision gates the release of
statements: denial, an Access Control outage and a revocation of a cited document all withhold the
answer, while a revocation of a source that was supplied but not cited does not.
"""

from __future__ import annotations

import traceback
from dataclasses import replace
from uuid import UUID

import pytest

from lda.modules.ai_gateway.api import LIMIT_EXCEEDED as GATEWAY_LIMIT_EXCEEDED
from lda.modules.citations.api import GenerationOutcome
from lda.modules.grounded_generation.domain import codes
from lda.modules.grounded_generation.tests.application.test_generate_grounded_answer import (
    committed_records,
    good_reply,
)
from lda.modules.grounded_generation.tests.support import PASSAGES, QUESTION, Rig, ctx
from lda.modules.prompts.testing import default_limits
from lda.shared.kernel.errors import (
    AuthorizationError,
    InfrastructureError,
    LdaError,
    ValidationError,
)

# ---- part 1: limits that disagree -----------------------------------------------------------


def many_sources(n: int) -> Rig:
    texts = tuple(
        f"Clause {i}: the parties agree to term number {i} of the contract." for i in range(n)
    )
    return Rig(passages=texts)


def test_the_renderer_limit_bites_before_the_provider_authorization_and_send() -> None:
    rig = Rig(renderer_limits=replace(default_limits(), max_parts=2))  # 3 sources
    rig.provider.replies.append(good_reply())
    with pytest.raises(ValidationError) as caught:
        rig.use_case.execute(ctx(), rig.command())
    assert caught.value.code == codes.LIMIT_EXCEEDED
    assert len(rig.access.calls) == 1  # evidence only: not even the pre-egress decision was taken
    assert rig.provider.requests == []
    assert committed_records(rig) == [] and rig.units.audit == []


def test_the_gateway_limit_bites_before_the_provider_and_nothing_is_recorded() -> None:
    rig = Rig(max_segments=2)  # 3 sources fit `max_sources` and the renderer, not the gateway
    rig.provider.replies.append(good_reply())
    with pytest.raises(ValidationError) as caught:
        rig.use_case.execute(ctx(), rig.command())
    assert caught.value.code == GATEWAY_LIMIT_EXCEEDED  # the gateway's own code, not ours
    assert rig.provider.requests == []  # no text left the process
    assert len(rig.access.calls) == 2  # evidence and egress decisions; never a release decision
    assert committed_records(rig) == [] and rig.units.audit == []


@pytest.mark.parametrize("sources", [1, 2, 3])
def test_a_limit_exactly_at_the_number_of_sources_is_accepted(sources: int) -> None:
    rig = many_sources(sources)
    rig = Rig(
        passages=rig.passages,
        renderer_limits=replace(default_limits(), max_parts=sources),
        max_segments=sources,
    )
    rig.provider.replies.append(
        '{"outcome":"answered","statements":'
        '[{"text":"t","citation":"S1","quote":"the parties agree to term"}]}'
    )
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.outcome is GenerationOutcome.ANSWERED
    assert len(rig.provider.requests) == 1


def test_the_policy_limit_is_enforced_before_anything_else_and_at_its_boundary() -> None:
    at_limit = many_sources(10)  # max_sources is 10
    at_limit.provider.replies.append('{"outcome":"insufficient_evidence","statements":[]}')
    assert at_limit.use_case.execute(ctx(), at_limit.command()).statements == ()
    over = many_sources(11)
    with pytest.raises(ValidationError):
        over.use_case.execute(ctx(), over.command())
    assert over.events == [] and over.provider.requests == []  # refused before authorization


# ---- part 2: the release decision on every path ---------------------------------------------


def revoke_after_generation(rig: Rig, *, allowed: set[UUID]) -> None:
    rig.provider.replies.append(good_reply())
    rig.provider.on_generate = lambda: setattr(rig.access, "allowed", allowed)


def doc_ids(rig: Rig) -> list[UUID]:
    return [d.value for d, _, _ in rig.docs]


def test_revoking_a_cited_document_withholds_the_whole_answer() -> None:
    rig = Rig()
    first, _second, third = doc_ids(rig)  # good_reply cites the first two
    revoke_after_generation(rig, allowed={first, third})  # `second` is cited by S2
    with pytest.raises(AuthorizationError):
        rig.use_case.execute(ctx(), rig.command())
    assert len(committed_records(rig)) == 1 and len(rig.units.audit) == 1  # it did generate


def test_revoking_only_an_uncited_source_does_not_block_the_release() -> None:
    rig = Rig()
    first, second, _third = doc_ids(rig)
    revoke_after_generation(rig, allowed={first, second})  # the uncited third is gone
    answer = rig.use_case.execute(ctx(), rig.command())
    assert answer.outcome is GenerationOutcome.ANSWERED and len(answer.statements) == 3
    assert rig.access.calls[-1] == {first, second}  # the release decision covers cited ones only


def test_an_access_control_outage_at_release_withholds_the_answer() -> None:
    rig = Rig()
    rig.provider.replies.append(good_reply())
    rig.access.error_at_call[3] = InfrastructureError("access_control_unavailable")
    with pytest.raises(InfrastructureError):
        rig.use_case.execute(ctx(), rig.command())
    assert len(committed_records(rig)) == 1  # generated and provable, but never shown


@pytest.mark.parametrize("failure", ["denied", "outage"])
def test_a_withheld_answer_leaks_no_statement_through_the_error_or_the_audit(failure: str) -> None:
    rig = Rig()
    rig.provider.replies.append(good_reply())
    if failure == "denied":
        rig.access.deny_from_call = 3
    else:
        rig.access.error_at_call[3] = InfrastructureError("access_control_unavailable")
    with pytest.raises(LdaError) as caught:
        rig.use_case.execute(ctx(), rig.command())
    rendered = "".join(traceback.format_exception(caught.value))
    leaked = ("ninety days", "thirty days", "invoice", QUESTION, *PASSAGES)
    assert not [s for s in leaked if s in rendered]
    audit = repr([(f.action, f.attributes) for f in rig.units.audit])
    assert not [s for s in leaked if s in audit]
    (record,) = committed_records(rig)
    assert "ninety" not in repr(record) and "thirty" not in repr(record)


def test_a_failed_release_check_is_not_retried_into_success() -> None:
    rig = Rig()
    rig.provider.replies.append(good_reply())
    rig.access.deny_from_call = 3
    with pytest.raises(AuthorizationError):
        rig.use_case.execute(ctx(), rig.command())
    assert len(rig.access.calls) == 3 and len(rig.provider.requests) == 1
