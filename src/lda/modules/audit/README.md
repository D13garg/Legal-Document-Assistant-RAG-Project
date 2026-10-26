# audit

> Insert-only, tenant-scoped evidence of effective mutations, written in the action's own transaction (ADR-019).

## Responsibility
Owns the `audit.records` table, the validation of what may be recorded (`AuditEntry`), the SQL recorder that implements the shared
`AuditRecorder` port, and its in-memory twin. It is **not** operational logging (ADR-020), **not** provenance/`GenerationRecord` (ADR-016) and holds
no business state. It has no use cases: producers write through `uow.audit`, never by calling this module.

## Invariants
- A fact is written on the action's own transaction: it commits and rolls back with the action, and any failure fails the action (fail-closed).
- Records are insert-only: the runtime role holds `INSERT` only; triggers reject `UPDATE`, `DELETE` (any role, owner included) and `TRUNCATE`.
- Tenant isolation: tenant-bound recorder, forced RLS on `tenant_id`, primary key leading with `tenant_id`, no foreign keys, no cross-schema reads.
- Identifiers and short labels only (invariant 13): at most 16 attributes, identifier keys (<= 64) that do not name content/secrets/personal data,
  printable single-line values (<= 256); `resource_id` has the opaque-id shape; `occurred_at` is timezone-aware; a fact for another tenant is rejected.
- The actor is a `PrincipalId` (none for system work, whose `purpose` is recorded as `attributes["purpose"]`). No display name or other Identity data.
- Step 4 records `success` facts of effective changes only. `denied`/`failure` exist in the port but nothing emits them yet.

## Public API (`api/`)
None. Consumers use the shared port. Wiring: `lda.bootstrap.audit.build_audit()` returns the `audit_factory` for `build_platform`
(call it first) and `AUDIT_MIGRATIONS` for `migrate.upgrade`. Fakes for tests: `lda.modules.audit.testing` (`InMemoryAuditStore`,
`StagedAuditRecorder`). Snapshot: `api.lock`.

## Dependencies
None (`depends_on = []`), and no module depends on `audit` (the DAG is unchanged).

## Owned data
Schema `audit`, table `audit.records` (`record_id` database-generated, `tenant_id`, `actor`, `action`, `resource_type`, `resource_id`, `outcome`,
`request_id`, `occurred_at`, `attributes`). No foreign keys.

## How an `@audited` use case emits
`uow.audit.record(AuditFact(action="<the declared label>", ...))` before `commit()`, only on an effective change. Archguard AG056 checks it. The
`@not_audited` exemption is explicit and reviewed (no emission); it does not override any future security-mandated audit requirement.

## Events in / out
None.

## Must NOT know about
Any other module, document content, prompts, model output, credentials, keys, proofs or tokens, HTTP, authorization decisions (deferred), retention
or deletion (ADR-023), a read/query API (not in Step 4), tamper-evidence chains.

## Testing in isolation
Unit tests (domain validation), a contract suite run on the in-memory and Postgres recorders, Postgres integration tests (insert-only, forced RLS,
atomicity, error hygiene) and `tests/platform/test_audit_bootstrap.py` (end-to-end through the composition root). Needs only this module's code.
