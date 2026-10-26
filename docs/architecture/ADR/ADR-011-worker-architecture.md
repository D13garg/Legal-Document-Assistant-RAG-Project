# ADR-011: Worker architecture

## Status
Accepted

## Context
ADR-012 deferred per-tenant concurrency caps and queue-per-concern deployment to this step. The platform already has `JobWorker` (per-queue concurrency, heartbeat, backoff), `JobLeaseStore` and `JobMaintenance.reclaim_expired`, but nothing calls `reclaim_expired`, `apps/worker` does not exist, and the claim is not tenant-fair.

## Problem
Define the worker loop's behaviour for crash recovery and tenant fairness without new infrastructure or an application entry point.

## Decision
1. **Boundary.** The worker is the existing `JobWorker`; `run_once` is the unit of behaviour. **No `apps/worker` entry point is created in Step 6** (nothing in the architecture requires one before the API layer); composition remains `bootstrap` plus tests.
2. **Periodic reclaim.** `JobWorker` takes an optional `JobMaintenance` and `reclaim_interval_seconds` (setting, default 30, minimum 1). At the start of `run_once`, when the interval has elapsed on the injected clock, it calls `reclaim_expired`. A failure is logged (`job_reclaim_failed`) and never stops the loop. Expired jobs return to the queue or the dead state exactly as ADR-012 defines.
3. **Per-tenant cap.** Setting `max_running_per_tenant` (default none = no cap, ge 1) limits how many jobs of one tenant may be `running` in one queue across all workers. The claim serializes per queue with a transaction-level advisory lock, counts running jobs per tenant and picks the oldest eligible jobs within the cap. Without a cap the existing `SKIP LOCKED` claim is unchanged.
4. **Queues per concern.** Queues remain configuration (`LDA_JOBS__QUEUES`): `events`, `documents`, `ingestion`. A worker process may serve any subset.
5. **Event handlers as jobs.** Bootstrap registers `EventJobHandler` entries (`event:<handler name>`) in the container's `job_handlers`; the `events` queue must be configured.
6. **Known gap kept.** `run_until` still ends if `claim` itself fails (a supervisor restarts it).

## Rationale
The reclaim tick closes the gap in which a crashed worker's job would stay `running` forever; the cap prevents one tenant from starving others; both reuse the Postgres queue.

## Alternatives considered
A separate reclaim process or cron (new moving part, no entry point to host it); a per-tenant queue (unbounded queue count); `SKIP LOCKED` with window functions (not expressible in one statement).

## Consequences
With a cap configured, claims on one queue are serialized for the duration of one short statement. The default (no cap) preserves Step 1 behaviour; production must set a value.

## Security implications
The cap bounds a noisy tenant's share of workers. The claim remains the only cross-tenant read, inside the sanctioned system scope (ADR-013).

## Operational implications
Two settings (`jobs.reclaim_interval_seconds`, `jobs.max_running_per_tenant`). Reclaimed and dead counts are visible through `JobMaintenance.metrics` and the `jobs_reclaimed` log event.

## Migration implications
None (settings only).
