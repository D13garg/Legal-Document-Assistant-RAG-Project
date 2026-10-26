# ADR-023: Data retention, purge and crypto-shredding

## Status
Accepted

## Context
Invariant 12: deletion is provable (tombstone and ACL revocation atomic, purge receipts, verify). ADR-019 made audit records insert-only, identifiers-only and untouched by purge, and deferred retention and deletion here. ADR-014 gives Access Control sole ownership of grants and the policy epoch; ADR-027 forbids cross-schema writes and foreign keys; each module's unit of work is its own transaction. The platform offers only an optional bucket-wide SSE-KMS key id, no key hierarchy. The HANDOFF notes that succeeded job rows are kept forever and need a retention policy in Step 18.

## Problem
Define document deletion end to end for Step 5 — how the tombstone and the loss of access are one atomic change, how bytes and rows are physically removed, what a receipt proves, and how this coexists with immutable audit — without implementing downstream (not yet existing) stores.

## Decision
1. **Deletion is two phases.** *Tombstone* (`DeleteDocument`, synchronous) then *purge* (`PurgeDocument`, an idempotent job).
2. **Atomic tombstone and revocation.** In **one transaction** `DeleteDocument` sets the document `deleted` (irreversible), removes **every grant on that document** and advances the tenant's policy epoch **exactly once** if any grant was removed (an epoch advance with no grant removed does not happen), audits `documents.document_deleted` (attribute `revoked_grants`), enqueues the purge job and publishes `DocumentDeleted`. Revocation crosses a module boundary through a new shared port `ResourceAccessRevoker.revoke_all(resource_kind, resource_id) -> int`, implemented by Access Control (`SqlResourceAccessRevoker`) **on the caller's transaction** (the same pattern as `AuditRecorder`) and handed to the Documents unit of work by bootstrap. If any step fails, nothing commits: the document stays active with its grants. Access Control gains an additive `GrantRepository.remove_all_for_resource`; its decision logic is untouched.
3. **Tombstoned documents** are unreadable (`GetDocument` raises not found), accept no new version, and are immutable. Deleting a tombstoned or absent document again is a no-op and emits no fact (ADR-019 D5).
4. **Purge (`PurgeDocument`, system context).** For a tombstoned document: delete each recorded original, verify absence with `exists`, then in one transaction delete the version and document rows, insert the receipt, audit `documents.document_purged` (attribute `versions_purged`), and publish `DocumentPurged`. Object deletion is idempotent and verification failures raise, so the job retries; a crash after deleting objects is repaired by the retry. A purge job for an already purged document is a no-op.
5. **Purge receipt** (`documents.purge_receipts`, insert-only, forced RLS, no update/delete/truncate even for the owner): tenant, document id, tombstone time, purge time, the version ids, their count, and the fixed `scope = 'documents.originals_and_rows'`. It records that **the originals and the Documents rows were removed and the originals verified absent**. It deliberately says nothing about chunks, embeddings, search projections, caches, citations, backups or any store owned by another module; those modules must register their own handling of `DocumentDeleted`/`DocumentPurged` and write their own receipts when they exist. Step 5 does **not** wait for them.
6. **Verify.** `VerifyDocumentPurge` reports whether a receipt exists, whether any Documents row still exists, and whether every recorded original is absent. It reads only; it asserts nothing about other modules.
7. **Audit versus erasure.** Audit records (ADR-019) hold only identifiers (document id, actor, counts, labels) and are **retained and untouched by purge**. Titles, filenames, media-type-derived names and bytes are never audited, so purging removes all document content and metadata the module holds while the audit trail keeps an identifier-only history. Limitation: document and principal identifiers persist in audit, so erasure of *those identifiers* (for example a principal's right to erasure) is not possible in v1 and is not claimed.
8. **Crypto-shredding is deferred.** v1 deletion is physical deletion, verification and a receipt. There is no key hierarchy. **Residual risk:** bucket versioning, replication, provider backups and any previous object versions can keep deleted bytes after the purge; deployments must configure lifecycle rules for them and v1 does not prove their removal. The title of this ADR records the deferral.
9. **No time-based retention** in Step 5: documents live until explicitly deleted. Retention of succeeded job rows stays assigned to Step 18.

## Rationale
One transaction for tombstone plus revocation is the only way to keep invariant 12 strong without a window in which a deleted document is still granted or a grant exists to a document that remains readable. Doing physical removal in an idempotent job keeps external I/O out of the transaction and makes failures retryable and visible.

## Alternatives considered
- Eventual revocation by an async job (a window of access after delete): rejected by the owner.
- Documents writing grants directly: forbidden by ADR-027.
- A cross-module transaction object in the platform: unnecessary; the port pattern already exists.
- Crypto-shredding now: new key infrastructure without a requirement from earlier steps.
- Editing or deleting audit rows on purge: contradicts ADR-019.

## Consequences
Access Control changes additively (repository method, SQL adapter, in-memory twin, bootstrap field). Downstream stores added later must hook `DocumentDeleted` and publish their own receipts; until then a document's derived data does not exist. The purge job needs retries and a dead-letter watch.

## Security implications
Revocation and tombstone cannot diverge. No content or titles in events, audit or receipts. Receipts and audit are append-only. Residual backup/versioning exposure is explicit.

## Operational implications
Dead-lettered `documents:purge` jobs mean a deleted document is not yet purged. Lifecycle rules for bucket versioning are a deployment responsibility.

## Migration implications
`d0001` creates `purge_receipts`. No change to `audit`.

## Amendment (Step 6: downstream hooks)
Ingestion is the first downstream module. Documents registers `ASYNC_JOB` handlers that call ingestion's `CancelIngestion` (on `DocumentDeleted`) and `PurgeIngestionRecords` (on `DocumentPurged`). Ingestion deletes only ingestion-owned rows (runs and stage runs: identifiers, statuses, counters, durations) and writes **no receipt**: Documents' receipt still claims only `documents.originals_and_rows`, and nothing may present ingestion's cleanup as "whole document purge complete". Derived data (chunks, embeddings, projections) still does not exist and later modules keep their own purge and receipts.

## Amendment (Step 7: derived data of document_processing)
`document_processing` hooks `DocumentPurged` (`ASYNC_JOB`) with `PurgeExtractions`: it deletes its extracted artifacts, verifies absence, removes its rows and writes its own insert-only receipt (`scope = document_processing.extracted_artifacts_and_rows`). Documents' receipt still claims only the originals and Documents' rows. `DocumentDeleted` needs no action there because no content read path exists in Step 7 (ADR-037 §8). Chunks, embeddings and indexes (Steps 8-11) remain to hook the same events.

## Amendment (Step 18, owner decisions D1, D2, D4, 2026-10-18)
§9's assignment of succeeded-job-row retention to Step 18 is answered *retain*: succeeded and dead `platform.jobs` rows are kept indefinitely, the runtime role keeps no DELETE on that table, and no scheduler or duration is introduced. Generation records are likewise retained (ADR-016 amendment). Legal holds are deferred; no hold model is defined. Nothing in Step 18 adds a purge hook, a receipt or a store to the document purge chain, so no claim of erasure beyond the receipts of Documents, document_processing, chunking and retrieval is made. See ADR-042.
