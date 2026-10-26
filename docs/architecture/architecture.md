# Legal Document Assistant: Architecture (canonical)

Status: Phase 1 baseline (approved). This file is the source of truth; ADRs record why.
Changing this file requires an ADR (archguard AG072).

## 1. Frozen decisions
Modular monolith; hexagonal/clean layering; DDD bounded contexts; the 15 v1 contexts below;
`retrieval` owns the search/index projection; `grounded_generation` owns the shared generation/validation
pipeline; `ai_gateway` owns provider infrastructure; `prompts` owns prompt lifecycle; Postgres-backed
transactional job queue; schema-per-module (no cross-schema reads/FKs); tenant-bound repositories + RLS;
`AccessScope` + authorization recheck + `AuthorizationProof`; immutable `GenerationRecord`; no persisted
citation excerpts; no content caches in v1; version-aware retrieval; no unvalidated answer streaming;
evaluation gates before production configuration activation; CI-enforced boundaries; ADR-required changes.

## 2. Bounded contexts (v1)
identity, access_control, audit, documents, ingestion, document_processing, chunking, retrieval, ai_gateway,
prompts, citations, grounded_generation, answering, conversations, evaluation.
Approved dependency edges: `docs/architecture/module-dag.toml` (machine-checked).
Deferred (created only when built): summarization, clause_analysis, document_comparison, usage, notifications.

## 3. Repository layout
```text
src/lda/modules/<m>/{module.toml,README.md,api/,api.lock,testing/,domain/,application/,infrastructure/,presentation/,config.py,tests/}
src/lda/shared/{kernel,ports}   src/lda/platform   src/lda/bootstrap
apps/{api,worker,evaluation}    tools/archguard    docs/    eval/    tests/{architecture,security,e2e,rag_eval,load}
```
Root package is `lda` (ADR-002, avoids shadowing stdlib `platform`).

## 4. Invariants
1. Authorization precedes content: text reaches a provider only inside an `AuthorizedContext` whose proof is verified at egress.
2. Tenant scope by construction: tenant-bound repositories, `tenant_id` + forced RLS, jobs derive tenant from their row.
3. Default deny; ACLs evaluated live; `policy_epoch` moves with every relevant change.
4. No citation without an authorized source in the same generation; excerpts never persisted.
5. Every AI output has an insert-only `GenerationRecord` carrying `config_id`.
6. Untrusted by default: retrieved text only in data segments; the LLM has no tools; output schema-validated.
7. Unvalidated text is never shown; `INSUFFICIENT_EVIDENCE` is a first-class outcome.
8. Module boundaries hold: imports only to `api`/`testing`; DAG equals manifests; no cross-schema access.
9. Domain and application are framework- and SDK-free; only `bootstrap` composes.
10. Stages are idempotent under at-least-once delivery.
11. Versions are immutable; only `READY` versions are searchable; default scope is the current version.
12. Deletion is provable (tombstone + ACL revocation atomic; purge receipts; verify).
13. No content in logs, metrics, traces or audit.
14. Every use case declares `@requires`/`@public` and `@audited`/`@not_audited`.
15. Every retrieval/generation-affecting change yields a new `RagConfiguration` and passes gates.
16. No god objects (naming bans, size/arity limits, enforced).
17. Provider failure never cascades.

## 5. Enforcement
Rule catalogue: `docs/architecture/enforcement.md`. Entry points: `make arch` (archguard + import-linter), `make check`.

## 6. Implementation order
0 enforcement skeleton (done) 1 shared+platform (done) 2 identity (done) 3 access_control (done) 4 audit (done) 5 documents (done) 6 ingestion (done)
7 document_processing 8 chunking 9 ai_gateway 10 prompts 11 retrieval 12 evaluation core 13 citations
14 grounded_generation 15 conversations 16 answering 17 full evaluation/release gating 18 hardening.
Sections for each module (ports, data, flows, diagrams) are added in its step.

