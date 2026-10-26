# Step 18 release notes (ADR-042 Accepted by the project owner, 2026-10-18; not production-ready)

Local validation only: embedded PostgreSQL via `pgserver`; **CI was not run; `adr-gate` was not run.** Nothing here has been verified against any external or production deployment.

## BREAKING for deployments with missing queue configuration
Start-up now **fails** if `LDA_JOBS__QUEUES` omits a queue that a composed module enqueues onto. Previously it started and silently stranded those jobs (never claimed, never dead-lettered).

| Builder (frozen step) | Queues it now requires in `LDA_JOBS__QUEUES` | Error code if missing |
|---|---|---|
| `build_docs` (Step 5, Documents) | `documents` and `events` | `documents_queue_not_configured`, `events_queue_not_configured` |
| `build_processing` (Step 7, Document Processing) | `events` | `events_queue_not_configured` |
| `build_chunking` (Step 8, Chunking) | `events` | `events_queue_not_configured` |
| `build_index` (Step 11, Retrieval) | `events` | `events_queue_not_configured` |
| `build_ingestion` (unchanged, already enforced) | `ingestion` | `ingestion_queue_not_configured` |

A deployment composing all of these therefore needs `documents`, `events` and `ingestion`. The check is on configuration presence, not on whether a worker for that queue is running.

**Before upgrading:** confirm `LDA_JOBS__QUEUES` lists every queue above for every module you compose. **Compatibility of external or existing deployments is unverified.** This is an authorized, deliberate exception to the frozen behavior of Steps 5, 7, 8 and 11 (ADR-042, "Authorized exception to frozen behavior").

## Added
- `JobMaintenance.dead_letter_summary()` and `overdue_lease_count()`: read-only, no tenant id, no payload. Queryable only: no alerting, exporter, dashboard or worker entry point exists.
- 14 tests (see ADR-042 "Testing and verification evidence").

## Unchanged by decision (D1-D5)
Generation records and citations retained; job rows retained indefinitely; replay is an unauthenticated internal primitive; legal holds deferred; `config_id` still omits the embedder model and chunker version. No erasure completeness is claimed.

## Verification record
`make check` (local, embedded PostgreSQL): exit 0, **3,834 passed**. CI: **not run**. `adr-gate`: **not run**. Mutation testing: **not run**. The ADR-005 slow-`put`-after-intent-completion race: **untested and still present**.

## Fixed after acceptance: evaluation store duplicate-write race
A pre-existing race (present in the Step 17 archive) made concurrent duplicate evaluation-run inserts occasionally fail with `evaluation_persistence_failed` instead of `evaluation_run_exists`, and concurrent same-`eligibility_id` eligibility retries occasionally fail instead of replaying. Fixed in `store.py` (two `ON CONFLICT` clauses). A follow-up fixed an edge case in that fix: an `eligibility_id` that belongs to a different run is now always a failure, even when the requested run already has its own record (previously it could be reported as a replay). A second follow-up made replay verify the run's hash: a matching `run_id` with a different `record_sha256` is now a failure, not a replay. 15 tests added in total. Observable change: losers of a duplicate race now always get `ConflictError`/replay; reusing another run's `eligibility_id` always fails. No schema or migration change. Local verification only: 3,849 passed under `make check` on embedded PostgreSQL; CI, `adr-gate` and mutation testing not run. See the ADR-041 amendment.
