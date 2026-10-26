"""Everything that must stop BEFORE any write, search or provider call (ADR-040 D2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

import pytest

from lda.modules.access_control.api import (
    AccessScope,
    AuthorizationRequest,
    DenyReason,
    ResourceRef,
)
from lda.modules.access_control.testing import an_access_scope
from lda.modules.answering.api import (
    INVALID_CANDIDATES,
    INVALID_COMMAND,
    INVALID_QUESTION,
    NOT_AUTHORIZED,
    REQUIRES_PRINCIPAL,
    Answer,
    AnswerCommand,
)
from lda.modules.answering.tests.support import QUESTION, Scopes, build, command, ctx
from lda.modules.conversations.api import ClientMessageId, ConversationId
from lda.shared.kernel.context import ExecutionContext, SystemContext, TenantContext
from lda.shared.kernel.errors import (
    AuthorizationError,
    InfrastructureError,
    LdaError,
    ValidationError,
)
from lda.shared.kernel.ids import PrincipalId, TenantId


def refused(use_case: Answer, context: ExecutionContext, cmd: object) -> LdaError:
    with pytest.raises(LdaError) as caught:
        use_case.execute(context, cmd)  # type: ignore[arg-type]  # malformed input on purpose
    return caught.value


def test_system_work_is_refused_before_anything_else() -> None:
    use_case, scopes, recorder = build()
    error = refused(use_case, SystemContext(ctx().tenant_id, "job"), command())
    assert isinstance(error, AuthorizationError) and error.code == REQUIRES_PRINCIPAL
    assert scopes.requests == [] and recorder.untouched()


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "not a command",
        AnswerCommand("x", ClientMessageId(uuid4()), QUESTION, (uuid4(),)),  # type: ignore[arg-type]
        AnswerCommand(ConversationId(uuid4()), "x", QUESTION, (uuid4(),)),  # type: ignore[arg-type]
    ],
)
def test_a_malformed_command_is_refused_with_no_effect(bad: object) -> None:
    use_case, scopes, recorder = build()
    error = refused(use_case, ctx(), bad)
    assert isinstance(error, ValidationError) and error.code == INVALID_COMMAND
    assert scopes.requests == [] and recorder.untouched()


@pytest.mark.parametrize("question", ["", "   \n\t", "x" * 61, "has\x00nul", None, 5])
def test_an_invalid_question_is_refused_with_no_effect(question: object) -> None:
    use_case, scopes, recorder = build()
    error = refused(use_case, ctx(), command(question=question))  # type: ignore[arg-type]
    assert isinstance(error, ValidationError) and error.code == INVALID_QUESTION
    assert scopes.requests == [] and recorder.untouched()


def test_a_question_at_the_limit_is_not_refused_for_its_length() -> None:
    use_case, scopes, _ = build()
    error = refused(use_case, ctx(), command(question="x" * 60))
    assert error.code == NOT_AUTHORIZED  # it got as far as the authorization decision
    assert len(scopes.requests) == 1


@pytest.mark.parametrize(
    "candidates",
    [(), [uuid4()], (uuid4(),) * 2, tuple(uuid4() for _ in range(4)), ("x",)],
)
def test_an_invalid_candidate_set_is_refused_with_no_effect(candidates: object) -> None:
    use_case, scopes, recorder = build()
    error = refused(use_case, ctx(), command(candidates=candidates))  # type: ignore[arg-type]
    assert isinstance(error, ValidationError) and error.code == INVALID_CANDIDATES
    assert scopes.requests == [] and recorder.untouched()


@pytest.mark.parametrize("reason", list(DenyReason))
def test_every_denial_is_one_indistinguishable_error_and_writes_nothing(
    reason: DenyReason,
) -> None:
    use_case, _, recorder = build(Scopes(reason))
    cmd = command()
    error = refused(use_case, ctx(), cmd)
    assert isinstance(error, AuthorizationError) and error.code == NOT_AUTHORIZED
    assert reason.value not in str(error) + error.detail  # no reason, no resource, no id
    assert str(cmd.candidate_document_ids[0]) not in str(error) + error.detail
    assert recorder.untouched()


def test_the_decision_is_asked_for_exactly_the_named_candidates_and_the_read_action() -> None:
    use_case, scopes, _ = build()
    ids = tuple(uuid4() for _ in range(3))
    refused(use_case, ctx(), command(candidates=ids))
    (request,) = scopes.requests
    assert str(request.action) == "documents.read_document"
    assert {(r.kind, r.id) for r in request.resources} == {("document", str(i)) for i in ids}


def test_an_access_control_outage_propagates_and_writes_nothing() -> None:
    use_case, _, recorder = build(Scopes(error=InfrastructureError("access_control_unavailable")))
    error = refused(use_case, ctx(), command())
    assert isinstance(error, InfrastructureError) and error.code == "access_control_unavailable"
    assert recorder.untouched()


def test_the_command_and_errors_never_show_the_question() -> None:
    use_case, _, _ = build()
    cmd = command()
    error = refused(use_case, ctx(), cmd)
    assert "SECRET-QUESTION-9917" not in repr(cmd) + str(error) + error.detail + repr(error)
    assert str(cmd.candidate_document_ids[0]) not in repr(cmd)


# ---- defense in depth: a decision that does not match the caller -------------------------------


@dataclass
class _Proof:
    token: str = field(default_factory=lambda: uuid4().hex)


@dataclass
class _Decision:
    """Decision-shaped stub: `Answer` reads only `allowed`, `scope` and `proof.token`. Real
    decisions cannot be mismatched like this (see tests/platform for the real-decision cases);
    this isolates each guard so none can be masked by another."""

    allowed: bool
    scope: AccessScope | None
    proof: _Proof | None


Make = Callable[[TenantContext, AuthorizationRequest], _Decision]


class _Stub:
    def __init__(self, make: Make) -> None:
        self.make = make

    def execute(self, ctx: TenantContext, request: AuthorizationRequest) -> _Decision:
        return self.make(ctx, request)


def _scope(
    ctx: TenantContext,
    request: AuthorizationRequest,
    *,
    tenant_id: TenantId | None = None,
    principal_id: PrincipalId | None = None,
    resources: frozenset[ResourceRef] | None = None,
) -> AccessScope:
    return an_access_scope(
        tenant_id=tenant_id or ctx.tenant_id,
        principal_id=principal_id or ctx.principal_id,
        resources=request.resources if resources is None else resources,
    )


MISMATCHES: dict[str, Make] = {
    "other-tenant": lambda c, r: _Decision(
        True, _scope(c, r, tenant_id=TenantId(uuid4())), _Proof()
    ),
    "other-principal": lambda c, r: _Decision(
        True, _scope(c, r, principal_id=PrincipalId(uuid4())), _Proof()
    ),
    "empty-scope": lambda c, r: _Decision(True, _scope(c, r, resources=frozenset()), _Proof()),
    "wider-scope": lambda c, r: _Decision(
        True,
        _scope(c, r, resources=r.resources | {ResourceRef("document", str(uuid4()))}),
        _Proof(),
    ),
    "no-scope": lambda c, r: _Decision(True, None, _Proof()),
    "no-proof": lambda c, r: _Decision(True, _scope(c, r), None),
    "not-allowed": lambda c, r: _Decision(False, _scope(c, r), _Proof()),
}


@pytest.mark.parametrize("name", list(MISMATCHES))
def test_each_guard_on_the_decision_refuses_on_its_own_and_writes_nothing(name: str) -> None:
    use_case, _, recorder = build()
    use_case._scopes = _Stub(MISMATCHES[name])  # type: ignore[assignment]  # noqa: SLF001
    error = refused(use_case, ctx(), command())
    assert isinstance(error, AuthorizationError) and error.code == NOT_AUTHORIZED
    assert recorder.untouched()
