# ADR-032: No content caching in v1

## Status
Accepted

## Context
Caches multiply leakage and staleness risks for confidential content.

## Problem
Whether v1 caches retrieval, answer or document content.

## Decision
No content caches. Redis is used only for rate-limit counters (fail-open to a per-instance limiter, logged). Scope/IdP-metadata caches, if added, are keyed by policy epoch with short TTLs in their own ADR.

## Rationale
Removes a whole leak class; correctness over latency in v1.

## Alternatives considered
Answer/retrieval caches (stale ACL risk).

## Consequences
Rate limiting remains available if Redis is down (degraded, per-instance).

## Security implications
No content in Redis.

## Operational implications
Redis is optional for readiness.

## Migration implications
None.
