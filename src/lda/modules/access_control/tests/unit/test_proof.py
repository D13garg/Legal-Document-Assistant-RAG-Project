"""Proof integrity: HMAC, canonical encoding, binding digest, lifetime, redaction."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from lda.modules.access_control.api import (
    PROOF_LIFETIME,
    ActionId,
    ResourceRef,
    SigningKey,
    SigningKeyring,
)
from lda.modules.access_control.domain.errors import InvalidScope, InvalidSigningKey
from lda.modules.access_control.domain.proof import (
    MAX_TOKEN_LENGTH,
    issue_proof,
    open_token,
    resources_digest,
)
from lda.modules.access_control.testing import a_signing_keyring, an_access_scope
from lda.shared.kernel.errors import AuthorizationError
from lda.shared.kernel.ids import PrincipalId, TenantId

NOW = datetime(2026, 1, 1, 12, 0, 0, 750_000, tzinfo=UTC)
TENANT = TenantId(UUID(int=1))
PRINCIPAL = PrincipalId(UUID(int=2))
READ = ActionId("documents.read_document")
DOC1, DOC2 = ResourceRef("document", "d1"), ResourceRef("document", "d2")


def make_token(keyring: SigningKeyring | None = None, epoch: int = 3) -> str:
    scope = an_access_scope(
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        resources=frozenset({DOC1, DOC2}),
        policy_epoch=epoch,
    )
    return issue_proof(scope, READ, NOW, keyring or a_signing_keyring()).token


def reject(token: str, keyring: SigningKeyring | None = None) -> str:
    with pytest.raises(AuthorizationError) as caught:
        open_token(token, keyring or a_signing_keyring())
    return caught.value.code


def forge(payload: dict[str, object], key: SigningKey, key_id: str | None = None) -> str:
    """A token signed with a real key but a chosen payload: what a key holder could mint."""
    kid = key_id or key.key_id
    segment = (
        base64.urlsafe_b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        .decode()
        .rstrip("=")
    )
    message = b"lda.authorization-proof.v1\x00" + kid.encode() + b"\x00" + segment.encode()
    mac = hmac.new(key.secret, message, hashlib.sha256).digest()
    return f"v1.{kid}.{segment}.{base64.urlsafe_b64encode(mac).decode().rstrip('=')}"


def forge_segment(segment: str, key: SigningKey) -> str:
    message = b"lda.authorization-proof.v1\x00" + key.key_id.encode() + b"\x00" + segment.encode()
    mac = hmac.new(key.secret, message, hashlib.sha256).digest()
    return f"v1.{key.key_id}.{segment}.{base64.urlsafe_b64encode(mac).decode().rstrip('=')}"


def good_payload() -> dict[str, object]:
    return {
        "tenant": str(TENANT),
        "principal": str(PRINCIPAL),
        "action": "documents.read_document",
        "resources": resources_digest([("document", "d1")]),
        "count": 1,
        "epoch": 3,
        "iat": int(NOW.timestamp()),
        "exp": int(NOW.timestamp()) + 60,
    }


def test_round_trip_carries_every_bound_claim() -> None:
    claims = open_token(make_token(), a_signing_keyring())
    assert (claims.tenant_id, claims.principal_id, claims.action) == (TENANT, PRINCIPAL, READ)
    assert claims.policy_epoch.value == 3 and claims.resource_count == 2
    assert claims.resources_digest == resources_digest([("document", "d1"), ("document", "d2")])


def test_lifetime_is_sixty_seconds_from_the_truncated_issue_time() -> None:
    claims = open_token(make_token(), a_signing_keyring())
    assert timedelta(seconds=60) == PROOF_LIFETIME
    assert claims.issued_at == datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    assert claims.expires_at - claims.issued_at == timedelta(seconds=60)
    assert claims.expires_at <= NOW + timedelta(seconds=60)  # never longer than 60 s from now


def test_any_flipped_character_in_payload_or_mac_is_rejected() -> None:
    token = make_token()
    version, kid, payload, mac = token.split(".")
    for i in range(0, len(payload), 7):
        flipped = payload[:i] + ("A" if payload[i] != "A" else "B") + payload[i + 1 :]
        assert reject(".".join([version, kid, flipped, mac])) in {
            "proof_signature_invalid",
            "proof_malformed",
        }
    for i in range(0, len(mac) - 1, 5):
        flipped = mac[:i] + ("A" if mac[i] != "A" else "B") + mac[i + 1 :]
        assert reject(".".join([version, kid, payload, flipped])) in {
            "proof_signature_invalid",
            "proof_malformed",
        }


def test_a_payload_swapped_from_another_token_is_rejected() -> None:
    other = make_token(epoch=4)
    version, kid, _, mac = make_token().split(".")
    swapped = ".".join([version, kid, other.split(".")[2], mac])
    assert reject(swapped) == "proof_signature_invalid"


def test_a_token_signed_by_a_different_secret_is_rejected() -> None:
    forged = make_token(a_signing_keyring(secret=b"a-completely-different-secret-value!"))
    assert reject(forged) == "proof_signature_invalid"


def test_a_key_id_swap_is_rejected_even_with_the_right_secret() -> None:
    secret = b"shared-secret-for-two-key-ids-00000"
    token = make_token(a_signing_keyring(key_id="k-one", secret=secret))
    both = SigningKeyring(SigningKey("k-two", secret))
    version, _, payload, mac = token.split(".")
    assert reject(".".join([version, "k-two", payload, mac]), both) == "proof_signature_invalid"


def test_unknown_key_id_is_reported_as_such() -> None:
    assert reject(make_token(), a_signing_keyring(key_id="other-key")) == "proof_unknown_key"


@pytest.mark.parametrize(
    "token",
    [
        "",
        "v1",
        "v1.a.b",
        "v1.a.b.c.d",
        "v2.test-key-1.AAAA.AAAA",
        "v1.Test-Key.AAAA.AAAA",
        "v1.test-key-1.AA AA.AAAA",
        "v1.test-key-1.AAAA.AA=A",
        "v1.test-key-1.é.AAAA",
        "x" * (MAX_TOKEN_LENGTH + 1),
    ],
)
def test_malformed_tokens_are_rejected(token: str) -> None:
    assert reject(token) in {"proof_malformed", "proof_unknown_key", "proof_signature_invalid"}


def test_non_canonical_base64_is_rejected_even_when_correctly_signed() -> None:
    key = a_signing_keyring().active
    payload = json.dumps(good_payload(), sort_keys=True, separators=(",", ":")).encode()
    canonical = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    assert reject(forge_segment(canonical + "=", key)) == "proof_malformed"  # padded
    assert reject(forge_segment(canonical + "\n", key)) == "proof_malformed"  # trailing newline


def test_non_string_token_is_rejected() -> None:
    assert reject(None) == "proof_malformed"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("epoch"),
        lambda p: p.update(extra="x"),
        lambda p: p.update(epoch="3"),
        lambda p: p.update(epoch=True),
        lambda p: p.update(epoch=-1),
        lambda p: p.update(count=0),
        lambda p: p.update(tenant="not-a-uuid"),
        lambda p: p.update(tenant="00000000-0000-0000-0000-0000000000AA"),  # not lower-case
        lambda p: p.update(action="Bad Action"),
        lambda p: p.update(resources="abc"),
        lambda p: p.update(exp=int(NOW.timestamp()) + 61),  # lifetime over 60 s
        lambda p: p.update(exp=int(NOW.timestamp())),  # zero lifetime
        lambda p: p.update(iat=10**15, exp=10**15 + 60),  # out-of-range timestamps
    ],
)
def test_authentic_but_invalid_claims_are_malformed(
    mutate: Callable[[dict[str, object]], object],
) -> None:
    payload = good_payload()
    mutate(payload)
    key = a_signing_keyring().active
    assert reject(forge(payload, key)) == "proof_malformed"


def test_a_well_formed_forgery_with_the_real_key_still_opens() -> None:
    # Sanity for the forge() helper itself: the negative cases above fail on the claim, not on it.
    assert open_token(forge(good_payload(), a_signing_keyring().active), a_signing_keyring())


def test_resource_digest_is_order_independent_and_set_exact() -> None:
    a, b = ("document", "d1"), ("document", "d2")
    assert resources_digest([a, b]) == resources_digest([b, a]) == resources_digest([a, b, a])
    assert resources_digest([a]) != resources_digest([a, b])
    assert resources_digest([a, b]) != resources_digest([a, ("document", "d3")])
    assert resources_digest([a]) != resources_digest([("folder", "d1")])


def test_resource_digest_has_no_boundary_ambiguity() -> None:
    assert resources_digest([("ab", "c")]) != resources_digest([("a", "bc")])
    assert resources_digest([("a", "b"), ("c", "d")]) != resources_digest([("a", "bcd")])


def test_an_empty_scope_cannot_be_signed() -> None:
    empty = an_access_scope(tenant_id=TENANT, principal_id=PRINCIPAL, resources=frozenset())
    with pytest.raises(InvalidScope, match="scope_empty"):
        issue_proof(empty, READ, NOW, a_signing_keyring())


@pytest.mark.parametrize("secret", [b"", b"short", b"x" * 31])
def test_short_secrets_are_rejected(secret: bytes) -> None:
    with pytest.raises(InvalidSigningKey):
        SigningKey("key-1", secret)


@pytest.mark.parametrize("key_id", ["", "Key", "-a", "a b", "a" * 33, "a\n"])
def test_bad_key_ids_are_rejected(key_id: str) -> None:
    with pytest.raises(InvalidSigningKey):
        SigningKey(key_id, b"x" * 32)


def test_nothing_secret_appears_in_reprs() -> None:
    keyring = a_signing_keyring()
    scope = an_access_scope(
        tenant_id=TENANT, principal_id=PRINCIPAL, resources=frozenset({DOC1}), policy_epoch=1
    )
    proof = issue_proof(scope, READ, NOW, keyring)
    text = " ".join(repr(x) for x in (keyring, keyring.active, proof, proof.claims, scope))
    assert proof.token not in text and proof.token.split(".")[3] not in text
    assert keyring.active.secret.decode() not in text
    assert "d1" not in text  # resource ids stay out of logs


def test_base64_with_nonzero_trailing_bits_is_rejected_even_when_correctly_signed() -> None:
    """Two different strings can decode to the same bytes; only the canonical one is accepted."""
    key = a_signing_keyring().active
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    payload = json.dumps(good_payload(), sort_keys=True, separators=(",", ":"))
    for padding in range(4):  # JSON tolerates trailing spaces: reach a length with slack bits
        raw = (payload + " " * padding).encode()
        canonical = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        if len(canonical) % 4 == 0:
            continue
        twins = [
            canonical[:-1] + c
            for c in alphabet
            if c != canonical[-1]
            and base64.urlsafe_b64decode((canonical[:-1] + c) + "=" * (-len(canonical) % 4)) == raw
        ]
        assert twins  # a different spelling of the very same bytes exists
        assert open_token(forge_segment(canonical, key), a_signing_keyring())  # canonical is fine
        assert reject(forge_segment(twins[0], key)) == "proof_malformed"
        return
    raise AssertionError("no payload length with slack bits found")
