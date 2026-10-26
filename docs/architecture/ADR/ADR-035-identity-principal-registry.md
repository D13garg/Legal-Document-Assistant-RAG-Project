# ADR-035: Identity context: principal registry and the first module persistence pattern

## Status
Accepted

## Context
`identity` (step 2) is the first implemented module. `architecture.md` fixes its place (no dependencies; `access_control` depends on it) and the boundary "Identity = WHO, Access Control = WHAT", but has no Identity section: the Phase 1 chat text HANDOFF §1 names as the detail source is not in the repository. Nothing in the repository requires credentials, an external identity provider, tokens, or tenant membership. Because no module exists yet, Identity also sets the pattern later modules copy. Its manifest and `api.lock` are ADR-governed (AG072).

## Problem
Fix the smallest supportable Identity scope, and record how a module composes the platform transaction, owns its schema and ships migrations.

## Decision
1. **Scope: a tenant-scoped principal registry.** A `Principal` is `id` (`PrincipalId`), `tenant_id`, `kind` (`user` | `service`, from the kernel's "user or service" definition), `display_name`, `status` (`active` | `disabled`), `version`, `created_at`, `status_changed_at`. `id`, `tenant_id`, `kind`, `created_at` never change. Moves: `active -> disabled` and `disabled -> active`; any other move is a domain error. Principals are never deleted (erasure belongs to the retention ADR-023).
2. **Use cases** (class-level ADR-034 markers): `RegisterPrincipal` (`@requires("identity.register_principal")`), `DisablePrincipal`, `EnablePrincipal` (`@requires`), and the query `GetPrincipal` (`@public`: authorization must be able to ask who a principal is; `@not_audited`). Disable/enable are idempotent; registration is not (every call mints a new id). Audit labels are metadata only until ADR-019; use cases do not write audit facts.
3. **Explicitly out of scope** (not required by the baseline, not invented): credentials, passwords, tokens, an external IdP/OIDC/JWT, email, tenant membership, roles, permissions, `AccessScope`, authorization decisions, events, listing/search, identity merge or external-subject linking. Each needs its own decision when the architecture asks for it.
4. **Public surface** (`api/`): the four use-case classes, `RegisterPrincipalCommand`, `PrincipalReference`, `PrincipalView`, `PrincipalKind`, `PrincipalStatus`. The unit-of-work port, repository, ORM objects and the domain `Principal` stay internal. Errors use the kernel taxonomy with stable codes: `principal_not_found`, `principal_exists`, `principal_state_changed`, `display_name_invalid`, `principal_invalid_transition`.
5. **Neutral lookups.** A principal of another tenant is indistinguishable from an absent one (`principal_not_found`) because the repository is tenant-bound and RLS applies again.
6. **Module persistence pattern.** The module defines its own `UnitOfWork` extension (`IdentityUnitOfWork`, port in `application/`) whose SQL adapter wraps the platform `SqlUnitOfWork` and adds a tenant-bound repository (`infrastructure/persistence/`, instantiated only in `unit_of_work.py`, AG055). Optimistic concurrency uses `version`. Driver errors are translated to `InfrastructureError` and chained away because they carry statement parameters (display names). In-memory adapters live in `testing/`; a contract suite runs against both adapters.
7. **Schema and migrations.** Schema `identity`, table `identity.principals`, forced RLS on `tenant_id`, unique index `(tenant_id, id)`, runtime role granted `SELECT, INSERT, UPDATE` only. The Alembic branch `identity` lives in `infrastructure/persistence/migrations/versions/` and is passed to `platform.database.migrate` through `extra_locations`; `bootstrap/identity.py` exposes the location and wires the use cases.
8. **Display name is personal data**: trimmed, 1-200 printable characters, redacted in `repr`, never in errors, logs or audit payloads.

## Rationale
The smallest model that answers WHO and "is it active" lets `access_control` (step 3) build on a stable contract without Identity learning anything about permissions. Unsupported features are cheaper to add with a requirement than to remove.

## Alternatives considered
- Credentials or an IdP link now: no baseline requirement; would pull authentication infrastructure ahead of need.
- Tenant membership in Identity: the baseline does not assign it; `TenantContext` already carries the tenant.
- Hard delete of principals: conflicts with later audit/retention; disable is sufficient.
- Raising on repeated disable/enable: pushes retry handling onto every caller.

## Consequences
- Authentication is not solved here; whoever builds it must map an authenticated actor to a `PrincipalId` and needs its own ADR.
- `@requires` strings are labels until ADR-014; `GetPrincipal` is `@public` by design and reviewers should revisit its reason when access control lands.
- First module fixes conventions (UoW extension, migrations location) that later modules inherit.

## Security implications
No secrets exist in Identity. Display names are redacted from `repr` and errors, tenant isolation is enforced by predicate plus forced RLS, and the runtime role cannot delete.

## Operational implications
Deployments must run the `identity` migration branch (`extra_locations=[IDENTITY_MIGRATIONS]`) before use; the migration needs the runtime role to exist.

## Migration implications
Introduces the `identity` Alembic branch (`i0001`); downgrade drops the schema.
