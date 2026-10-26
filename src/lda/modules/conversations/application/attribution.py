from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lda.modules.conversations.domain import codes
from lda.shared.kernel.context import ExecutionContext, TenantContext
from lda.shared.kernel.errors import AuthorizationError
from lda.shared.kernel.ids import PrincipalId, RequestId


@dataclass(frozen=True, slots=True)
class Attribution:
    """Who acted, taken from the execution context only (ADR-019 D1)."""

    actor: PrincipalId | None
    request_id: RequestId | None
    attributes: Mapping[str, str]


def attribution_of(ctx: ExecutionContext) -> Attribution:
    if isinstance(ctx, TenantContext):
        return Attribution(ctx.principal_id, ctx.request_id, {})
    return Attribution(None, None, {"purpose": ctx.purpose})


def require_principal(ctx: ExecutionContext) -> TenantContext:
    """Conversations belong to a principal: system work has none and is refused (ADR-039 C4).

    Live principal status is not checked here: the module has no identity dependency. The API
    boundary that authenticates the caller owns that check.
    """
    if not isinstance(ctx, TenantContext):
        raise AuthorizationError(codes.REQUIRES_PRINCIPAL)
    return ctx
