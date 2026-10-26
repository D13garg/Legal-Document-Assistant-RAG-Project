# ADR-042: Step 18 hardening: fail-fast queue configuration, operator visibility, and recorded retention limits

## Status
Accepted

## Context
Approval state (2026-10-18): the sole project owner decided **D1-D5** for Step 18 in conversation and authorized the policy-neutral work. That is a project-owner decision record only; it is not a team approval, a code-owner review, a CI run or a production-readiness approval. **Accepted by the sole project owner on 2026-10-18**, including the documented exception to frozen behavior for Steps 5, 7, 8 and 11 and the D1-D5 decisions as recorded. Acceptance is a project-owner decision record only (conversation record): not a team approval, a code-owner review, a CI run, or a production-readiness approval, and it does not change any limitation or verification status recorded below.

Steps 0-17 are accepted and frozen. ADR-016 (amendment 6), ADR-023, ADR-010, ADR-039 and ADR-041 left retention, record purge, job-row retention, orphan handling and operational alerting open for Step 18. ADR-005 and ADR-023 give operators guidance that depends on telling dead job types apart.

## Problem
Step 18 had two jobs: (1) establish from the checkpoint which reported gaps are real defects, and fix the ones that are policy-neutral; (2) record, without inventing policy, what stays retained, undeleted or unenforced.

## Decision
**Owner decisions (2026-10-18), applied as given and not reopened:**
1. **D1, generation records and citations: keep immutable and retained.** No purge path, no time-based retention, no relaxation of insert-only protections or runtime privileges. Generation-record identifiers and hashes can persist after document purge or conversation deletion. Comprehensive erasure is **not** claimed.
2. **D2, job rows: keep succeeded and dead `platform.jobs` rows indefinitely.** No deletion, retention duration or scheduler; the runtime role keeps SELECT, INSERT, UPDATE only. Indefinite accumulation is a known operational limitation.
3. **D3, replay: remains an internal platform primitive** (`JobMaintenance.replay`). No operator API, no `ReplayJob` use case, no operator principal, no authorization or audit workflow. Replay authorization and auditing must be designed before any externally reachable replay exists.
4. **D4, legal holds: deferred.** No hold model, exception or deletion override. Nothing here authorizes destructive retention or erasure.
5. **D5, configuration identity: unchanged.** The embedder model and a chunker version are not in `config_id`; no eligibility record is invalidated, rewritten or recomputed. Adding either later changes every `config_id` and so orphans all recorded eligibility; that needs its own authorization.

**Implemented (policy-neutral):**
6. **Fail-fast queue configuration.** `bootstrap/container.require_queues(platform, *queues)` raises `ConfigurationError("<queue>_queue_not_configured")`. `build_docs` requires `documents` and `events`; `build_processing`, `build_chunking` and `build_index` require `events`. `build_ingestion` already did this for `ingestion` and is unchanged. Rationale: `JobWorker` claims only configured queues, so a job on any other queue is never claimed and never dead-lettered. Reproduced before the change: three builders returned normally with `events` absent (`DID NOT RAISE`), and `build_docs` had no queue check.
7. **Read-only visibility.** `JobMaintenance.dead_letter_summary()` (queue, job type, last error code, count, oldest age) and `overdue_lease_count()`. They read through the existing system-queue scope, write nothing, and expose no tenant id and no payload. They make ADR-005 and ADR-023 operator guidance actionable (a dead `documents:purge` job and a dead `documents:upload_intent` job share a queue and were indistinguishable in `metrics()`). Nothing alerts on them.
8. **Regression and characterization tests** (section "Testing and verification evidence").

**Not implemented, by decision or by scope:** generation-record or conversation-linked erasure, job-row deletion, time-based retention, legal holds, an operator replay API, a `config_id` change, a worker or application entry point, a scheduler, an exporter, a dashboard, alerting, deployment gating, an evaluation-writer role, backup or object-version expiry.

