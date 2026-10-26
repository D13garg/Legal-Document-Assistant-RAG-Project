"""Every Access Control use case consumes ADR-034 correctly."""

from __future__ import annotations

import pytest

from lda.modules.access_control.api import (
    AuthorizeAction,
    GrantAccess,
    ResolveAccessScope,
    RevokeAccess,
)
from lda.shared.kernel.declarations import (
    Audited,
    NotAudited,
    Requires,
    audit_of,
    authorization_of,
)

COMMANDS = {
    GrantAccess: (
        Requires(("access_control.grant_access",)),
        Audited("access_control.access_granted"),
    ),
    RevokeAccess: (
        Requires(("access_control.revoke_access",)),
        Audited("access_control.access_revoked"),
    ),
}


@pytest.mark.parametrize("use_case", list(COMMANDS))
def test_commands_require_an_action_and_declare_their_audit_label(use_case: type) -> None:
    assert (authorization_of(use_case), audit_of(use_case)) == COMMANDS[use_case]


@pytest.mark.parametrize("query", [AuthorizeAction, ResolveAccessScope])
def test_decision_queries_are_public_with_a_reason_and_not_audited_yet(query: type) -> None:
    from lda.shared.kernel.declarations import Public

    authorization, audit = authorization_of(query), audit_of(query)
    assert isinstance(authorization, Public) and authorization.reason
    assert isinstance(audit, NotAudited) and "ADR-019" in audit.reason


def test_every_declared_requirement_is_a_valid_action_identifier() -> None:
    from lda.modules.access_control.api import ActionId

    for use_case in COMMANDS:
        declared = authorization_of(use_case)
        assert isinstance(declared, Requires)
        for requirement in declared.requirements:
            ActionId(requirement)  # raises if it is not in the action vocabulary
