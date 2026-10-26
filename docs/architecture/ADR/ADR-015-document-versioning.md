# ADR-015: Document versioning

## Status
Accepted

## Context
Invariant 11: document versions are immutable, only `READY` versions are searchable, and the default retrieval scope is the current version. `documents` depends on `access_control` and, at the end of the plan, `ingestion` (module DAG); `ingestion` (Step 6) owns the pipeline that makes a version ready. ADR-014 authorizes `(resource, action)` pairs per principal with no inheritance. No decision fixes document/version identity, numbering, status ownership or what a version's ACL is.

## Problem
Define identity, immutability, numbering and the lifecycle skeleton of documents and versions in Step 5 without implementing ingestion, parsing or search visibility.

## Decision
1. **Identity.** A `Document` is a tenant-scoped aggregate (`DocumentId`) with a title (1-255 printable single-line characters), a creator (`PrincipalId` or none for system work), a creation time, a status (`active` | `deleted`) and `latest_version_number`. A `DocumentVersion` (`DocumentVersionId`) belongs to exactly one document and carries its number, media type, size in bytes, creator, creation time and status.
2. **Immutability.** A version row is never updated in Step 5 (the database trigger rejects changes to every column except `status`, and `status` is constrained to `pending`). A new upload creates the next version; nothing edits an old one. A document's title, creator and creation time never change. The only document changes are `latest_version_number` stepping by exactly 1 under the document's row lock and the one-way `active -> deleted` tombstone.
3. **Numbering and "current".** Numbers start at 1 and are assigned under `SELECT ... FOR UPDATE` on the document row. The *latest* version is the highest number. Which version is searchable and which one retrieval treats as current is decided with the ingestion and retrieval steps; Step 5 stores no pointer and no searchability flag.
4. **Lifecycle skeleton and ownership.** `documents` owns the version record and the status column. A new version is `pending` and is **never searchable**. The transitions to `ready` / `failed` are owned by ingestion (Step 6), which will widen the status CHECK, grant the runtime role `UPDATE (status)` and extend the update trigger in its own migration; Step 5 offers no transition use case and no events beyond `DocumentVersionAdded`.
5. **ACL.** Authorization is per document: resource kind `document` with actions `documents.read_document`, `documents.delete_document` and `documents.add_document_version`, registered through the Access Control registry from bootstrap. **There are no per-version grants**; every version inherits its document's grants. `documents.upload_document` (creating a document has no resource yet) and `documents.verify_purge` are declared by `@requires` only; how creation and verification are authorized is decided with the API layer (Step 16). Step 5 makes no creator grant, so nobody can read a new document until the first-grant bootstrap exists.
6. **Dependencies.** `documents` imports no other module in Step 5 (`depends_on = []`): registration of the resource kind is bootstrap wiring, and revocation goes through the shared `ResourceAccessRevoker` port (ADR-023). The DAG's `documents -> ingestion` and `documents -> access_control` edges remain permitted but are not declared until an import needs them; this keeps the staged plan intact (AG011/AG012).
7. **Events.** Identifier-only `documents.DocumentVersionAdded` (document, version, number), published `ASYNC_JOB` in the upload transaction; no handler exists yet.
8. **Deletion** is document-level (ADR-023); versions are never deleted individually.

## Rationale
Making rows immutable and the next version an insert removes update races and keeps evidence stable for citations. Leaving "current" and searchability to the steps that need them avoids inventing retrieval semantics.

## Alternatives considered
- A mutable `current_version_id` pointer: an extra mutable column and a race with no consumer yet.
- Per-version grants: more rows and revocation surface for no requirement.
- Declaring the `ingestion` edge now: there is no ingestion code to import and AG012 would fail.

## Consequences
Step 6 must migrate the status machinery and decide the ready/failed events. Until the first-grant bootstrap exists no document is readable through authorization. Versions can never be edited or deleted individually.

## Security implications
Tenant isolation and RLS on every table. Titles are stored only in the `documents` schema and never appear in events, audit facts or logs. A tombstoned document is immutable and unreadable.

## Operational implications
None beyond ADR-005. Titles are user data: erasure is handled by purge (ADR-023).

## Migration implications
Migration `d0001`. Later steps extend the status CHECK and grants additively.

## Amendment (Step 6: lifecycle ownership, owner decisions D1-D3)
- §4 said ingestion would widen the status CHECK, grant `UPDATE (status)` and extend the trigger "in its own migration". Ingestion cannot do that (ADR-027, AG034: persistence code references only its own schema; DAG: `ingestion` imports nothing). **Documents owns the status column and its migration:** `d0002` admits `ready`, `failed`, `quarantined`, grants the runtime role `UPDATE (status)` only, and extends the version trigger so that the only allowed status change is `pending -> ready | failed | quarantined` (all terminal), a version of a tombstoned document can never become `ready`, and every other column stays immutable.
- Documents applies a transition only through `RecordVersionOutcome`, triggered by ingestion's outcome events (ADR-010 §7), with a compare-and-set on `pending`. It is audited (`documents.version_outcome_recorded`) in the same transaction.
- `documents.depends_on = ["ingestion"]` (an edge already in the DAG). `access_control` stays undeclared.
- A dead-lettered ingestion run leaves the version `pending`; replay never resets a `failed`/`quarantined` version (ADR-010 §8-9).
- Searchability and "current version" remain decided by retrieval (Step 11). `ready` is necessary, not sufficient.
