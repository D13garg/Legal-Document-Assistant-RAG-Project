# retrieval

> Tenant-bound vector index projection and authorized, ranked, provenance-carrying search (Step 11).

## Responsibility
Owns the search/index projection: per-version embedding vectors with stable chunk identifiers, and `SearchIndex`, which returns a ranked list of references (document, version, version number, chunk id, ordinal, score, text hash) for an authorized scope. It never returns or stores chunk text, never builds prompts or context, and is not an authorization proof (ADR-008). Derived and rebuildable: Documents, Chunking and Access Control stay the sources of truth.

## Invariants
- Search verifies the Access Control proof for exactly the supplied scope before anything else, and again before returning (ADR-008 §3); stale, expired, widened or foreign proofs fail closed.
- Only `ready` versions, and per document only the highest-numbered one, are searchable; status and number are asked of Documents on every search, never stored (ADR-008 §5-7).
- No chunk text, no copy of permissions, no cache; vectors are treated as content (ADR-004).
- Writers of derived data take the document's lifecycle lock; the purge leaves an insert-only receipt scoped to the vector rows (ADR-008 §11-12).
- Embedding egress has no approved rule: only the deterministic fake embedder exists (ADR-007 §6).

## Public API (`api/`)
`IndexStage` (pipeline stage `index`, after `chunk`), `SearchIndex`/`SearchQuery`/`SearchResult`/`SearchHit`, `PurgeRetrievalIndex` and its `DocumentPurged` handler, `Embedder`, `EmbeddingIdentity`, `SearchLimits`, error codes. Snapshot: `api.lock`.

## Dependencies
`chunking` (`ReadChunks`), `documents` (`ReadVersionState`, `DocumentPurged`), `access_control` (`AccessScope`), `ingestion` (`PipelineStage` contract). `ai_gateway` is allowed by the DAG but deliberately not used (ADR-007).

## Owned data
Schema `retrieval` (migration branch `retrieval`, revision `l0001`): `index_versions`, `chunk_vectors` (pgvector `vector(256)`), `purge_receipts`. No cross-schema reads or FKs.

## Events in / out
In: `DocumentPurged` (ASYNC_JOB handler `retrieval.purge_index`). Out: none. `DocumentDeleted` is intentionally not handled (ADR-008 §13).
