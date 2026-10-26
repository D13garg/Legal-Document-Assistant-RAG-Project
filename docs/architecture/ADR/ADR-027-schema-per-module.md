# ADR-027: Schema-per-module, no cross-schema reads or FKs

## Status
Accepted

## Context
Module data ownership must be checkable.

## Problem
How data isolation between modules is enforced.

## Decision
Each module owns a Postgres schema equal to its name (`platform` for platform). No cross-schema FKs or reads. Enforced by AG002 (owns_schema == module name), AG034 (persistence code may only reference its own schema, including SQL strings and ForeignKey), and a database introspection test that fails on any cross-schema foreign key.

## Rationale
Ownership becomes mechanical, and a module can be extracted with its schema.

## Alternatives considered
Shared public schema (coupling); convention only (erodes).

## Consequences
Retrieval's projection (ADR-008) consumes published contracts, not other schemas.

## Security implications
Limits blast radius of a mis-scoped query.

## Operational implications
Per-module DB roles remain deferred.

## Migration implications
Extraction moves a schema.
