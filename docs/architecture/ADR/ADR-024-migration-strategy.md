# ADR-024: Migration and versioning strategy

## Status
Accepted

## Context
Schema-per-module requires independent but ordered migrations.

## Problem
How migrations are authored, run and verified.

## Decision
Alembic with one branch per module (platform branch label `platform`), run programmatically by `platform.database.migrate` with owner credentials; modules contribute version locations. Runtime never runs DDL. Expand/contract for breaking changes. Migrations must grant to the configurable runtime role.

## Rationale
Branches keep module history independent; owner/runtime separation is required for RLS.

## Alternatives considered
Single linear history (cross-module coupling); ORM autogenerate (hides RLS/policy intent).

## Consequences
Upgrade and downgrade of the platform branch are tested on real Postgres.

## Security implications
Migrations are reviewed SQL; no content.

## Operational implications
CI runs migrate up/down on a Postgres service.

## Migration implications
Per-module branches added in each module's step.
