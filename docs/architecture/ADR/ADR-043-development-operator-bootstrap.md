# ADR-043: Development-only trusted-operator bootstrap for the headless Step 20 slice

## Status
Accepted

## Context
Approval state (2026-10-21): the sole project owner authorized "Option (a)": a tightly constrained, development-only trusted-operator bootstrap, to unblock the Step 20 headless vertical slice. That is a project-owner decision record only; it is not a team approval, a code-owner review, a CI run or a production-readiness approval.

Step 3 left the **first grant** open (HANDOFF Step 3, limitation 3): `GrantAccess` is declared `@requires("access_control.grant_access")`, which an empty policy cannot satisfy. ADR-014 section 13 places "authentication" and "an administrative bootstrap for the very first grant" out of scope. Facts established from source at the Step 18 checkpoint:

- `@requires` is metadata only (ADR-034); nothing enforces it anywhere, and no HTTP/API layer exists.
- `ExecutionContext = TenantContext | SystemContext`. `TenantContext` is a plain dataclass; nothing issues it from an authenticated source. `SystemContext(tenant_id, purpose)` is the existing, distinct type for platform work ("must never masquerade as a user action"); `purpose` is mandatory and is recorded in audit with no actor (ADR-019 D6).
- `RegisterPrincipal` and `GrantAccess` accept any `ExecutionContext`, scope every write to `ctx.tenant_id` through `TenantTransaction` (the `app.tenant_id` setting that forced RLS reads), and emit audit facts. Existing tests already register principals under `SystemContext(T1, "bootstrap-test")`.
- Grants are per `(principal, resource, action)`; a document id exists only after upload, so a one-time pre-grant is impossible.
- `RegisterPrincipal` mints a random principal id and there is no lookup by name.
- The repository has no environment model (no "development/production" setting).

## Problem
The headless slice must upload a document and then answer a question over it, which needs a tenant, a principal, and a `documents.read_document` grant on the uploaded document. No supported way exists to obtain them, and the driver must not silently fabricate a `TenantContext`, bypass `@requires`, or trust request-supplied identifiers.

## Decision
1. **Purpose and scope.** A *development-only* operator boundary, `lda.bootstrap.dev_operator`, for local use by the Step 20 slice. It is not an authentication design and not a production bootstrap.
2. **Trusted operator.** Trust is exactly: the person who controls the process environment of a command they run themselves (`LDA_DEV_OPERATOR__*` variables, read only through `ConfigurationLoader`). No request, header, file upload or database value is ever an input to identity. The operator-supplied values are a tenant UUID and an optional principal UUID.
3. **Tenant and principal binding.** The tenant is the configured UUID. The principal is either (a) created by the real `RegisterPrincipal` under that tenant, or (b) the configured `principal_id`, which is verified through the real `GetPrincipal` under that tenant (RLS makes another tenant's principal look absent), must be `USER` kind and `ACTIVE`. A `TenantContext` is built by `DevOperator.tenant_context()` only after that verification succeeds, on every call.
4. **Initial grant.** `DevOperator.grant_first_read(resource_id)` calls the real, unmodified `GrantAccess` for exactly: the configured principal, resource kind `document`, action `documents.read_document`, in the configured tenant. It cannot grant any other action, kind, principal or tenant, and has no parameter for them. It is called by the development driver after upload, because the document id does not exist earlier.
5. **Authorization boundary.** No exception inside Access Control is created and `@requires` is not modified. The operator acts under `SystemContext(tenant, "dev_operator_bootstrap")`. Honest statement: `@requires` remains declaration-only repo-wide, so *no* caller is checked; this ADR does not add enforcement, it adds a narrow, explicit, environment-guarded, audited caller. The class is not reachable from any request path (none exists), is not exported through any module `api`, and lives in `bootstrap/`, which modules cannot import (import-linter contract).
6. **Auditability.** The existing audit facts are reused: `identity.principal_registered` and `access_control.access_granted`, each recorded with `actor = None` and `purpose = dev_operator_bootstrap`, the policy epoch, the principal id and the document id. No secret, display name or document content is recorded or printed.
7. **Database privileges.** The operator uses the normal runtime engine (`build_platform`, `LDA_DATABASE__URL`), i.e. the non-superuser, non-owner, non-`BYPASSRLS` role, with forced RLS. It never receives or uses the admin URL.
8. **Environment guard.** `LDA_DEV_OPERATOR__ENVIRONMENT` must equal the literal `development`; there is no default, so an absent or any other value fails closed with `configuration_invalid` (values are never echoed). A second guard refuses a database whose host is not loopback (`localhost`, `127.0.0.1`, `::1`) or a Unix socket, so a production URL fails even if the first variable is set by mistake. Neither guard is a security boundary against an attacker who controls the environment; they prevent accidents.
9. **Lifecycle.** A separate one-shot command (`python -m apps.dev_bootstrap`). It is never run at worker or application start-up and the worker does not import it.
10. **Failure behavior.** Repeat run with the configured principal: no write, success (`created=false`). No configured principal: a *new* principal is registered each run and its id is printed once (not a secret); the operator records it in `LDA_DEV_OPERATOR__PRINCIPAL_ID`. An unrecorded principal holds no grants and is harmless. Wrong tenant, absent, disabled or non-USER principal: fail with a stable code and no write. Missing or invalid configuration: fail before any connection pool exists. Repeated grant is idempotent (`GrantAccess` already is) and writes no second audit fact.
11. **Testing.** Settings refuse a non-`development` or absent environment and non-loopback database; identity verification fails for another tenant's principal, an absent, disabled or non-USER principal; the grant is scoped to the document read action and the configured tenant, and is invisible from another tenant (real Postgres, runtime role); audit facts carry the system purpose and no actor; the apps entry point has an allow-list test.
12. **Limitations.** This is not production authentication. It must not be used as a substitute for an API authentication design, and it should be removed or replaced when an authenticated API layer defines tenant identity and the first-grant policy. Principal creation is not idempotent without the recorded id (Identity has no lookup; frozen). Identity and Access Control are unchanged.

## Rationale
`SystemContext` already exists to carry platform work without pretending to be a user, and both use cases already audit it. Reusing it needs no change to frozen modules, no new bypass, and keeps tenant scope enforced by the database rather than by this code.

## Alternatives considered
- **Driver builds `TenantContext` and calls `GrantAccess`**: what tests do; rejected because it makes the driver an unlabelled trust root.
- **Seed grants with SQL**: rejected; bypasses the use case, the epoch rule and audit.
- **A "bootstrap" permission or super-grant in Access Control**: rejected; a reusable authorization bypass and a frozen-module change.
- **Wait for an API layer**: valid, but blocks the owner-requested slice; this ADR is deliberately temporary.

## Consequences
One bootstrap-layer module and one thin `apps/` entry point are added. The Step 6 guard `test_no_production_entry_point_was_created` is superseded by an allow-list (see Operational implications). No module, migration, `api.lock` or dependency DAG change.

## Security implications
Residual risks: an operator who sets the environment variable in a shared environment gets a principal and read grants on documents of the configured tenant only; grants are tenant-scoped by RLS regardless of this code; `@requires` is still unenforced repo-wide.

## Operational implications
`apps/` now may contain exactly the allow-listed entry points (`dev_bootstrap.py`). `tests/architecture/test_ingestion_boundaries.py` is changed from "apps is empty" to "apps holds only allow-listed files"; this is a deliberate, owner-authorized supersession, not a weakening of the dependency rules (those still run on `apps/`).

## Migration implications
None. No schema, role or grant changes.
