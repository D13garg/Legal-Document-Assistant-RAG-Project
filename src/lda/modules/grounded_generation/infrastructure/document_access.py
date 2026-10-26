"""`DocumentAccess` over Access Control's `AuthorizeAction` (ADR-029).

Strict by design: ALLOW only when the caller holds `documents.read_document` on EVERY requested
document (the decision point's own rule); a partial grant, an unknown principal, an unregistered
action and a tenant mismatch are all one `grounded_not_authorized`, with no reason and no resource
in the error. The returned proof is wrapped `Sensitive`: it is a bearer credential.
"""

from __future__ import annotations

from lda.modules.access_control.api import (
    ActionId,
    AuthorizationRequest,
    AuthorizeAction,
    ResourceRef,
)
from lda.modules.citations.api import CitedDocumentId
from lda.modules.documents.api import DOCUMENT_KIND, READ_DOCUMENT
from lda.modules.grounded_generation.domain import codes
from lda.shared.kernel.context import TenantContext
from lda.shared.kernel.errors import AuthorizationError
from lda.shared.kernel.sensitive import Sensitive


class AuthorizeActionDocumentAccess:
    def __init__(self, authorize: AuthorizeAction) -> None:
        self._authorize = authorize

    def authorize_read(
        self, ctx: TenantContext, documents: frozenset[CitedDocumentId]
    ) -> Sensitive[str]:
        request = AuthorizationRequest(
            ActionId(READ_DOCUMENT),
            frozenset(ResourceRef(DOCUMENT_KIND, str(d.value)) for d in documents),
        )
        decision = self._authorize.execute(ctx, request)
        if not decision.allowed or decision.proof is None:
            raise AuthorizationError(codes.NOT_AUTHORIZED)
        return Sensitive(decision.proof.token)
