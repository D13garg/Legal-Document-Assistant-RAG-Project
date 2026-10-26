# identity

> Principal registry: WHO is the actor (user or service) and is it active.

`Identity = WHO are you?`  `Access Control = WHAT may you access?` (step 3, not here).
Decision record: [ADR-035](../../../../docs/architecture/ADR/ADR-035-identity-principal-registry.md); markers: ADR-034.

## Responsibility
Owns the existence and lifecycle of principals inside a tenant. It answers "who is this principal and is it
active", nothing more.

## Domain concepts
- `Principal` (immutable): `id`, `tenant_id`, `kind` (`user` | `service`), `display_name`, `status`
  (`active` | `disabled`), `version`, `created_at`, `status_changed_at`.
- `DisplayName`: trimmed, 1-200 printable single-line characters; personal data (redacted in `repr`).
- Lifecycle: `active -> disabled -> active`. Principals are never deleted.

## Invariants
- `id`, `tenant_id`, `kind`, `created_at` never change; every move returns a new value with `version + 1`.
- Timestamps are timezone-aware and `status_changed_at >= created_at`.
- Invalid moves (disable a disabled principal, enable an active one) are domain errors;
  the use cases make disable/enable idempotent by returning the current state without a write.
- A principal is only visible inside its own tenant (tenant-bound repository + forced RLS). Another tenant's id
  is reported as `principal_not_found`, never as "forbidden".
- Nothing in Identity holds or returns a credential, token or secret. Display names never appear in errors,
  `repr`, logs or driver exceptions.

## Dependencies
None (`module.toml` `depends_on = []`; the approved DAG gives Identity no dependencies). Uses `shared.kernel`,
`shared.ports` (UnitOfWork, Clock, IdGenerator) and, in infrastructure only, `platform.database`.

## Public API (`api/`, snapshot in `api.lock`)
| Name | Kind | Declarations (ADR-034) |
|---|---|---|
| `RegisterPrincipal` | use case | `@requires("identity.register_principal")`, `@audited("identity.principal_registered")` |
| `DisablePrincipal` | use case | `@requires("identity.disable_principal")`, `@audited("identity.principal_disabled")` |
| `EnablePrincipal` | use case | `@requires("identity.enable_principal")`, `@audited("identity.principal_enabled")` |
| `GetPrincipal` | query | `@public(reason=...)`, `@not_audited(reason=...)` |
| `RegisterPrincipalCommand`, `PrincipalReference`, `PrincipalView`, `PrincipalKind`, `PrincipalStatus` | DTOs / enums | |

Every `execute(ctx, input)` takes an `ExecutionContext`. Errors: `display_name_invalid` (validation),
`principal_not_found`, `principal_exists` / `principal_state_changed` (conflict).
The markers are metadata only: nothing enforces `@requires` until the API layer (step 16). Each effective `@audited` mutation emits one
`AuditFact` through `uow.audit` before `commit()` (ADR-019, checked by AG056): the principal id and kind, never the display name. A no-op
(disabling a disabled principal, enabling an active one) and every failure emit nothing.

## Owned data
Schema `identity`, table `identity.principals` (forced RLS on `tenant_id`, CHECK constraints for kind, status,
version, name length and timestamp order; runtime role has no DELETE). Alembic branch `identity`
(`infrastructure/persistence/migrations/versions/`), passed to `platform.database.migrate` via
`extra_locations` (see `lda.bootstrap.identity.IDENTITY_MIGRATIONS`). No foreign keys leave the schema.

## Events in / out
None.

## Explicit non-responsibilities
Authorization decisions, permissions, roles, ACLs, `AccessScope`, tenant authorization policy, resource or
document access, credentials, passwords, tokens, an external identity provider (OIDC/OAuth/JWT), email,
tenant membership, listing/search of principals, the audit runtime (module `audit`), RAG and AI providers.

## Testing strategy (`tests/`, `testing/`)
- `tests/unit`: domain invariants, lifecycle, redaction.
- `tests/application`: use cases against `testing.InMemoryIdentityUnitOfWorkFactory` (success, invalid input,
  idempotency, commit/rollback, tenant isolation, conflicts, no logging, declaration markers).
- `tests/contract`: one repository/UoW contract run against the in-memory and the SQL adapters, and the
  public-API contract (a consumer uses `api` + `testing` only).
- `tests/integration`: real Postgres: schema ownership, constraints, forced RLS, no DELETE, rollback,
  driver-error hygiene, migration round trip.
- `tests/architecture/test_identity_boundaries.py`: DAG, schema isolation, purity, no authorization or
  credential vocabulary, constructors confined to the module UoW.
