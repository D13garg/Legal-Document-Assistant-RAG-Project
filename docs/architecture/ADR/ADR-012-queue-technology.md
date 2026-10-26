# ADR-012: Postgres-backed queue vs Celery/Redis vs Temporal

## Status
Accepted

## Context
Ingestion and event handling need atomic enqueue, retries, DLQ, recovery, replay, observability.

## Problem
Pick the v1 job mechanism without distributed-system overhead.

## Decision
In-house Postgres queue behind the JobQueue port: `platform.jobs`, `SELECT ... FOR UPDATE SKIP LOCKED` claim, lease + heartbeat, unique keys, retry/backoff, `dead` state, replay, metrics from SQL. Spike result: the Procrastinate candidate uses its own psycopg connections and cannot join a SQLAlchemy transaction, defeating transactional enqueue; the in-house implementation is ~300 lines and fully tested (stalled-job recovery, unique keys, per-queue concurrency, retry semantics, attempt-neutral provider backoff).

## Rationale
Enqueue commits atomically with business state, which removes the outbox/relay/inbox machinery entirely.

## Alternatives considered
Celery+Redis+outbox+inbox (not durable, redelivery traps for long OCR tasks, extra tables); Temporal (heavy ops burden, not needed for a linear pipeline); Procrastinate (see spike).

## Consequences
Queue churn adds bloat (dedicated schema, tune autovacuum). Throughput ceiling is a migration trigger. Temporal adoption triggers: human-in-the-loop/multi-day workflows, fan-out sagas, sustained throughput beyond Postgres, multiple teams needing workflow tooling.

## Security implications
Job rows carry tenant_id (authoritative) and no content. The only cross-tenant path is the RLS system scope for queue maintenance (ADR-013).

## Operational implications
Metrics (depth, oldest age, DLQ) come from SQL (`JobMaintenance.metrics`). Workers scale by queue. Per-tenant concurrency caps and queue-per-concern deployment arrive with ADR-011 (step 6).

## Migration implications
Handlers are idempotent activities, so a move to Temporal is mechanical.

## Amendment (Step 1 hardening)
The worker's heartbeat thread survives a failing renewal (logged as `job_heartbeat_failed`); previously one transient error ended renewal silently, the lease expired and the job could run twice concurrently. Backoff exponent is bounded (no `OverflowError` for very large attempt counts). `JobWorkerSettings` rejects values that make the queue unsafe: no queues, a per-queue concurrency below 1, `heartbeat_seconds >= lease_seconds`, `backoff_cap_seconds < backoff_base_seconds`. Delivery stays at-least-once; handlers must be idempotent.

## Amendment (Step 6, owner decision D5: fencing token)
Step 6 found a real concurrency flaw: `ClaimedJob` carried no fencing token, so a worker that lost its lease (long pause, failed heartbeats, reclaim) could still commit effects and complete a job another worker now owned. The queue contract is extended **additively**:
- `platform.jobs.fencing_token bigint` (migration `p0002`, default 0) increases by one on every claim and on every reclaim; it never decreases (unlike `attempts`, which attempt-neutral retries decrement).
- `ClaimedJob.fencing_token` carries the token of that claim. `JobLeaseStore.heartbeat/complete/retry/fail` require it in addition to the worker id; the SQL gates on `id`, `lease_owner`, `fencing_token` and `status = 'running'`, so a stale worker (even one that restarted with the same worker id) cannot mutate the job and fails closed (`job_lease_lost` is logged by the worker).
- A new shared port `JobFence.verify(job_id, fencing_token)` (implemented by `PostgresJobFence`, exposed as `SqlUnitOfWork.fence`) lets an effect transaction prove, inside itself, that the token is still current: it takes `SELECT ... FOR SHARE` on the job row requiring `status = 'running'`, the same tenant, the same token and an unexpired lease, else raises `ConflictError("job_lease_lost")`. The share lock makes a concurrent reclaim wait until the effect transaction ends, so effects cannot commit after another worker has taken over.
- Handlers still must be idempotent; the fence prevents interleaving, it does not make effects exactly-once. The shared `UnitOfWork` Protocol is unchanged (modules expose `fence` on their own unit of work).
- Per-tenant claim cap and the periodic reclaim tick are specified in ADR-011. Redis/Celery/Temporal remain rejected.
