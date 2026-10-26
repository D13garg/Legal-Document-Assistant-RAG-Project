from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from lda.shared.kernel.context import ExecutionContext, TenantContext
from lda.shared.kernel.ids import PrincipalId, RequestId


@dataclass(frozen=True, slots=True)
class Attribution:
    """Who acted, taken from the execution context only (ADR-019 D1)."""

    actor: PrincipalId | None
    request_id: RequestId | None
    attributes: Mapping[str, str]


def attribution_of(ctx: ExecutionContext) -> Attribution:
    """A principal for user work; no actor and `purpose` for system work (ADR-019 D6)."""
    if isinstance(ctx, TenantContext):
        return Attribution(ctx.principal_id, ctx.request_id, {})
    return Attribution(None, None, {"purpose": ctx.purpose})
