# access_control

> Authorization: WHAT may a principal do - explicit grants, default deny, AccessScope, policy epoch, authorization proof.

`Identity = WHO are you?`  `Access Control = WHAT may you access?`
Decision records: [ADR-014](../../../../docs/architecture/ADR/ADR-014-authorization-model.md) (model, registry,
AccessScope, epoch) and [ADR-026](../../../../docs/architecture/ADR/ADR-026-authorization-proof-and-egress.md)
(proof, live-epoch verification, egress). Markers: ADR-034. ADR-022 is deferred.

## Responsibility
Decides whether a principal of a tenant may perform an action on resources, from explicit per-principal grants
of `(resource, action)`. Default deny. Returns the **exact** authorized resource set as a frozen `AccessScope`
and an HMAC-bound, 60-second `AuthorizationProof`. Owns the resource registry, the grants, the per-tenant
`policy_epoch`, and the only signing capability.

## Domain concepts
- `ResourceRef(kind, id)`: opaque `(kind, id)`; the id is owned by the owning module. `ActionId`: dotted
  identifier (`documents.read_document`), the vocabulary of `@requires`.
- `ResourceRegistry` / `ResourceKindDefinition`: which actions exist for which kinds. Code-defined, registered by
  owning modules' bootstrap through this API, **sealed at composition**.
- `Grant`: one `(principal, resource, action)` permission in one tenant. No roles, groups, inheritance,
  wildcards, deny rules, conditions or expiry in v1.
- `PolicyEpoch`: monotonic per-tenant integer (0 = no change yet).
- `AccessScope`: frozen `(tenant, principal, policy_epoch, resources: frozenset[ResourceRef])`. Not a cache; it
  cannot be widened or re-resolved. Constructible only inside this module (AG090).
- `AuthorizationProof`: `v1.<key_id>.<payload>.<mac>`; binds tenant, principal, action, resource-set digest and
  count, epoch, issued-at and expiry (<= 60 s). Constructible only inside this module (AG090).
- `AuthorizationDecision`: ALLOW (scope + proof) or DENY (reason only): `principal_not_active`, `not_granted`,
  `unknown_action`.

## Invariants
- Default deny; an unregistered kind/action is never honoured, even with a stale grant row.
- Tenant comes from the execution context only; repositories are tenant-bound and RLS applies again.
  `SystemContext` cannot request a decision (`authorization_requires_principal`).
- Principal status is read **live** from Identity on every decision (never cached); a disabled, absent or
  other-tenant principal is denied.
- The epoch advances by exactly 1, in the same transaction as a *changing* grant/revoke (idempotent repeats do
  not advance it); a trigger rejects any other epoch update. A decision reads the epoch before the grants.
- A proof is valid only if ALL hold, checked live in this order: HMAC + canonical encoding; exact binding; `now < exp`; `policy_epoch` equals the tenant's live epoch; the principal is **currently active** (Identity, never cached). Any failure raises; a database/Identity failure also means no egress.
- No cross-module foreign keys or reads; no secrets or resource ids in errors, `repr`, logs.

