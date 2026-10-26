# chunking

> Chunking: deterministic, immutable chunk sets derived from extracted text of pending versions

## Responsibility
Turns the extracted text of a version into ordered, bounded chunks with explicit boundaries and
provenance, as the `chunk` pipeline stage after `extract` (ADR-038). Owns the chunk artifact, its
record and its purge. Does not parse files, embed, index, retrieve or cite.

## Invariants
- Text is read only through `document_processing.api.ReadExtraction`, which asks Documents first
  (active document, version `pending` or `ready`, the caller's tenant). No foreign key layout is known here.
- Deterministic: the same extraction and chunker version give the same bytes. Chunks never exceed
  `max_characters`, lose no text, overlap nothing, and rebuild exactly from their `segments`.
- A recorded chunk set is immutable: a re-run adopts it, keeps it (other chunker version) or refuses a
  conflicting result; it is never overwritten.
- Content lives only in the object store; Postgres holds identifiers, hashes, the chunker version and counts.
- Nothing is recorded after the purge receipt; a late object is removed (per-document advisory lock).
- No citation without provenance: every chunk carries `extraction_hash`, `chunker_version`, ordinal and spans.

## Public API (`api/`)
`ChunkStage`, `ChunkingParameters`, `PurgeChunks`, `PurgeChunksOnDocumentPurged`, `PurgeCommand`,
`PurgeView`, `PERMANENT_CODES`, `PURGE_SCOPE`, `STAGE_NAME`, `ChunkingRejected`. No read path for chunk
text yet (Step 11 decides). Snapshot: `api.lock`.

## Dependencies
`document_processing` (ReadExtraction), `documents` (DocumentPurged), `ingestion` (PipelineStage).

## Owned data
Schema `chunking`: `chunk_sets`, `purge_receipts` (migration `k0001`). Artifact `chunking/<doc>/<version>/chunks`.

## Events in / out
In: `DocumentPurged` (purge + receipt). `DocumentDeleted` is deliberately not handled (ADR-038 §8). Out: none.