## 7. Step 1 foundations (implemented)
**shared/kernel** (pure): typed ids (`TenantId`, `PrincipalId`, `RequestId`, `EventId`, `JobId`), `TenantContext`/`SystemContext`
(+ `ExecutionContext`), `Sensitive[T]`, error taxonomy (`LdaError` + categories), `DomainEvent`, use-case declaration markers (`declarations.py`, ADR-034: pure metadata).
Deferred until a second consumer exists: `CharSpan`, `PageNumber`, `SectionPath`, `Result`, version primitives.
`TenantContext` has no `roles_epoch` (policy epoch belongs to access_control's `AccessScope`; adding fields is additive).

**shared/ports** (contracts only): `Clock`, `IdGenerator`, `UnitOfWork`/`UnitOfWorkFactory`, `EventPublisher` (+`Delivery`),
`EventHandler`, `JobQueue`/`JobHandler` (+ `JobSpec`, `ClaimedJob`, outcomes), `ObjectStore` (+`TenantScopedKey`),
`RateLimiter` (+`RateLimitKey` tenant-qualified), `AuditRecorder` (+`AuditFact`), `ProofVerifier` (+`ProofExpectation`, `ProofResource`; step 3, ADR-026).

**platform**: `configuration` (only env reader, ADR-031), `telemetry` (allow-listed JSON logs, ADR-020), `database`
(runtime engine, tenant-bound `TenantTransaction` + forced RLS, `SqlUnitOfWork(Factory)`, Alembic runner, Postgres job queue:
enqueue/leases/maintenance/worker; ADR-003/012/013/024), `events` (registry, codec, publisher, job handler; ADR-028),
`storage` (S3 `ObjectStore`), `ratelimit` (in-memory, Redis token bucket, fail-open), `testing` (in-memory doubles; tests only).

**bootstrap**: `build_platform(config, audit_factory=...)` validates every config slice first, then wires platform components. `audit_factory` has no default: audit is
never silently absent (the audit module supplies it: `bootstrap.audit.build_audit()`, step 4).

**Not in Step 1 (by design):** health endpoints and tracing/metrics export (need apps), `apps/*` entrypoints, per-tenant job caps (ADR-011, step 6).

## 8. Step 2: identity (implemented, ADR-035)
`identity` answers WHO: a tenant-scoped `Principal` (`user` | `service`; `active` | `disabled`) with `RegisterPrincipal`,
`DisablePrincipal`, `EnablePrincipal` and the query `GetPrincipal`. It has no dependencies, owns schema `identity`
(forced RLS), and holds no credentials, permissions or authorization logic (`access_control` decides WHAT, step 3).
Public surface and rules: `src/lda/modules/identity/README.md` and `api.lock`.

## 9. Step 3: access_control (implemented, ADR-014, ADR-026)
`access_control` answers WHAT: explicit per-principal grants of `(resource, action)` over `ResourceRef(kind, id)`, default deny, no roles or groups.
It owns the resource registry (code-defined, sealed at composition; owning modules register kinds and actions through its API), schema
`access_control` (`grants`, `policy_epochs`; forced RLS; no foreign keys) and the signing capability for `AuthorizationProof`. Use cases:
`GrantAccess`, `RevokeAccess`, `AuthorizeAction` (strict) and `ResolveAccessScope` (filter). A decision returns an `AccessScope` (tenant,
principal, `policy_epoch`, exact resource set) and a 60-second HMAC proof. `policy_epoch` is one monotonic integer per tenant that advances in the
transaction of every grant change; principal status is read live from `identity` on every decision. Proofs are verified at the provider boundary
through the `shared/ports` `ProofVerifier`, implemented by Access Control and injected by `bootstrap`; `ai_gateway` never depends on
`access_control`. There is no HTTP middleware yet. ADR-022 is deferred. Public surface and rules: `src/lda/modules/access_control/README.md` and `api.lock`.

## 10. Step 4: audit (implemented, ADR-019)
`audit` records evidence of effective mutations; it is not logging (ADR-020), not provenance (`GenerationRecord`, ADR-016) and not debugging output. It
depends on nothing and nothing depends on it: producers reach it only through `uow.audit` and the `shared/ports` `AuditRecorder`/`AuditFact`, and
`bootstrap.audit.build_audit()` supplies the `audit_factory` that `build_platform` requires. An `@audited` use case emits **explicitly**:
`uow.audit.record(AuditFact(...))` before `commit()`, with tenant, actor and request id from the execution context and resource/outcome from the use
case; archguard AG056 proves that a declared action is emitted before commit and that `@not_audited` use cases do not emit. There is no wrapper
(ADR-034 stays pure metadata). The write joins the action's own transaction and is fail-closed. Step 4 records `success` facts of effective changes only
(no-ops, failures, denials, authorization decisions and egress are not recorded; they come with the API layer and `ai_gateway`). `audit.records` (schema
`audit`) is insert-only: the runtime role holds `INSERT` only, triggers reject `UPDATE`/`DELETE`/`TRUNCATE` for every role, forced RLS, tenant-leading
key, no foreign keys. Facts carry identifiers and short labels only (at most 16 attributes; content-bearing key names are refused). There is no read API,
retention or deletion (ADR-023) and no tamper-evidence chain in v1. Public surface and rules: `src/lda/modules/audit/README.md`.

## 11. Step 5: documents (implemented, ADR-005, ADR-015, ADR-023)
`documents` owns ownership, document/version identity, immutable version metadata, the originals in object storage, and the tombstone/purge boundary. It
imports only `ingestion.api` since Step 6 (`depends_on = ["ingestion"]`; the `access_control` edge stays a permitted ceiling, declared only when imported). A version is
immutable except its status and starts `pending` (never searchable); it leaves `pending` once, to `ready`/`failed`/`quarantined`, applied only by Documents on ingestion's outcome events (section 12); authorization is per document (kind `document`, no per-version
grants). **Upload** is intent, object, rows: a durable `upload_intents` row plus a delayed job commit *before* the first byte, so an orphan object is always
findable from the database and removed by `documents:upload_intent` (deterministic, no bucket listing, no manual step). **Delete** is one transaction: tombstone,
removal of every grant on the document with exactly one policy-epoch advance (through the shared `ResourceAccessRevoker` port, implemented by Access Control on
the caller's transaction and wired by bootstrap, like `AuditRecorder`), purge job, `DocumentDeleted`, and the audit fact; if any part fails nothing commits.
**Purge** (`documents:purge`, idempotent) deletes the originals, verifies they are absent, removes the rows, writes an insert-only receipt whose scope is exactly
`documents.originals_and_rows`, audits and publishes `DocumentPurged`. Downstream modules must add their own handling of `DocumentDeleted`/`DocumentPurged` and
their own receipts; Step 5 does not wait for them. Audit records are identifier-only and are never touched by a purge. Crypto-shredding is deferred (residual risk:
bucket versioning and backups). Public surface and rules: `src/lda/modules/documents/README.md`.


## 12. Step 6: ingestion (implemented, ADR-010, ADR-011, ADR-036; ADR-012/015/020/023 amended)
`ingestion` is the engine that moves a document version from `pending` to a terminal outcome as at-least-once, tenant-bound jobs on the Postgres queue. It imports
**no module** (`depends_on = []`); the DAG edges point into it (`documents`, and later `document_processing`, `chunking`, `retrieval`, which implement `PipelineStage`).
It does not parse, OCR, normalize, chunk, embed, index, retrieve or generate. Ownership: Documents owns the version lifecycle and its database-enforced status
(`pending -> ready | failed | quarantined`, migration `d0002`); ingestion owns runs and stage runs (schema `ingestion`, migration `g0001`, forced RLS, no foreign key outside
the schema, lifecycle triggers). **Flow:** `DocumentVersionAdded` -> a Documents handler enqueues `ingestion:run` (ids and generation only; the tenant comes from the job
row) -> `ExecuteRun` runs the frozen plan (`scan`, then the configured stages, in order; an empty plan is rejected at composition) in two short transactions per stage -> the
terminal transition publishes `IngestionCompleted | IngestionFailed | IngestionQuarantined` in the same transaction -> Documents applies it (compare-and-set on `pending`, audited,
idempotent) and acknowledges it. `ready` means scan clean **and** every stage of the plan succeeded. A permanent failure fails the version; a scan hit quarantines it
(ADR-036); exhausted retries dead-letter the **run** and leave the version `pending` until an explicit, audited replay creates the next generation. **Fencing (ADR-012
amendment):** `platform.jobs.fencing_token` increases on every claim and reclaim, every lease operation requires it, and `JobFence.verify` (a `FOR SHARE` check inside the
effect transaction) stops a worker that lost its lease from committing. **Recovery:** a delayed per-run watchdog dead-letters a stalled run and re-publishes an
unacknowledged outcome (bounded); the worker loop reclaims expired leases (ADR-011) and may cap running jobs per tenant per queue. Metrics are SQL-backed
(`GetIngestionMetrics`) plus the queue's own counters; no exporter. Rules and limits: `src/lda/modules/ingestion/README.md`.

## 13. Step 9: ai_gateway (implemented, ADR-006; ADR-026 §9 satisfied)
`ai_gateway` is the only way text leaves the system for an LLM provider. It imports **no module** (`depends_on = []`), owns no schema and persists, caches and logs nothing.
Its single use case, `GenerateText`, validates the request, derives the exact resource set from the context segments (each carries a mandatory source resource),
verifies the proof through the shared `ProofVerifier` port (injected by `bootstrap`, never `access_control`), applies a tenant-qualified rate limit and only then calls the
`LlmProvider` port, with a per-attempt timeout and bounded retries for failures that happened before the provider received anything. Verification and rate limiting repeat before
every retry. Every rejection, including an empty scope, means zero provider calls. The result is the completion as `Sensitive` plus content-free `CallEvidence`
(provider, model, parameters, SHA-256 of the canonical request, resource set, attempts). **Scope:** text generation only; Step 9 ships a deterministic fake provider and
a provider contract suite and **no real adapter, SDK, credential or network call**. **Deferred, on purpose:** egress auditing (ADR-019 D3), embeddings (ADR-007), prompts
(built in Step 10, section 14), retrieval, citations and `GenerationRecord` (ADR-016), an in-process circuit breaker, and the Step 12 question whether a contentless evaluation call may ever skip
the proof (until then every call needs one). Rules, codes and limits: `src/lda/modules/ai_gateway/README.md` and ADR-006.

## 14. Step 10: prompts (implemented, ADR-017)
`prompts` renders prompt text deterministically and owns the lifecycle of code-defined, immutable, versioned templates. It imports **no module** (`depends_on = []`; not `access_control`, not `ai_gateway`, not `bootstrap`), owns no schema and persists, caches and logs nothing.
**Interface (`prompts.api`):** `PromptRenderer.render(PromptRequest) -> RenderedPrompt`. A request names a template explicitly (`template_id`, `template_version`), carries the untrusted question and the ordered context (each item a neutral `ProofResource` plus `Sensitive[str]` text). The result keeps the template identity and version, the instructions (template text followed by the labeled, escaped question block) and one rendered part per input in the caller's order, each with its own resource. Every text is `Sensitive`.
**Lifecycle:** templates are code in `domain/catalog/`; a new wording is a new version; there is no database, activation state, tenant-specific template or cache. Step 10 shipped the framework and one **test-only** template reachable only through `prompts.testing`, with an empty production catalog; since Step 14 (2026-10-16, ADR-017 amendment) the production catalog holds exactly one owner-approved template, `grounded_answer` v1, and any other selection fails with `prompt_template_unknown`. Evaluation-gated activation (ADR-030) is later.
**Security constraints:** prompts is authorization-agnostic: a `ProofResource` is provenance, never proof, and the module never creates `AuthorizedContext`, scopes, proofs or `ContextSegment` (AG090 unchanged). The rule that prompt construction accepts only an authorized context is met at the future composition boundary (`grounded_generation`); the real egress gate stays the gateway's `ProofVerifier`. Question and document text are untrusted data inside fixed `[[LDA:...]]` blocks, with the marker escaped reversibly inside them; this is defense in depth, not prompt-injection prevention, and ADR-022 remains deferred. Validation fails closed with the content-free codes `prompt_invalid_input`, `prompt_limit_exceeded`, `prompt_context_empty`, `prompt_template_unknown`; limits apply to the rendered text and are checked against the gateway's at composition (`bootstrap/prompts.py`). Escaping changes the text the gateway hashes.
**Consumers:** `grounded_generation`, `answering` and `evaluation` (the DAG). **Deferred:** production templates, activation, retrieval, citations, `GenerationRecord`, answer schema validation, conversation history, a template fingerprint. Rules and limits: `src/lda/modules/prompts/README.md` and ADR-017; pins: `tests/architecture/test_prompts_boundaries.py`.

## 15. Step 12: evaluation core (implemented, ADR-018, ADR-030)
`evaluation` is the offline, tenant-less evaluation core. It imports **no module** (`depends_on = []`; the DAG edges to `ai_gateway` and `prompts` stay unused), owns no schema and persists, caches and logs nothing. It defines synthetic `EvaluationDataset`s of cases (question as `Sensitive`, expected outcome, expected and forbidden source ids), the `EvaluationTarget` port (`observe(case)` returns an outcome plus source ids, never text), deterministic metrics (`outcome_accuracy`, `retrieval_recall`, `citation_precision`, `citation_recall`, `isolation`), `RagConfiguration` with a deterministic `config_id` (invariant 15), a fail-closed `GatePolicy`/`GateDecision`, and the use case `RunEvaluation`, which returns an `EvaluationReport` of identifiers, counts and scores. A target failure is one errored case with a stable code; the exception is discarded. **The proof rule is unchanged (ADR-006 question settled):** every provider call, including one without document content, needs a valid proof; Step 12 adds no judge and no gateway change. **Deferred to Step 17:** persisted runs, configuration activation bound to a passing report, CI gating, LLM-judged metrics, a real target adapter running as an evaluation principal. Rules and limits: `src/lda/modules/evaluation/README.md`, ADR-018, ADR-030.

## 16. Step 13: citations (implemented, ADR-016)
`citations` (`depends_on = ["access_control", "chunking"]`, schema `citations`, layers `domain`, `application` and `infrastructure`) turns untrusted source claims into an `EvidenceSet` or refuses. `EvidenceBuilder` verifies the proof for `documents.read_document` over exactly the cited documents before touching `chunking.api.ReadChunks`, matches each claim's chunk id, ordinal and `text_hash` to the store (pages come from the store; none are invented; no offsets or version numbers exist), verifies the proof again and issues markers `S1..Sn`. `EvidenceSet.resolve` maps markers to `Citation`s and refuses unknown ones. `GenerationRecord` is a frozen, content-free value (ids, `config_id`, outcome, template/model labels, request SHA-256, evidence fingerprint, citations); `GenerationRecorder` refuses foreign evidence and citations the evidence did not issue and writes through the insert-only `GenerationRecordRepository` port (Postgres adapter with forced RLS and database-enforced insert-only; in-memory twin for tests). A citation is not an authorization: showing or re-reading a source needs a fresh decision over `source_resource(source)`. **Deferred:** record retention/purge (no approved policy; open owner decision). Step 14 added text-bearing evidence (`TextEvidenceBuilder`, ADR-016 amendment 2); answer validation and the generation use case live in `grounded_generation` (section 17). Rules and limits: `src/lda/modules/citations/README.md`, ADR-016; pins: `tests/architecture/test_citations_boundaries.py`.

## 17. Step 14: grounded_generation (implemented and accepted 2026-10-16; ADR-029 Accepted: D1-D8 project-owner-approved, D4 outage semantics and CODEOWNERS-unrouted owner-decided)
`grounded_generation` (`depends_on = ["access_control", "ai_gateway", "prompts", "citations", "documents"]`, no schema, layers `domain`, `application`, `infrastructure`) turns a question plus untrusted source claims into a validated, cited answer, an explicit `INSUFFICIENT_EVIDENCE`, or a recorded `REJECTED`. `GenerateGroundedAnswer`: fresh authorization for exactly `documents.read_document` over exactly the cited documents (all or nothing) -> `TextEvidenceBuilder` (proof, then the lifecycle gate that lets only `ready` versions release text, then chunk store, claim and passage-hash checks; chunk text arrives only through `citations`, ADR-016 amendment 2) -> `PromptRenderer` (question and passages as untrusted blocks) -> a second fresh authorization immediately before egress -> `GenerateText` -> strict answer contract (JSON; one `S<n>` marker and one verbatim quote per statement, quote verified against the cited chunk text) -> one transaction that writes the `GenerationRecord` and its audit fact -> a third fresh authorization before the statements are released. Failures never become answers: provider errors propagate, contract violations are `REJECTED`, a failed record write withholds the answer. No text, quote, prompt or reply is logged, audited, recorded or put in an error. A passing answer proves the cited source exists, was authorized and contains the quoted words; it does not prove entailment. **Approvals:** ADR-029 D1-D7 (2026-10-15) and D8 (2026-10-16, the exact `grounded_answer` v1 wording) are project-owner approvals, none by a team; the production catalog serves only that template and any other selection fails closed (`grounded_template_unavailable`). **Deferred:** a real provider adapter, record retention, entailment evaluation (Step 17). Rules and limits: `src/lda/modules/grounded_generation/README.md`, ADR-029; pins: `tests/architecture/test_grounded_generation_boundaries.py`.

## 18. Step 15: conversations (implemented and accepted by the project owner 2026-10-17; ADR-039 Accepted, C1-C7 owner-approved)
`conversations` (`depends_on = ["citations"]`, a subset of the approved `citations` and `documents`; schema `conversations`, layers `domain`, `application`, `infrastructure`) is an owner-scoped, immutable, append-only message log. It stores the user's messages and, for each assistant turn, only the identifier of a `GenerationRecord` (read through `citations.api` and required to belong to the caller's tenant and principal). It stores no answer text, no excerpt, no document or version reference, and generates nothing; its only intended dependent is `answering` (Step 16). Every use case requires a `TenantContext` and the owner's principal; missing, foreign-tenant and not-owned conversations are indistinguishable (`conversation_not_found`). Messages cannot be edited or individually deleted (PostgreSQL triggers and grants, not only application code); user messages are idempotent per `client_message_id` and assistant turns per record id, enforced by unique constraints and a row lock. `DeleteConversation` hard-deletes the conversation and its messages with a content-free receipt and an audit fact in one bounded transaction (at most 1,000 messages); it touches no generation record, audit history or identity record and is neither time-based retention nor comprehensive erasure. Live principal-status checks belong to the API boundary. `answering` will call `AppendUserMessage` then `AppendAssistantTurn`.

