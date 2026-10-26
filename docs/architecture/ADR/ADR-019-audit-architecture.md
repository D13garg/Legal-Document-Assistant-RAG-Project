# ADR-019: Audit architecture

## Status
Accepted

## Context
Architecture invariant 13 forbids content in logs, metrics, traces and audit. The shared port `AuditRecorder` and `AuditFact` (frozen in Step 1) already say what a producer hands over, that the write happens "in the caller's transaction", and that its failure "must fail the action (fail-closed)"; the platform's `SqlUnitOfWork` builds the recorder per unit of work from an `audit_factory` that has no default. ADR-028 treats audit as a reaction that must be atomic with the action. ADR-034 declares `@audited` / `@not_audited` as pure metadata, and its §10 predicted that "the audit module reads `audit_of(...)`" with resource and outcome taken from "the execution context". That prediction cannot be implemented: `audit` has `depends_on = []` (it cannot read other modules' use cases) and the execution context carries no resource or outcome. Identity (Step 2) and Access Control (Step 3) declare five `@audited` use cases that emit nothing, and ADR-014 notes that hard-deleted grants leave no history "until audit exists". No ADR file for audit existed; this one records the decisions the owner approved for Step 4 (D1-D9).

## Problem
Decide what Audit is and is not, what a record contains, how an `@audited` declaration becomes a record, which outcomes are recorded, what makes records immutable, and how failure and transaction semantics work, without adding dependencies, generic logging, or machinery the architecture does not yet need.

## Decision
**Concepts kept apart.**
- *Audit records* (this ADR): who did what to which resource, with what outcome, within one tenant, written transactionally and kept as evidence.
- *Operational logs, metrics and traces* (ADR-020): allow-listed, no content, not evidence.
- *Provenance and `GenerationRecord`* (ADR-016, Step 13): owned elsewhere; Audit does not store generations, prompts, outputs or citations.
- *Debug output*: never persisted and never carries content.

**D1. Explicit emission; no wrapper.** Each `@audited` use case calls `uow.audit.record(AuditFact(...))` itself, in the unit of work that performs the mutation, before `commit()`. Tenant, actor and request id come from the execution context (`TenantContext`: principal and request id; `SystemContext`: no actor, no request id). Resource and outcome are decided by the use case, which is the only code that knows them. There is no runtime wrapper, interceptor or decorator behaviour: ADR-034 §1 stands. The declaration is connected to the runtime by the archguard rule **AG056**: an `@audited(x)` class must call `<uow>.audit.record(AuditFact(action="x", ...))` with the declared label as a string literal, each `record` call must be an inline `AuditFact(...)` with that action, and the method that commits must record before its first `commit()`; a `@not_audited` class must not emit. The kernel is unchanged. `audit` depends on nothing and nobody depends on it: producers use only the shared port; bootstrap supplies the recorder.

**D2. Outcome scope.** Step 4 records successful, effective mutations (`outcome=success`) only. The port keeps `denied` and `failure`, and the recorder accepts them, but nothing emits them: a failed operation rolls back its transaction, which would roll back a same-transaction record, so failed or denied attempts need a separate transaction with its own failure semantics. That is deferred until the API layer (Step 16) exists; no separate failure-audit transaction is invented here.

**D3. Decision auditing.** `AuthorizeAction` and `ResolveAccessScope` remain `@not_audited`; the Step 3 hot path is untouched. Decision and egress auditing are deferred to the API-layer and `ai_gateway` steps.

**D4. Frozen modules.** The five existing audited use cases gain only the additive `record` call (and a small per-module `attribution_of(ctx)` helper that reads the context). `RevokeAccess` takes a `Clock` for the timestamp, which changes its constructor and `api.lock`. No DAG, authorization or API-semantics change.

**D5. Effective changes only.** Exactly one fact per effective state change; an idempotent or no-op mutation (re-grant, revoke of an absent grant, disabling a disabled principal, enabling an active one) emits none, as does any failed or rejected call.

