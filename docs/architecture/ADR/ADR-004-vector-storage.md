# ADR-004: Vector storage: pgvector inside PostgreSQL, exact search, no stored text

## Status
Accepted

## Context
Step 11 adds `retrieval`, the owner of the search/index projection. ADR-003 makes PostgreSQL the transactional source of truth, ADR-013 and ADR-027 fix shared-schema multi-tenancy with forced RLS and one schema per module, ADR-032 and ADR-038 §8-§9 forbid content caches and chunk text in Postgres, and ADR-023 requires every downstream module to purge its own derived data with its own receipt. The environment already runs the Postgres used by every other module.

## Problem
Choose where vectors live, how they are searched under a per-query authorization scope, and what the index may store, without introducing a second datastore, a second tenancy model or a copy of content.

## Decision
1. **pgvector in the existing PostgreSQL.** One extension (`vector`, created in `public` by migration `l0001`, which needs a role allowed to create it; the runtime role never creates it). Every statement schema-qualifies the type and operators (`public.vector`, `OPERATOR(public.<=>)`), so no role needs `public` on its search path. Rejected: a dedicated vector database (a second tenancy and RLS model, a second purge target, no transactional purge with the lifecycle lock, another operational dependency for a v1 corpus of per-tenant legal documents).
2. **Fixed shape.** `retrieval.chunk_vectors.embedding` is `public.vector(256)`; the metric is cosine (`<=>`), vectors are L2-normalized and validated at the boundary (finite, unit length within 1e-3, exactly 256 numbers), `similarity = 1 - distance`. Another dimension is a new migration and a new index identity (ADR-007), never an insert of a different size (a database test pins this).
3. **Exact search, no ANN index in v1.** The query filters by tenant (predicate and RLS), index identity and the exact set of authorized documents, then orders by distance (ties by chunk id, so paging is stable). An approximate index (HNSW, IVFFlat) combined with a selective, per-query filter loses recall unpredictably and would make "the highest-numbered ready version" results depend on the index's tuning. Exact search scans only the rows of the scoped documents. This is a performance limit, not a correctness one; adding an approximate index needs its own ADR with measured recall under the real scope sizes.
4. **What the tables hold.** `index_versions` (tenant, version, index id, document, the embedding identity's parts, chunker version, the chunk set's content hash, chunk count, creation time), `chunk_vectors` (tenant, index id, chunk id, document, version, ordinal, the chunk's SHA-256 `text_hash`, the vector) and `purge_receipts`. No chunk text, no snippet, no tsvector, no copy of any permission, no status or version number (Documents answers those live; ADR-008). The `text_hash` is a content-derived fingerprint, stored so a consumer that re-reads the text through `chunking.api.ReadChunks` can verify it is the text that was indexed; it is not reversible to prose but is not secret from someone who can guess a whole chunk, so it is treated as sensitive as the vector and kept out of logs, errors, metrics, traces and audit.
5. **Vectors are content-derived.** Embeddings can leak information about their source text. They get the same handling as content: tenant-bound rows under forced RLS, never in logs, errors, jobs, events or audit, and driver errors (which echo statement parameters) are translated to a content-free `retrieval_persistence_failed` with the cause suppressed.
6. **Immutability and lifecycle.** Index rows are immutable for every role (no `UPDATE` grant; a trigger rejects it even for the owner). The runtime role may `INSERT`, `SELECT` and `DELETE` index rows and only `INSERT`/`SELECT` receipts (insert-only for every role, `TRUNCATE` blocked). Rows are deleted only by `PurgeRetrievalIndex` (ADR-008).
7. **No cross-schema access.** Migration branch `retrieval`, revision `l0001`, schema `retrieval`, no foreign key out, tenant-leading primary keys on every table.

## Rationale
Keeping vectors in the module's own schema of the existing PostgreSQL gives forced RLS, one transaction per version and per purge, and the existing lifecycle lock, with no second system to secure. Exact search is the only mode whose results do not depend on index tuning when the filter (the authorized document set) is selective.

## Alternatives considered
A dedicated vector database (second tenancy model, second purge target, no shared transaction); pgvector with an HNSW or IVFFlat index now (recall loss under selective filters, unmeasured); storing chunk text beside the vectors for convenience (contradicts ADR-032/038); a separate `tsvector` column (deferred, ADR-009).

## Consequences
- One transaction covers a version's record and all its vectors; one transaction covers a purge. No second store to reconcile.
- Search cost grows with the number of chunk rows in the scoped documents. The 10,000-resource scope cap bounds the filter list, not the row count; a large-scope projection and an approximate index are deferred.
- Backups and replicas hold vectors. A purge receipt therefore claims only the live rows (`retrieval.vector_rows`), never backups (ADR-023).
- Test environments need pgvector; the architecture tests, the contract suite and the platform test run against a real pgvector.

## Security implications
Vectors and text hashes are content-derived and handled as content: forced RLS, tenant-leading keys, no cross-schema access, immutable rows, insert-only receipts, driver errors translated with the cause suppressed. The runtime role cannot create the extension or alter rows.

## Operational implications
Requires pgvector on every environment's Postgres (the dev/test image must include it). Search cost grows with rows in the scoped documents; there is no ANN index to tune or rebuild. Backups and replicas include vectors.

## Migration implications
Migration `l0001` (branch `retrieval`) creates the extension in `public`, the schema, three tables, policies, triggers and grants; downgrade drops the schema and leaves the shared extension. A different dimension or an ANN index needs a new migration and ADR.