## 19. Step 16: answering (implemented, validated and accepted by the project owner 2026-10-18; ADR-040 Accepted, Q1-Q4 owner-approved 2026-10-18)
`answering` (`depends_on = ["retrieval", "grounded_generation", "conversations", "access_control"]`, a subset of its six approved DAG edges; no schema, layers `domain` and `application`) owns one use case, `Answer`. It validates a question and a caller-supplied, bounded set of candidate documents, asks Access Control for one decision over exactly those documents, appends the user message, searches the granted subset, builds claims through the neutral `ClaimReference` (added additively to `grounded_generation.api`, so no `citations` edge exists), calls `GenerateGroundedAnswer`, appends the assistant turn that references the generation record, and only then returns the validated statements. The answer text is returned once and stored nowhere (ADR-039 C1). The three writes are three transactions in three modules; partial-failure behaviour (user message kept, answer withheld on a failed link, orphan records, no exactly-once generation) is explicit in ADR-040 D5. It is an application use case: no HTTP surface exists, and authentication and disabled-principal rejection stay with the outer boundary. `config_id` is operator-declared and not a computed fingerprint (Step 17). See `src/lda/modules/answering/README.md`.

## 20. Step 17: evaluation persistence, configuration identity and release eligibility (implemented, validated locally and accepted by the project owner 2026-10-18; ADR-041 Accepted; thresholds provisional)
`evaluation` (`depends_on = []`, DAG ceiling `ai_gateway` and `prompts` unchanged; schema `evaluation`; layers `domain`, `application`, `infrastructure`) keeps its pure core and gains a persistence adapter confined to `infrastructure/persistence`. The configuration identity is a typed six-component `EffectiveConfiguration` computed at the composition boundary (`bootstrap/rag_configuration.py`, the only place that reads other modules' settings for it) and passed to `answering`, which records it on every generation; a declared id must equal it or start-up fails. Run records and eligibility records live in two global, insert-only tables (database-checked hash and column integrity, a composite foreign key from eligibility to a passing run, triggers rejecting UPDATE/DELETE/TRUNCATE for every role). Eligibility is fail-closed and recomputed on every check; it deploys nothing. No new DAG edge, no new dependency. Limitations: see ADR-041 (deterministic doubles only, no real provider/CI/production evidence; metrics see identifiers not text; no evaluation-writer role; no retention).

## 21. Step 18: hardening and recorded retention limits (implemented and locally validated; ADR-042 **Accepted** 2026-10-18; D1-D5 owner-decided 2026-10-18)
Policy-neutral hardening only. As an owner-authorized, deliberate exception to frozen Steps 5, 7, 8 and 11 (external-deployment compatibility unverified), `bootstrap/container.require_queues` makes the Documents, Document Processing, Chunking and Retrieval compositions fail at start-up when a queue they enqueue onto is not in `LDA_JOBS__QUEUES` (previously the jobs stranded silently). `JobMaintenance` gains read-only `dead_letter_summary` and `overdue_lease_count`. Recorded, not changed: generation records and citations are retained and immutable (D1); job rows are retained indefinitely (D2); replay is an internal primitive with no authorization or audit (D3); legal holds are deferred (D4); `config_id` omits the embedder model and chunker version (D5). No schema, migration, `api.lock`, DAG edge or `CODEOWNERS` change. No worker entry point, scheduler, alerting or deployment gating exists. See ADR-042 and `HANDOFF.md`.