**D6. Fact constraints** (validated by the domain value `AuditEntry`; `domain` imports no `ports`/`platform`). At most 16 attributes. Keys are ADR-034 identifiers of at most 64 characters, and a key containing a content-, secret- or personal-data word (`content`, `text`, `body`, `prompt`, `completion`, `response`, `output`, `answer`, `excerpt`, `snippet`, `chunk`, `payload`, `token`, `secret`, `password`, `credential`, `key`, `signature`, `proof`, `mac`, `display`, `email`) is refused. Values are printable, single-line, at most 256 characters. `action` is an ADR-034 identifier; `resource_type` has the resource-kind shape; `resource_id` the resource-id shape (no free text); `occurred_at` is timezone-aware; the outcome is one of the port's three. `SystemContext.purpose` is recorded as `attributes["purpose"]`. A fact whose tenant differs from the unit of work's is rejected. The record id is generated by the database and never read back. Rejections raise a code-only `InvalidAuditEntry`; storage errors become `InfrastructureError("audit_persistence_failed")` with the driver error chained away (it embeds parameters). The key-word guard stops the obvious mistake; it is not proof that a value is not content, which remains the author's and reviewer's responsibility.

**D7. Immutability.** Records are insert-only. Table `audit.records` (schema `audit`, ADR-027) has forced RLS, a policy bound to `app.tenant_id`, a primary key leading with `tenant_id`, CHECK constraints mirroring the domain shape, and no foreign keys. The runtime role is granted `INSERT` only (no `SELECT`, `UPDATE`, `DELETE`, `TRUNCATE`); triggers reject `UPDATE` and `DELETE` for every role including the owner, and `TRUNCATE` is blocked. No retention or deletion exists in Step 4; that is deferred to ADR-023. There is no hash chain or other tamper-evidence mechanism.

**D8. No read side.** Audit exposes no read or query API in Step 4. Tests inspect records through the admin test engine and the in-memory store. A read use case, with its own authorization and pagination decisions, is a later decision.

**D9. `@not_audited`.** It is an explicit, reviewed exemption from ordinary declaration-driven emission, with a mandatory reason. It is not permission to bypass any future security-mandated audit requirement, and no such separate category is introduced now. The set of exemptions is pinned by a test, so adding one is a visible diff.

**Atomicity and failure.** The audit insert executes on the action's own `TenantTransaction` when `record` is called, so it commits and rolls back with the action. Any failure raises and fails the action (fail-closed). It is not best-effort and not outbox-backed.

## Rationale
The port, the unit of work and ADR-028 already imply in-transaction, fail-closed audit; making the use case emit explicitly keeps every dependency pointing at the kernel and ports, keeps declarations metadata, and lets a static rule prove that "declared" means "emitted". Scoping outcomes to committed changes avoids a second transaction model before there is a layer that needs it. Insert-only with defence in depth (privileges, triggers, RLS) matches the evidence role of the records.

## Alternatives considered
- A runtime wrapper/interceptor reading `audit_of`: contradicts ADR-034 §1, hides emission, and cannot know the resource or outcome.
- The audit module scanning use cases: needs an edge from `audit` to every module.
- Best-effort or outbox-backed audit: a lost record would be silent; the port already says fail-closed.
- Auditing failures and denials now: needs a separate transaction and failure semantics not yet designed.
- A generic `AuditService`/manager: banned by AG050 and unnecessary.
- A read API now: an unreviewed new authorization surface.

## Consequences
- Five audited use cases now emit; `RevokeAccess` has a new constructor parameter (`api.lock` changes).
- AG056 is a new archguard rule (waivable like other design rules); `tools/archguard/policy.py` is ADR-governed, and this ADR satisfies the gate.
- Facts for denied or failed attempts, authorization decisions and egress are absent until later steps; the audit trail is therefore incomplete by design.
- Records cannot be deleted in Step 4; erasure and retention conflicts are for ADR-023.
- Records cannot be read through the application until a read side is decided.

## Security implications
Audit never stores content, prompts, outputs, credentials, keys, proofs, tokens or display names; only identifiers and labels are accepted, bounded, and checked. The runtime role cannot read or change history. Tenant isolation is enforced by the tenant-bound recorder, forced RLS and the tenant-leading key. Because a rolled-back action leaves no record, attempted-but-failed operations are not evidence in Step 4.

## Operational implications
One extra insert per effective mutation, inside the existing transaction. The `audit` migration branch must be passed to `migrate.upgrade` (`AUDIT_MIGRATIONS`); `build_audit()` must be called before `build_platform`, which still has no default audit factory.

## Migration implications
Migration `u0001` creates schema `audit`. Its downgrade drops it. No existing data is touched.
