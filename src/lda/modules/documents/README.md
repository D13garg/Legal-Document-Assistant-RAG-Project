# documents

> Ownership, immutable versions, original objects and the tombstone/purge boundary (ADR-005, ADR-015, ADR-023).

## Responsibility
Owns `Document` and `DocumentVersion` identity, each version's immutable metadata (number, media type, size, creator, time, status), the originals in object
storage, the lifecycle skeleton, and deletion: tombstone then purge. It does **not** ingest, parse, chunk, embed, search or cite, and it has no content read path.

## Invariants
- Versions are immutable (database trigger); a new upload is the next version. A new version is `pending` and never searchable; `ready`/`failed` belong to
  ingestion (Step 6). Authorization is per document (kind `document`); there are no per-version grants.
- Upload is intent, object, rows (ADR-005). A durable intent and a delayed `documents:upload_intent` job exist before the first byte; an orphan object is removed
  by that job once the grace has elapsed, verified absent, with the intent row locked so a slow upload and the cleanup cannot both win.
- `DeleteDocument` is ONE transaction: tombstone (irreversible), removal of every grant on the document with a single policy-epoch advance, purge job,
  `DocumentDeleted`, audit fact. Failure of any step (including audit or the revoker) commits nothing. Deleting twice is a silent no-op.
- `PurgeDocument` (system job, idempotent) deletes the originals, verifies absence, removes the rows and writes an insert-only receipt whose scope is exactly
  `documents.originals_and_rows`. It claims nothing about chunks, embeddings, search projections, caches or backups; later modules add their own.
- Identifiers only in events, audit facts, jobs and receipts: never a title, media-type-derived name or byte. Titles live only in `documents.documents`.
- Tenant isolation: tenant-bound repositories, forced RLS, tenant-leading keys, no cross-schema FK or read. The runtime role cannot update versions or receipts.

## Public API (`api/`)
Use cases `UploadDocument`, `AddDocumentVersion`, `DeleteDocument`; queries `GetDocument`, `VerifyDocumentPurge`; system jobs `PurgeDocument`,
`ReapAbandonedUpload` and their handlers; commands/views; events `DocumentVersionAdded`, `DocumentDeleted`, `DocumentPurged`; constants for the resource
kind/actions and the job names. Snapshot: `api.lock`. `@requires`: `documents.upload_document`, `documents.add_document_version`, `documents.delete_document`,
`documents.read_document`, `documents.verify_purge` (enforced only at the API layer, Step 16; creation and verification have no resource kind yet).

## Dependencies
None (`depends_on = []`). Wiring is in `lda.bootstrap.documents`: `register_doc_resources(registry)` BEFORE `build_access_control`, then `build_docs(platform,
access_control, settings)`; pass `DOCUMENTS_MIGRATIONS` to `migrate.upgrade`; register `container.job_handlers` with the worker; configure the `documents` queue in
`LDA_JOBS__QUEUES` and optionally the `documents` slice (`max_upload_bytes`, `allowed_media_types`, `upload_intent_grace_seconds` >= 300).

## Owned data
Schema `documents`: `documents`, `versions`, `upload_intents`, `purge_receipts` (forced RLS; guard triggers; receipts insert-only for every role).

## Events in / out
Out: `documents.DocumentVersionAdded`, `documents.DocumentDeleted`, `documents.DocumentPurged` (identifiers and counts, `ASYNC_JOB`, no handler yet). In: none.

## Must NOT know about
Other modules, ingestion/parsing/retrieval/AI, how grants are stored (only the `ResourceAccessRevoker` port), audit storage, presigned URLs, content.

## Testing in isolation
Unit (domain, config, events), a repository contract run on the in-memory and SQL units of work, Postgres integration (RLS, grants, triggers, row locks), application
tests with fakes (every crash point of the upload protocol, atomic delete, purge, verify, job handlers) and `tests/platform/test_documents_bootstrap.py`
(real Identity, Access Control, Audit and Documents on Postgres).


## Step 6: version outcomes and the ingestion edge (ADR-010, ADR-015 amendment)

Documents stays the **authority over its own version status**: `pending -> ready | failed |
quarantined`, each terminal, enforced by `RecordVersionOutcome` *and* by migration `d0002` (CHECK,
trigger, column-level `UPDATE (status)` grant; a version of a tombstoned document can never become
`ready`; every other column stays immutable).

* `documents.depends_on = ["ingestion"]`: Documents imports only `ingestion.api`.
* Handlers (all `ASYNC_JOB`, idempotent, registered by bootstrap): a new version enqueues one
  ingestion job (`StartPipelineOnVersionAdded`); `RecordReady/Failed/QuarantinedOn...` apply the
  outcome event, audit it (`documents.version_outcome_recorded`, identifiers only) and acknowledge
  it to ingestion; `DocumentDeleted` cancels, `DocumentPurged` clears ingestion-owned state.
* A dead-lettered ingestion run leaves the version `pending`. Replay never resets a
  `failed`/`quarantined` version. `ready` is necessary, not sufficient, for search (Step 11).
