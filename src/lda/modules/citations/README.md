# citations

Citations and the `GenerationRecord` contract (Step 13; ADR-016). Pure core (domain, application) plus a Postgres
persistence adapter (`infrastructure/`, schema `citations`, migration `c0001`: forced RLS, insert-only for every role,
content-free). No provider call, no logging, no cache. Retention/purge of records: none (no approved policy).

## Public surface (`citations.api`)
- `EvidenceBuilder.build(ctx, BuildEvidenceCommand(claims, proof_token)) -> EvidenceSet`: proof first (exactly
  `documents.read_document` over exactly the cited documents), then each claim is matched to `chunking` provenance
  (chunk id, ordinal, hash; pages from the store), then the proof again. All-or-nothing.
- `EvidenceSet.resolve(markers, max_markers=..., allow_empty=False)`: `S1..Sn` markers only; unknown markers refused.
- `CitationSource` / `Citation`: references (ids, hash, optional pages), never text, titles or offsets.
- `GenerationRecord`, `GenerationOutcome`, `GenerationRecorder`, `GenerationRecordRepository` (`add`/`get` only).
- `source_resource(source)`: what a later authorization decision must cover before a cited source is shown.
- `TextEvidenceBuilder.build(ctx, BuildEvidenceCommand(claims, proof_token)) -> TextEvidence` (ADR-016 amendment 2,
  Step 14): the same proof and provenance checks through one shared routine, then the stored chunk text of every
  cited chunk (read only through chunking's `ReadChunks`, via `ReadChunksContent`) and a check that each passage
  hashes to its recorded `text_hash`. `TextEvidence` = `EvidenceSet` + `EvidencePassage(marker, text)` per citation.
  Text is `repr=False`, never persisted or logged, and cannot be supplied by a caller: there is no parameter for it.

## Persistence (ADR-016 amendment)
- Schema `citations` (migration branch `citations`, revision `c0001`): `generation_records` and
  `generation_record_citations`; identifiers, hashes, labels and counts only. Forced RLS; the runtime role has
  `SELECT, INSERT` only; triggers reject `UPDATE`, `DELETE` and `TRUNCATE` for every role; a deferred trigger keeps a
  record and its citations all-or-nothing.
- Wiring: `bootstrap.citations.build_generation_records(platform)` returns the production repository
  (`UnitOfWorkGenerationRecords`, one tenant-bound transaction per call); pass `CITATIONS_MIGRATIONS` to
  `platform.database.migrate.upgrade`. `SqlGenerationRecordRepository(tx)` binds the same logic to a caller's
  transaction. `InMemoryGenerationRecords` (`citations.testing`) is a test fake only.
- A duplicate `(tenant, record_id)` raises `citations_record_exists`; nothing overwrites or deletes a record.
- Retention, purge and erasure of records are undefined (ADR-023 sets none; open owner decision, Step 18).

## Rules
- A citation is not an authorization; re-authorize before showing or reading a source.
- Errors are stable `citations_*` codes; ids, hashes, tokens and text never appear in errors or reprs.
- `text_hash`, the evidence fingerprint and `request_sha256` are content-derived: keep out of logs and responses.
- Not implemented here: record purge/retention (ADR-016 amendment, item 6). Answer validation and
  `PromptRequest`/`ContextSegment` construction live in `grounded_generation` (Step 14, ADR-029).
