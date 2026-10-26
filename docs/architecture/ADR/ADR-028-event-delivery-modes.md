# ADR-028: Event delivery modes (sync in-txn vs async job)

## Status
Accepted

## Context
Some reactions must be atomic with the action (audit, security projections); others can be eventual.

## Problem
How domain events are delivered without an outbox.

## Decision
EventPublisher offers SYNC_IN_TXN (handlers run inside the publishing transaction; failure aborts it) and ASYNC_JOB (one job per handler enqueued in the same transaction, unique on `(handler, event_id)`; the EventJobHandler decodes the event and runs the handler in its own tenant transaction). Events are flat identifier-only dataclasses with a stable `type_name`; EventCodec supports a small closed set of field types. Publishing an event for another tenant is rejected.

## Rationale
The Postgres queue makes the outbox/relay/inbox unnecessary (ADR-012).

## Alternatives considered
Outbox + relay + inbox; in-process bus only (not durable).

## Consequences
Handlers must be idempotent. Exact-type matching only; wiring is explicit in bootstrap.

## Security implications
Tenant mismatch is a hard error; payloads carry identifiers only.

## Operational implications
Failed async handlers retry with backoff, then dead-letter.

## Migration implications
Adding an event type is additive.
