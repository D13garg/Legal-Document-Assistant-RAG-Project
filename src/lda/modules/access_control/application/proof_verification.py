"""The concrete ProofVerifier: integrity, binding, expiry, the LIVE policy epoch and the LIVE
principal status (ADR-026 §7)."""

from __future__ import annotations

from lda.modules.access_control.application.ports import AccessControlUnitOfWorkFactory
from lda.modules.access_control.domain.proof import SigningKeyring, open_token, resources_digest
from lda.modules.identity.api import GetPrincipal, PrincipalReference
from lda.shared.kernel.context import SystemContext
from lda.shared.kernel.errors import AuthorizationError, NotFoundError
from lda.shared.ports.authorization import ProofExpectation
from lda.shared.ports.clock import Clock


class EpochCheckedProofVerifier:
    """Implements `lda.shared.ports.authorization.ProofVerifier`.

    Injected into `ai_gateway` by bootstrap; `ai_gateway` never imports Access Control.
    """

    def __init__(
        self,
        keyring: SigningKeyring,
        units_of_work: AccessControlUnitOfWorkFactory,
        get_principal: GetPrincipal,
        clock: Clock,
    ) -> None:
        self._keyring = keyring
        self._units_of_work = units_of_work
        self._get_principal = get_principal
        self._clock = clock

    def verify(self, token: str, expected: ProofExpectation) -> None:
        claims = open_token(token, self._keyring)  # shape, key id, HMAC
        pairs = [(r.kind, r.id) for r in expected.resources]
        if (
            claims.tenant_id != expected.tenant_id
            or claims.principal_id != expected.principal_id
            or claims.action.value != expected.action
            or claims.resource_count != len(expected.resources)
            or claims.resources_digest != resources_digest(pairs)
        ):
            raise AuthorizationError("proof_mismatch")
        if self._clock.now() >= claims.expires_at:
            raise AuthorizationError("proof_expired")
        # Live epoch, read now in a fresh transaction scoped to the expected tenant. A database
        # failure propagates: no verification result means no egress (fail closed).
        context = SystemContext(expected.tenant_id, "authorization_proof_verification")
        with self._units_of_work.begin(context) as uow:
            live = uow.policy_epochs.current()
        if live != claims.policy_epoch:
            raise AuthorizationError("proof_stale_epoch")
        # Live principal status through the existing Identity boundary, never cached and never
        # copied here. Absent, other-tenant (looks absent) or disabled: no egress. Any other
        # failure (for example the database) propagates: fail closed.
        try:
            principal = self._get_principal.execute(
                context, PrincipalReference(claims.principal_id)
            )
        except NotFoundError:
            raise AuthorizationError("proof_principal_inactive") from None
        if not principal.is_active:
            raise AuthorizationError("proof_principal_inactive")
