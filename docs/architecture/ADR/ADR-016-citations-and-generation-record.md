# ADR-016: Citations and GenerationRecord provenance

## Status
Accepted

## Context
Scope note (Step 13): The decisions-log row said "Planned"; no earlier text existed, so this ADR fixes the scope from invariants 4, 5 and 7, the DAG (`citations` -> `access_control`, `chunking`) and ADR-008 (a `SearchResult` is not authorization). Owner decision at Step 13 (original scope; the Postgres adapter was added later, see the Step 13 completion amendment): pure core plus a persistence port; no Postgres adapter yet.

Invariant 4: no citation without an authorized source in the same generation; excerpts are never persisted. Invariant 5: every AI output has an insert-only `GenerationRecord` carrying `config_id`. Retrieval returns hits (document, version, chunk id, ordinal, `text_hash`) but no authorization; chunking records pages (optional) and no character offsets. `citations` may not import `documents`, `retrieval`, `evaluation`, `prompts` or `ai_gateway`.

## Problem
Make a citation something that can only be derived from real chunk provenance under a valid proof, never from answer text or client-supplied identifiers, and fix the shape of the record that proves what a generation was built from.

## Decision
1. **A citation is a reference, not an excerpt.** `CitationSource` = document id, version id, chunk id, ordinal, `text_hash`, optional page range. No text, section title, offset or version number (chunking records no offsets; the version number cannot be verified through the permitted edges, so none is invented). Pages are `None` when the store recorded none; a half-open or inverted range is refused.
2. **Evidence is built, not claimed.** `EvidenceBuilder.build(ctx, claims, proof_token)`: validate shape and limits; verify the proof (`ProofVerifier`, action `documents.read_document`, exactly the cited documents) BEFORE any chunk store is touched; match every claim to `ReadChunks` provenance (same chunk id, ordinal and hash; pages come from the store); verify the proof again. Any failure refuses the whole set. Purged, deleted, foreign-tenant or ineligible versions all yield `citations_source_unavailable`, so a caller cannot probe which. Chunk text is read by the adapter and dropped at once.
3. **Markers, not identifiers.** The set assigns `S1..Sn`; `EvidenceSet.resolve` maps markers named by validated output to citations. Unknown or malformed markers are refused, repeats count once, an empty result is refused unless the caller allows a non-answer. Anything outside the set cannot be cited.
4. **`GenerationRecord`** is a frozen value: ids, `config_id` (ADR-030 shape), outcome (`answered`, `insufficient_evidence`, `rejected`), template id/version, model id, request SHA-256, evidence fingerprint and the cited `Citation`s. No prompt, answer, question, excerpt or proof. `answered` requires at least one citation; a non-answer carries none. `GenerationRecorder` checks that the evidence belongs to the caller's tenant and principal and that every citation was issued by that evidence, then inserts through `GenerationRecordRepository` (`add`/`get` only: no update, no delete; tenant-bound `get`).
5. **A citation is not an authorization.** Showing a cited source or reading its text again needs a fresh decision covering `source_resource(source)` (Step 14/15). `SearchResult`, citation reference and authorized context stay three different things.
6. **Not use cases.** The builder and recorder have no `execute` and sit outside `use_cases/`; the Step 14 use cases that call them own the `@requires`/`@audited` declarations. No audit exemption is added.
7. **Deferred** (original text: the Postgres adapter, its migration and DB tests are now implemented, see the Step 13 completion amendment; record purge/retention remains undefined). The Postgres adapter for `GenerationRecord` (schema `citations`, forced RLS, insert-only triggers, migration, DB contract tests), purge handling for records (ADR-023: records hold no content), retention of records, the request-hash policy, and any display/re-authorization flow. The in-memory twin is a test fake, not a production store: until the adapter exists, nothing may claim invariant 5 is enforced in storage.