## Dependencies
`identity` (`module.toml`, approved DAG edge): only `identity.api.GetPrincipal` (decisions, grants and the proof verifier's live status check). Uses `shared.kernel`,
`shared.ports` (UnitOfWork, Clock) and, in infrastructure only, `platform.database`. `ai_gateway` does **not**
depend on this module: it verifies proofs through `lda.shared.ports.authorization.ProofVerifier`, implemented by
`EpochCheckedProofVerifier` and injected by `bootstrap/access_control.py`.

## Revoking everything on a resource (Step 5, ADR-023)
`GrantRepository.remove_all_for_resource` and the adapter `SqlResourceAccessRevoker` (shared port `ResourceAccessRevoker`) remove every grant on one resource
and advance the policy epoch exactly once if any was removed, inside the CALLER's transaction; it never commits. Bootstrap (`AccessControlContainer.revoker_factory`)
hands it to the owning module's unit of work. It is not a use case and not an HTTP surface.

## Public API (`api/`, snapshot in `api.lock`)
| Name | Kind | Declarations (ADR-034) |
|---|---|---|
| `GrantAccess` | use case | `@requires("access_control.grant_access")`, `@audited("access_control.access_granted")` (emits one fact per effective grant) |
| `RevokeAccess` | use case | `@requires("access_control.revoke_access")`, `@audited("access_control.access_revoked")` (emits one fact per effective revoke; takes a `Clock` since Step 4) |
| `AuthorizeAction` | query (strict: every requested resource must be granted) | `@public(reason=...)`, `@not_audited(reason=...)` |
| `ResolveAccessScope` | query (filter: scope = granted subset; none = DENY) | `@public(reason=...)`, `@not_audited(reason=...)` |
| `EpochCheckedProofVerifier`, `PolicyReader` | verifier / decision reader (wired by bootstrap) | |
| `ResourceRegistry`, `ResourceKindDefinition`, `ResourceRef`, `ActionId`, `PolicyEpoch`, `AccessScope`, `AuthorizationDecision`, `AuthorizationProof`, `AuthorizationRequest`, `GrantAccessCommand`, `RevokeAccessCommand`, `PolicyChangeView`, `DenyReason`, `Effect`, `SigningKey`, `SigningKeyring`, `PROOF_LIFETIME`, `MAX_SCOPE_RESOURCES` | DTOs / values | |

Errors: `authorization_requires_principal`, `authorization_resources_invalid`, `action_not_allowed_for_resource`
(validation), `principal_not_found`, `proof_malformed`, `proof_unknown_key`, `proof_signature_invalid`,
`proof_mismatch`, `proof_expired`, `proof_stale_epoch`, `proof_principal_inactive` (authorization), `access_control_persistence_failed`.
There is **no HTTP middleware** (D8): `@requires` is wired at the API layer in a later step.

## Configuration (slice `access_control`, ADR-031)
`LDA_ACCESS_CONTROL__PROOF_KEY_ID` (`[a-z0-9][a-z0-9_-]{0,31}`) and `LDA_ACCESS_CONTROL__PROOF_KEY`
(base64url, >= 32 bytes). One active key; the token carries the key id so rotation can be added without a
format change. The 60-second lifetime is a constant, not configuration.

## Owned data
Schema `access_control`: `grants` (PK `(tenant_id, principal_id, resource_kind, resource_id, action)`) and
`policy_epochs` (PK `tenant_id`). Forced RLS on both; CHECK constraints mirror the identifier shapes; the epoch
trigger enforces `+1`; runtime role: `SELECT, INSERT, DELETE` on grants, `SELECT, INSERT, UPDATE` on epochs. No
foreign keys. Alembic branch `access_control` (`infrastructure/persistence/migrations/versions/`), passed via
`extra_locations` (`lda.bootstrap.access_control.ACCESS_CONTROL_MIGRATIONS`).

## Events in / out
None.

## Explicit non-responsibilities
Authentication, credentials, tokens other than the proof, roles/groups, tenant membership, bootstrapping the first
grant, decision auditing (deferred by ADR-019 D3), HTTP/middleware, retrieval filtering, prompt construction, the egress call
itself (`ai_gateway`, step 9), untrusted-content security (ADR-022, deferred).

## Principal-disable semantics
Disabling a principal does not touch `policy_epoch` (Identity never bumps it and does not depend on this module).
Instead, new decisions are denied (`principal_not_active`) and proof verification rejects every outstanding proof of
that principal immediately (`proof_principal_inactive`). Re-enabling revives nothing by itself: validity is the
conjunction of the five rules evaluated afresh, so an expired, stale or otherwise invalid proof stays invalid.

## Testing strategy (`tests/`, `testing/`)
- `tests/unit`: resource/action shapes, registry, proof codec (tamper, canonical encoding, digest, key id), config.
- `tests/application`: decision behaviour, grants/revocation/epoch, identity boundary, tenant isolation, proof
  verification (stale, tampered, wrong tenant/principal/action/resources, expired, fail closed). Run against the
  in-memory **and** the Postgres storage.
- `tests/contract`: one repository/UoW contract on both adapters; public-API contract.
- `tests/integration`: real Postgres: schema, no FKs, forced RLS, privileges, epoch trigger, atomicity, concurrency,
  error hygiene, migration round trip.
- `tests/architecture/test_access_control_boundaries.py`: DAG, no `ai_gateway` edge, mint confinement, no roles,
  no foreign persistence, kernel stays opaque, no waivers. `tests/platform/test_access_control_bootstrap.py`: the
  real composition end to end.
