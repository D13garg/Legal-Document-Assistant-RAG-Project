# ADR-001: Modular monolith vs microservices

## Status
Accepted

## Context
A legal RAG platform needs strict confidentiality, transactional consistency between ACLs, chunks and vectors, and an auditable pipeline. The team has no independent scaling or ownership boundaries yet.

## Problem
Choose the deployment/architecture style for v1 without foreclosing later extraction.

## Decision
One codebase and one database, deployed as `api` and `worker` (same image, scaled independently) plus an `evaluation` runner. Bounded contexts are packages with enforced boundaries (ADR-002, ADR-025).

## Rationale
"Authorize before retrieval" is one SQL statement in one transaction rather than a distributed-consistency problem. Cross-service authorization drift is a data-leak risk with no offsetting benefit at v1 scale. Enforced boundaries keep extraction possible.

## Alternatives considered
- Microservices per feature: rejected (distributed transactions, authz drift, operational cost, no team boundary).
- Unstructured monolith: rejected (coupling erosion; the explicit goal is the opposite).
- Two services (API + AI): rejected for v1; no evidence of differing scaling needs beyond the existing api/worker split.

## Consequences
Single release train; modules share a runtime failure domain (mitigated by separate worker processes, breakers, bulkheads). Extraction later requires replacing in-process `api/` calls with a remote adapter; the authorization proof (ADR-026) already survives a process boundary.

## Security implications
One trust boundary and one authorization authority; fewer network surfaces.

## Operational implications
One image, one migration pipeline, one observability setup.

## Migration implications
Extraction candidates (in order): document_processing, retrieval index, grounded_generation. Triggers: independent scaling, a separate team, or isolation requirements.