## Rationale
Deriving citations from the chunk store under a proof keeps a model or client from inventing a source; failing closed on the whole set avoids plausible-looking partial evidence; a port keeps persistence an additive decision without touching frozen modules.

## Alternatives considered
- Validate citations against retrieval hits: retrieval is not in the DAG edge set and a hit carries no authorization.
- Store excerpts for display: forbidden by invariant 4.
- Offsets/`version_number` in the source: no verifiable origin through `chunking`.

## Consequences
Builder cost is one chunk-set read per distinct version. `ReadChunks` also serves `pending` versions; callers cite only what retrieval returned (ready versions). Python cannot forbid hand-built `EvidenceSet`s; a pin in `tests/architecture/test_citations_boundaries.py` allows only the builder to construct one in production code.

## Security implications
Errors carry stable codes only; hashes, ids, tokens and text never appear in reprs or messages. `text_hash` and the evidence fingerprint and request hash are content-derived: keep them out of logs and client responses. Tenant isolation comes from tenant-bound `ReadChunks` and tenant-bound record reads.

## Migration implications
(Original text, superseded by the amendment below.) None in Step 13: no schema, table or migration. The deferred Postgres adapter will add a `citations` schema migration (forced RLS, insert-only) under ADR-024 and ADR-027.

## Operational implications
None in Step 13: nothing is deployed, scheduled or persisted. Configuration slice `citations` (`max_sources`, `max_markers`) only bounds work. No metric, log field or audit event is added.

## Amendment (Step 13 completion): Postgres persistence
Implements the part of decision 7 that was deferred; nothing else in this ADR changes (the `GenerationRecord` value, the port and citation semantics are untouched).
1. **Schema `citations`** (`owns_schema = "citations"`, layer `infrastructure`, migration branch `citations`, revision `c0001`): `generation_records` (one row per record: ids, `config_id`, outcome, template/model labels, request SHA-256, evidence fingerprint, `citation_count`) and `generation_record_citations` (marker, position, document/version/chunk ids, ordinal, `text_hash`, optional pages). Identifiers, hashes, labels and counts only; no prompt, answer, excerpt, text or proof column. No cross-schema reference: cited ids are opaque values, not foreign keys. The only foreign key is the child to its record inside the schema.
2. **Tenant isolation:** forced RLS on both tables with the `app.tenant_id` policy (`USING` and `WITH CHECK`); a missing tenant context sees nothing. The repository also carries the tenant predicate and refuses a foreign tenant before any statement.
3. **Insert-only for every role:** the runtime role is granted `SELECT, INSERT` only; row triggers reject `UPDATE` and `DELETE` and a statement trigger rejects `TRUNCATE`, even for the table owner. Database `CHECK`s repeat the domain rules (outcome set, hash/label/marker shapes, answered if and only if at least one citation, page range). A deferred constraint trigger makes a record and its citations all-or-nothing: the stored citation rows must equal `citation_count` at commit, including when citations are added to an existing record later.
4. **Duplicates:** a second `add` of the same `(tenant, record_id)` raises `ConflictError(citations_record_exists)` and leaves the first record untouched; there is no idempotent replay and no overwrite. Concurrent submissions of one id yield exactly one record. The same id in two tenants is two records.
5. **Adapter:** `UnitOfWorkGenerationRecords` implements the port with one tenant-bound transaction per call (`add` commits on its own; `get` runs under a `SystemContext` with a fixed purpose and still carries the tenant scope). `SqlGenerationRecordRepository` is the same logic bound to a caller's `TenantTransaction`, for a caller that later needs the record to commit atomically with other work (Step 14's decision). Driver errors are replaced by the stable codes `citations_persistence_failed`, `citations_record_corrupt` and `citations_tenant_mismatch`; no value from a record appears in an error. A stored row set that no longer matches its count or fails the domain rules is `citations_record_corrupt` (fail closed).
6. **Retention and purge: none, deliberately.** ADR-023 defines no retention period, legal hold or record purge for generation records, and this amendment invents none: nothing deletes a record, `DocumentPurged` is not subscribed to (records hold no document content, only opaque ids and hashes), and no receipt is written. Consequently the cited ids and hashes of a purged document remain in records. A retention or erasure policy for records is an open owner decision (Step 18); it needs its own ADR and a migration that deliberately relaxes insert-only.
7. **Still deferred:** request-hash policy (the hash is content-derived: keep it out of logs and responses), display/re-authorization flow, `grounded_generation` use cases.

