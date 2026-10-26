"""Every Identity use case consumes ADR-034 correctly (metadata only; nothing enforced here)."""

from __future__ import annotations

import pytest

from lda.modules.identity.api import (
    DisablePrincipal,
    EnablePrincipal,
    GetPrincipal,
    RegisterPrincipal,
)
from lda.shared.kernel.declarations import (
    Audited,
    NotAudited,
    Public,
    Requires,
    audit_of,
    authorization_of,
)

EXPECTED = {
    RegisterPrincipal: (
        Requires(("identity.register_principal",)),
        Audited("identity.principal_registered"),
    ),
    DisablePrincipal: (
        Requires(("identity.disable_principal",)),
        Audited("identity.principal_disabled"),
    ),
    EnablePrincipal: (
        Requires(("identity.enable_principal",)),
        Audited("identity.principal_enabled"),
    ),
}


@pytest.mark.parametrize("use_case", list(EXPECTED))
def test_commands_declare_requirements_and_audit_action(use_case: type) -> None:
    assert (authorization_of(use_case), audit_of(use_case)) == EXPECTED[use_case]


def test_query_is_an_explicit_public_exemption_with_reasons() -> None:
    authorization, audit = authorization_of(GetPrincipal), audit_of(GetPrincipal)
    assert isinstance(authorization, Public) and authorization.reason
    assert isinstance(audit, NotAudited) and audit.reason


def test_markers_do_not_change_behaviour_or_wrap_execute() -> None:
    for use_case in (RegisterPrincipal, DisablePrincipal, EnablePrincipal, GetPrincipal):
        execute = use_case.execute
        assert not hasattr(execute, "__wrapped__")
        assert execute.__qualname__ == f"{use_case.__name__}.execute"
