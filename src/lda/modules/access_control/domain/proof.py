"""AuthorizationProof: an HMAC-bound, 60-second statement of one authorization (ADR-026).

Token: `v1.<key_id>.<payload>.<mac>`; `payload` is canonical JSON in unpadded base64url and `mac`
is HMAC-SHA256 over a domain tag, the key id and the payload segment. The payload is parsed only
after the MAC verifies. The live-epoch check is NOT here: it needs the database (application).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import struct
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from lda.modules.access_control.domain.epoch import PolicyEpoch
from lda.modules.access_control.domain.errors import (
    InvalidProofClaims,
    InvalidScope,
    InvalidSigningKey,
)
from lda.modules.access_control.domain.resource import ActionId
from lda.modules.access_control.domain.scope import AccessScope
from lda.shared.kernel.errors import AuthorizationError, DomainError
from lda.shared.kernel.ids import PrincipalId, TenantId

PROOF_LIFETIME = timedelta(seconds=60)
"""Fixed by decision D7; deliberately not configurable."""

TOKEN_VERSION = "v1"  # noqa: S105 - a format version tag, not a credential
MAX_TOKEN_LENGTH = 2048
MIN_KEY_BYTES = 32
KEY_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")

_MAC_TAG = b"lda.authorization-proof.v1\x00"
_SET_TAG = b"lda.resource-set.v1\x00"
_B64URL = re.compile(r"[A-Za-z0-9_-]+")
_CLAIM_NAMES = frozenset(
    {"tenant", "principal", "action", "resources", "count", "epoch", "iat", "exp"}
)
_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True, repr=False)
class SigningKey:
    key_id: str
    secret: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.key_id, str) or KEY_ID_PATTERN.fullmatch(self.key_id) is None:
            raise InvalidSigningKey("signing_key_id_invalid")
        if not isinstance(self.secret, bytes) or len(self.secret) < MIN_KEY_BYTES:
            raise InvalidSigningKey("signing_key_too_short")

    def __repr__(self) -> str:  # the secret never leaves via repr
        return f"SigningKey(key_id={self.key_id!r})"


class SigningKeyring:
    """One active key in v1. Tokens carry the key id, so verify-only keys can be added later."""

    def __init__(self, active: SigningKey) -> None:
        self._keys = {active.key_id: active}
        self._active = active

    @property
    def active(self) -> SigningKey:
        return self._active

    def key_for(self, key_id: str) -> SigningKey | None:
        return self._keys.get(key_id)

    def __repr__(self) -> str:
        return f"SigningKeyring(active={self._active.key_id!r})"


def resources_digest(resources: Iterable[tuple[str, str]]) -> str:
    """SHA-256 over the canonical form of a resource set: unique, sorted, length-prefixed."""
    unique = sorted(set(resources))
    digest = hashlib.sha256(_SET_TAG)
    digest.update(struct.pack(">I", len(unique)))
    for kind, resource_id in unique:
        for part in (kind.encode("utf-8"), resource_id.encode("utf-8")):
            digest.update(struct.pack(">I", len(part)))
            digest.update(part)
    return digest.hexdigest()


def _utc(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


@dataclass(frozen=True, slots=True, repr=False)
class ProofClaims:
    tenant_id: TenantId
    principal_id: PrincipalId
    action: ActionId
    resources_digest: str
    resource_count: int
    policy_epoch: PolicyEpoch
    issued_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        for moment in (self.issued_at, self.expires_at):
            if moment.tzinfo is None or moment.utcoffset() is None:
                raise InvalidProofClaims
        lifetime = self.expires_at - self.issued_at
        if (
            _DIGEST.fullmatch(self.resources_digest) is None
            or type(self.resource_count) is not int
            or self.resource_count < 1
            or not timedelta(0) < lifetime <= PROOF_LIFETIME
        ):
            raise InvalidProofClaims

    def __repr__(self) -> str:
        return (
            f"ProofClaims(tenant_id={self.tenant_id}, principal_id={self.principal_id}, "
            f"action={self.action}, resources={self.resource_count}, "
            f"policy_epoch={self.policy_epoch.value})"
        )


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizationProof:
    """Only Access Control creates one (AG090); `token` is a bearer credential: never log it."""

    token: str
    claims: ProofClaims

    def __repr__(self) -> str:
        return f"AuthorizationProof(claims={self.claims!r})"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    """Strict: only the canonical unpadded encoding is accepted."""
    if _B64URL.fullmatch(text) is None:
        raise AuthorizationError("proof_malformed")
    try:
        data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except ValueError:
        raise AuthorizationError("proof_malformed") from None
    if _b64e(data) != text:
        raise AuthorizationError("proof_malformed")
    return data


def _mac(key: SigningKey, payload_segment: str) -> bytes:
    message = _MAC_TAG + key.key_id.encode("ascii") + b"\x00" + payload_segment.encode("ascii")
    return hmac.new(key.secret, message, hashlib.sha256).digest()


def issue_proof(
    scope: AccessScope, action: ActionId, now: datetime, keyring: SigningKeyring
) -> AuthorizationProof:
    """Sign `scope` for `action`. `issued_at` is truncated to whole seconds."""
    if not scope.resources:
        raise InvalidScope("scope_empty")
    issued = _utc(int(now.timestamp()))
    claims = ProofClaims(
        tenant_id=scope.tenant_id,
        principal_id=scope.principal_id,
        action=action,
        resources_digest=resources_digest((r.kind, r.id) for r in scope.resources),
        resource_count=len(scope.resources),
        policy_epoch=scope.policy_epoch,
        issued_at=issued,
        expires_at=issued + PROOF_LIFETIME,
    )
    payload = {
        "tenant": str(claims.tenant_id),
        "principal": str(claims.principal_id),
        "action": claims.action.value,
        "resources": claims.resources_digest,
        "count": claims.resource_count,
        "epoch": claims.policy_epoch.value,
        "iat": int(claims.issued_at.timestamp()),
        "exp": int(claims.expires_at.timestamp()),
    }
    segment = _b64e(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    key = keyring.active
    token = f"{TOKEN_VERSION}.{key.key_id}.{segment}.{_b64e(_mac(key, segment))}"
    return AuthorizationProof(token=token, claims=claims)


def open_token(token: str, keyring: SigningKeyring) -> ProofClaims:
    """Authenticate `token` and return its claims. Raises AuthorizationError(`proof_*`)."""
    if not isinstance(token, str) or len(token) > MAX_TOKEN_LENGTH or not token.isascii():
        raise AuthorizationError("proof_malformed")
    parts = token.split(".")
    if len(parts) != 4 or parts[0] != TOKEN_VERSION:
        raise AuthorizationError("proof_malformed")
    _, key_id, segment, mac_segment = parts
    if KEY_ID_PATTERN.fullmatch(key_id) is None:
        raise AuthorizationError("proof_malformed")
    key = keyring.key_for(key_id)
    if key is None:
        raise AuthorizationError("proof_unknown_key")
    presented = _b64d(mac_segment)
    if not hmac.compare_digest(presented, _mac(key, segment)):
        raise AuthorizationError("proof_signature_invalid")
    return _claims_of(_b64d(segment))  # parsed only after authentication


def _claims_of(raw: bytes) -> ProofClaims:
    try:
        payload: object = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != _CLAIM_NAMES:
            raise ValueError
        ints = [payload[name] for name in ("count", "epoch", "iat", "exp")]
        texts = [payload[name] for name in ("tenant", "principal", "action", "resources")]
        if not all(type(v) is int for v in ints) or not all(isinstance(v, str) for v in texts):
            raise ValueError
        tenant, principal = UUID(texts[0]), UUID(texts[1])
        if str(tenant) != texts[0] or str(principal) != texts[1]:
            raise ValueError
        return ProofClaims(
            tenant_id=TenantId(tenant),
            principal_id=PrincipalId(principal),
            action=ActionId(texts[2]),
            resources_digest=texts[3],
            resource_count=ints[0],
            policy_epoch=PolicyEpoch(ints[1]),
            issued_at=_utc(ints[2]),
            expires_at=_utc(ints[3]),
        )
    except (ValueError, TypeError, OverflowError, OSError, DomainError):
        raise AuthorizationError("proof_malformed") from None