## Amendment 2 (Step 14): provenance-verified, text-bearing evidence
Approved by the owner in the Step 14 blocker resolution (decision 1: Option 1). Nothing else in this ADR changes: the `GenerationRecord`, the port, citation semantics and the persistence amendment are untouched, and the change to `citations.api` is **additive** (the regenerated `api.lock` gains lines and loses none).

**Why this is necessary.** Grounded generation must put the text of the cited chunks into a prompt. The only text read path is chunking's `ReadChunks`; `grounded_generation` has no DAG edge to `chunking` (and adding one would create a second reader of chunk text and a second place that must get authorization ordering right). `citations` already holds the only authorized, hash-checked view of chunks, but deliberately discards the text. Without this amendment Step 14 would have to accept caller-supplied text, which cannot satisfy "only authorized, provenance-backed evidence may support an answer".

**Contract.**
1. `TextEvidenceBuilder.build(ctx, BuildEvidenceCommand) -> TextEvidence` shares one verification routine with `EvidenceBuilder`, so text-bearing evidence cannot be issued under weaker checks: shape and limits, then the proof (exactly `documents.read_document` over exactly the cited documents) before any chunk store is touched, then each claim against the store (chunk id, ordinal, hash; pages from the store), then the proof again. All-or-nothing; one code (`citations_source_unavailable`) for every unavailable cause.
2. Additionally every passage must hash (SHA-256 over UTF-8) to the `text_hash` recorded in its citation, so text that is not the stored chunk text cannot be paired with a citation. `TextEvidence` re-checks this in its own constructor.
3. `ChunkContentReader` (port) and `ReadChunksContent` (adapter over `ReadChunks`, purpose `citations.read_evidence_text`) are the single text read path; `ReadChunksProvenance` still drops text. `ChunkContent` extends `ChunkProvenance` with a `repr=False` text field.
4. Text is transient: `repr=False` on every carrier, never persisted (no persistence file may mention the new types; a pin checks), never logged, never in an error. Exactly two classes in `citations` may have a field named `text` (`ChunkContent`, `EvidencePassage`); a pin checks that too.
5. Caller-supplied text never qualifies: the command has no text parameter, the reader is injected by composition, and `TextEvidence` is constructed only inside `evidence_builder.py` (pin).

**Frozen artifacts touched, and only as required.** `citations/api.lock` (additive); `tests/architecture/test_citations_boundaries.py`: the "only bootstrap imports citations" pin now also admits `grounded_generation` (public `citations.api` only; its bootstrap additionally uses citations' config and `bind_generation_records`) and gains the four confinement pins above; `citations.infrastructure.persistence.unit_of_work.bind_generation_records` exposes the transaction-bound repository without instantiating it elsewhere (AG055). No guard was weakened, no exemption added, no DAG edge added or changed.

**Consequences.** One additional chunk-set read per distinct version per generation (same shape as provenance reads). `ReadChunks` also serves `pending` versions: callers cite only what retrieval returned (ready versions), as before.

## Amendment (Step 18, owner decision D1, 2026-10-18)
The open question in amendment 6 is answered *retain*: generation records and their citation rows stay immutable and retained; no purge path, no time-based retention and no change to insert-only triggers or runtime privileges is introduced. Record identifiers, document and version ids and hashes can persist after `DocumentPurged` and after conversation deletion. No comprehensive-erasure claim is made. Legal holds are deferred (D4). See ADR-042.