## Authorized exception to frozen behavior (Steps 5, 7, 8 and 11)
**This is the only intentional change to frozen-step behavior within the Step 18 scope accepted above.** (A second, separately authorized defect correction made after acceptance is recorded under "Post-acceptance defect correction"; it does not alter this exception, D1-D5 or this ADR's status.) It is a deliberate exception to the Steps 5, 7, 8 and 11 rule that frozen behavior is preserved, made under the Step 18 authorization to harden "required queue configuration and handler registration". The project owner authorized it as a documented exception on 2026-10-18 (sole-owner conversation record; not a team approval or code-owner review, and ADR-042 was then **Proposed**; it is now **Accepted**, see Status and Context).

- **Exact files:** `src/lda/bootstrap/container.py` (adds `require_queues`), `documents.py`, `processing.py`, `chunking.py`, `retrieval.py` (each calls it first in its `build_*` function). No other frozen file changes behavior.
- **Previous behavior:** these builders accepted any `LDA_JOBS__QUEUES`. The queue requirement was documented in docstrings only.
- **Failure mode prevented:** `JobWorker` claims only queues named in `LDA_JOBS__QUEUES`. A job enqueued on a queue that is not listed is never claimed and never dead-lettered, so it sits `queued` forever with no error and no signal. For `events` that strands every `event:*` job: the `DocumentPurged` fan-out to document_processing, chunking, retrieval and ingestion cleanup, the pipeline start, and the outcome handlers. For `documents` it strands document purge and upload-intent orphan recovery. The result is deleted documents that are never purged and derived data that is never removed, with nothing reporting it.
- **New behavior:** start-up raises `ConfigurationError("<queue>_queue_not_configured")`. The code names the queue only, never an environment value.
| Builder (frozen step) | Queues it now requires in `LDA_JOBS__QUEUES` | Error code if missing |
|---|---|---|
| `build_docs` (Step 5, Documents) | `documents` and `events` | `documents_queue_not_configured`, `events_queue_not_configured` |
| `build_processing` (Step 7, Document Processing) | `events` | `events_queue_not_configured` |
| `build_chunking` (Step 8, Chunking) | `events` | `events_queue_not_configured` |
| `build_index` (Step 11, Retrieval) | `events` | `events_queue_not_configured` |
| `build_ingestion` (unchanged, already enforced) | `ingestion` | `ingestion_queue_not_configured` |

A deployment composing all of these therefore needs `documents`, `events` and `ingestion`. The check is on configuration presence, not on whether a worker for that queue is running.
- **Compatibility:** every composition in this repository's tests already configures the required queues, and the 3,820 baseline tests plus the 14 new ones pass locally. **Compatibility of external or existing deployments has not been verified**: this repository contains no deployment configuration, and none was inspected or tested. Any deployment whose `LDA_JOBS__QUEUES` omits a required queue will now fail at start-up.
- **Rollback:** revert the `require_queues` calls. No data, schema or API is involved.
- **Tests:** `tests/platform/test_queue_configuration_required.py` (5 cases, reproduced red before the fix).

## Rationale
Where a gap had a policy-neutral fix that matches an existing precedent (`build_ingestion`'s queue check), it was fixed. Where the owner chose to keep a limitation, the limitation is recorded precisely rather than described as solved. Where tests showed the reported gap did not exist, that is recorded as a verified non-defect, not a fix.

## Alternatives considered
- A queue check inside the platform worker (it would have to know module queue names; rejected: composition knows them).
- Deleting succeeded job rows after N days, or purging generation records on `DocumentPurged` (offered as D2-B and D1-B; not chosen).
- A `ReplayJob` use case (D3-B; not chosen).
- Putting the embedder and chunker version in `config_id` (D5; not chosen).

## Consequences
A deployment whose `LDA_JOBS__QUEUES` omits `documents` or `events` now **fails at start-up** where it previously started and stranded work silently. This is a deliberate behaviour change to the bootstrap of frozen Steps 5, 7, 8 and 11. Every composition in the repository's tests already configures both queues. A deployment that runs only some modules must still list the queues its enqueueing modules need, even if it does not run a worker for them in that process.

## Security implications
No schema, grant, RLS policy, trigger, API lock or DAG edge changed. Append-only and insert-only protections are untouched. The new methods are read-only; they run inside a rolled-back system-queue transaction and return identifiers, codes and counts. Error codes name a queue, never an environment value. Replay remains unauthenticated and unaudited at the platform layer: anything holding runtime database credentials can replay a dead job, and anything holding them can read these summaries.

## Operational implications
Operators can distinguish dead job types and detect expired leases that nothing reclaims, but **no process reads these values**. There is still no worker or application entry point in the repository (`apps/` is empty), so nothing in the repository runs `JobWorker` or `reclaim_expired`. Dead `documents:purge` jobs still mean a deleted document is not purged; dead `documents:upload_intent` jobs still mean an orphan object may remain.

## Migration implications
None. No migration was added or changed.

## Testing and verification evidence
Local only: embedded PostgreSQL 16 via `pgserver` 0.1.4, Python 3.12.3, **not** a CI service container; CI has not run. Baseline `make check` on the untouched Step 17 archive: exit 0, 3,820 passed. Final `make check`: exit 0, **3,834 passed** (+14, all new; no existing test changed, removed, skipped or weakened); ruff format and lint clean; mypy clean (676 and 668 files); `archguard check` OK; import-linter 65 kept, 0 broken; `gen-importlinter --check` and `api-lock` OK.

| New test file | Tests | What it establishes |
|---|---|---|
| `tests/platform/test_queue_configuration_required.py` | 5 | **Defect reproduced, then fixed.** Red first: three builders `DID NOT RAISE` with `events` absent. Now each refuses a missing queue with its code. |
| `tests/platform/test_purge_recovery_hardening.py` | 2 | Real PostgreSQL. A failure in the purge's final transaction after the originals were deleted rolls back receipt, row removal and audit fact together; a retry completes; a further redelivery is a no-op. Two concurrent purge workers converge on one receipt and one audit fact. **Not a defect; passed on first run.** |
| `tests/platform/test_ingestion_after_purge.py` | 1 | Real PostgreSQL. An `ingestion:run` job claimed after its document was purged leaves no ingestion rows, revives no version, records no outcome, does not dead-letter, and its delayed watchdog is a no-op. **Not a defect.** The end state is pinned; the intermediate "run recreated" step ADR-010 describes is not separately asserted. |
| `tests/platform/test_config_identity_limitations.py` | 2 | Characterization pin (D5): the exact identity parameter set, and the absence of any embedder or chunker-version parameter. |
| `tests/platform/test_job_attention.py` | 4 | Real PostgreSQL. Dead-letter grouping by type, read-only, no payload or tenant, empty case, overdue-lease counting. Red first (methods absent). |

**Not independently verified or not changed:** upload-intent crash points, stale-lease reclaim and the retry/dead-letter paths already had real-PostgreSQL or application tests in the checkpoint (`test_documents_bootstrap.py`, `test_reaper_and_jobs.py`, `test_job_queue*.py`, `test_ingestion_end_to_end.py`); they were read and left as they are, not re-proved here. The ADR-005 residual (a `put` slower than the grace period can land after the intent job finished) was **not** tested and remains. `adr-gate` was **not run** (no git history in the archive). No mutation framework was run; the only red-first evidence is the two reproductions above. The store is an in-memory double throughout, so nothing here shows a real object store behaves the same way.

## Post-acceptance defect correction: evaluation store duplicate-write handling
ADR-042 stays **Accepted**; D1-D5 and the startup-validation exception are unchanged. Authorized by the sole project owner (conversation record, 2026-10-19; not a team approval or code-owner review) as a narrowly scoped defect correction after ADR-042 was accepted. The full record is in the ADR-041 amendment; summary: a pre-existing race (reproduced on the untouched Step 17 archive) made concurrent duplicate `append_run` calls occasionally fail as `PERSISTENCE_FAILED` instead of `ConflictError`, because the `ON CONFLICT` arbiter named only `run_id`; the eligibility insert had the same flaw for concurrent same-`eligibility_id` retries (confirmed, about 0.1% of synchronized rounds, not reachable through the production use case). Fix: `on_conflict_do_nothing()` without an arbiter in two places in `store.py`; no schema, grant, RLS, migration, API-lock or DAG change. A follow-up correction (same authorization) fixed an eligibility edge case the first version missed: an `eligibility_id` owned by a different run was reported as a replay when the requested run already had its own record; the read-back now distinguishes the cases (see the ADR-041 follow-up). A second follow-up (same authorization) made the read-back verify the run's identity, `(run_id, record_sha256)`, so a matching `run_id` with a different hash is no longer returned as a replay (see the second follow-up in ADR-041). Tests: 15 new in `test_conflict_handling.py`. Local result: `make check` exit 0, **3,849 passed** (3,834 + 15). CI, `adr-gate` and mutation testing were **not run**; the statistical tests are evidence, not proof. This does not claim production readiness.

## Known limitations
- Generation records, citations and their identifiers or hashes persist after document purge and conversation deletion (D1). No comprehensive-erasure claim.
- Job rows accumulate indefinitely (D2). Audit records are never deleted (ADR-019). Evaluation runs and eligibility are never deleted (ADR-041).
- `replay` is unauthenticated and unaudited at the platform layer (D3); there is no operator API.
- No worker or application entry point exists; nothing in the repository runs the worker or reclaim loop. Handlers are exposed per module and aggregated by nobody.
- Visibility is queryable only; no alerting, exporter or dashboard.
- `config_id` omits the embedder model and chunker version (D5), so two deployments with different embedders or chunkers can share an id and an eligibility record.
- Evaluation eligibility is recorded and enforced by nothing at deployment; no evaluation-writer role; deterministic doubles only; 14 synthetic cases.
- Backups, bucket versioning and replication are outside what any receipt proves (ADR-023 §8).
- Legal holds are deferred (D4).
- Unchanged from earlier steps: answer text is not stored, generation is not exactly-once, no HTTP layer, candidates caller-supplied, no CI, real-provider, load, fuzz or penetration evidence.

## Deferred concerns
A retention policy for generation records and job rows; legal holds; an operator replay API with authorization and audit; adding the embedder and chunker version to the identity (invalidates recorded eligibility); a worker entry point and scheduler; alerting; deployment enforcement of eligibility; an evaluation-writer role.

## Owner decision record
2026-10-18, sole project owner: D1 Decision A, D2 Decision A, D3 Decision A, D4 Defer, D5 Decision No; authorization to proceed with policy-neutral work only. ADR-042 was accepted by the project owner on 2026-10-18 (no change to D1-D5).

