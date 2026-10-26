# ADR-045: Development adapters, shared object store and the headless vertical slice

## Status
Accepted

## Context
Approval state (2026-10-23): the sole project owner authorized Step 20C in the conversation.
Approval state (2026-10-24): the sole project owner **resolved D3 by choosing option (a)** in the conversation (Step 20D); recorded below and as an amendment to ADR-007. A project-owner decision only; not a team approval, a code-owner review, a CI run or a production-readiness approval. That is a project-owner decision record only; it is not a team approval, a code-owner review, a CI run or a production-readiness approval.

Facts established from source at the Step 20B checkpoint:

- `ContentScanner.scan` receives identifiers only (tenant, document, version, run); it never sees bytes.
- The grounded-generation verifier accepts a statement only when its cited marker names supplied evidence and its quote occurs verbatim in that stored passage (quote length 12 to 400 characters).
- The retrieval `Embedder` port documents an egress gap ("NOT permitted until an approved rule", ADR-007) and a boundary test pins the test double as the only implementation inside the module. A module `api` may not import its `infrastructure` (AG020); the platform and bootstrap vocabulary guard rejects the port method name `embed_documents` in those layers; `testing` packages are test-only.
- `S3ObjectStore` already accepts `endpoint_url` and explicit keys; its conditional create-only write uses `IfNoneMatch`.
- `RegisterPrincipal`/`GrantAccess` and the document read require a context; ADR-043 provides `DevOperator.tenant_context()` and `grant_first_read()`.

## Problem
The headless slice needs a scanner, an embedder and an LLM that run offline and deterministically, a store that two processes can share, and a driver that exercises the real use cases, without weakening any guard or frozen decision.

## Decision
**D1. Development adapters and one guard.** `lda.bootstrap.development.require_development` requires `LDA_DEVELOPMENT__ENVIRONMENT=development` (no default) and a loopback or Unix-socket database host. Every development factory calls it first; errors are sanitized codes. Accident prevention, not a defence against whoever controls the environment.
- *Scanner* (`DevelopmentScanner`): always CLEAN, inspects nothing. Not malware scanning.
- *LLM* (`DevelopmentLlm`, in `bootstrap/ai_gateway.py`, the one production file the gateway guard lets name the provider port): extractive. Reads the question and `document_context` blocks from the request and quotes one verbatim sentence of the best-overlapping part, or answers `insufficient_evidence`. It cannot emit a quote that is not in the supplied context; it accepts only the model `development-extractive`; it makes no network call. Not a language model.

**D2. Placement.** No guard was loosened. Scanner and LLM live where existing guards already allow their ports.

**D3. RESOLVED (2026-10-24, option (a)): one offline development embedder.** The owner ruled: *allow one named, deterministic, offline `DevelopmentEmbedder` in the retrieval module's `application` layer, with an explicit ADR-007 amendment and an additive `api.lock` update. A narrow, explicit, development-only exception; not approval for network egress, a general relaxation of retrieval boundaries, or a production embedder.*
Implemented as exactly that, and nothing wider:
- `src/lda/modules/retrieval/application/development_embedder.py`: class `DevelopmentEmbedder` implementing the unchanged `Embedder` port. Signed hashed bag of words over NFKC-casefolded Unicode word runs (SHA-256 picks one of 256 dimensions and a sign), L2-normalised, validated by retrieval's own `checked_vector`. Identity `development` / `hashed-bag-of-words` / `1`, 256 dimensions: a different `index_id` from the test double's, so indexes are never shared or silently reused (ADR-007 points 2 and 3). No network, SDK, file, clock, cache or module-level state; it keeps no input. Blank, non-`str`, or over-100,000-character input is a `ValidationError("embedding_input_invalid")`.
- Exported through `retrieval.api` (additive `api.lock`: one class). `api` imports `application`, never `infrastructure` (AG020 intact). No new import from another module; the retrieval DAG is unchanged.
- ADR-007 is amended (appended section; its original text and the egress restriction stand). The boundary test now pins exactly two files as the only definitions of `embed_documents`/`embed_query` under `src/` outside the port (this file and the test double), pins the embedder's import set, and a second test fails if any other production code implements the port.
- The vocabulary guard is untouched; no bootstrap exemption. The development guard (D1) builds it through `build_development_embedder` only after `require_development`. The worker and the driver both obtain it through the same registered set, so indexing and querying use one identity.
- Limitation, stated plainly: texts are close only when they share words. It exercises indexing, search, ranking and persistence mechanics; it says nothing about retrieval quality.

**D4. Provisioning password.** The runtime password is no longer sent to the server in clear. libpq derives a SCRAM-SHA-256 verifier on the client (`PQencryptPasswordConn`); only the verifier appears in `CREATE ROLE` and in the preparatory `SELECT format(...)`. There is no plaintext fallback. The previous code also bound the plaintext as a query parameter; that exposure is removed too. Residual: the verifier is offline-guessable by anyone who can read statement logs, so a throwaway password and log hygiene still apply.

**D5. Shared object store.** Production storage code is unchanged. Added: `platform/storage/readiness.check_bucket_ready` (head-bucket with its own stable codes for missing bucket, denied access, unreachable store); `require_shared_object_store` (the slice needs `endpoint_url` and explicit keys, so it cannot silently fall back to process-local storage); MinIO and an idempotent bucket initialiser in the dev Compose file with placeholder credentials and loopback-bound ports; `moto[server]` in the dev extras for the cross-process test.

**D6. Headless driver.** `lda.bootstrap.headless.run_slice` (shell `apps/dev_slice.py`) builds the production composition for one tenant, obtains the only user context from `DevOperator.tenant_context()`, uploads through `UploadDocument`, grants exactly one read via `grant_first_read(resource_id)` after the upload, polls the authorized `GetDocument` view with a bounded timeout, asks through `AnswerQuestion`, and verifies that every citation's chunk is read back through `ReadChunksContent` with the recorded text hash and contains the statement. It then runs a second real tenant against the first tenant's document and requires denial. It never writes a domain row, never builds an `ExecutionContext`, and returns identifiers and counts only. The separate worker process does the pipeline work.

**D7. Allow-lists.** `apps/` admits `dev_slice.py`; the ADR-043 importer allow-list admits `bootstrap/development.py`, `bootstrap/headless.py` and their tests. No architecture guard on module boundaries changed.

## Rationale
Reuse the existing ports, builders and use cases; keep every development component behind one guard; stop at a frozen decision rather than weaken it.

## Alternatives considered
- Reuse the test doubles from production code: rejected (test-only by guard).
- Put the development classes in a new top-level package: rejected (outside the zone model; a parallel architecture).
- Fake success in the driver: rejected.
- Keep the plaintext password statement and document the risk: rejected once a supported mechanism existed.

## Consequences
Two bootstrap modules, one platform module, one retrieval application class, four `apps/` files in total allow-listed, Compose services, an additive dev dependency extra. One additive `api.lock` change (retrieval: `DevelopmentEmbedder`). No migration or dependency-DAG change.

## Security implications
The driver uses the real authorization path (a missing grant raises `answering_not_authorized`, mutation-checked). The development guard and the loopback check are accident prevention only. MinIO credentials in Compose are placeholders; the object-store credentials are separate from the database credentials.

## Operational implications
Verified only against embedded PostgreSQL and moto's standalone S3 server (an emulator, not MinIO). Compose, MinIO's own conditional-write support, real password authentication on a standalone server and any real embedder, scanner or LLM are not verified.

## Migration implications
None.
