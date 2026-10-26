# ADR-003: PostgreSQL as transactional source of truth

## Status
Accepted

## Context
One transactional store for ACLs, metadata, jobs, chunks and vectors (ADR-004 later).

## Problem
Which database and access stack for v1.

## Decision
PostgreSQL 16 is the source of truth. SQLAlchemy 2 (sync) with psycopg 3 is the access stack; Alembic owns migrations (ADR-024). Sync was chosen because FastAPI runs sync endpoints in a threadpool and RLS per-transaction settings are simplest without async session juggling.

## Rationale
Single atomic boundary for authorization + data; mature RLS; no second system for the queue (ADR-012).

## Alternatives considered
Async SQLAlchemy (more moving parts, no v1 need); MySQL (weaker RLS); document stores (no RLS, weaker transactions).

## Consequences
Connection pool sizing and statement timeouts are configuration (DatabaseSettings). Switching to async later is contained in platform/database.

## Security implications
Runtime role is not the owner and has no BYPASSRLS (tested).

## Operational implications
Needs a Postgres service in CI; local dev uses compose or an embedded server.

## Migration implications
Alembic from day one.
