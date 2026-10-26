# ADR-007: Embedding abstraction and index identity

## Status
Accepted

## Context
ADR-006 abstracts LLM text generation and ADR-026 defines the proof that authorizes a provider call that carries content. Neither covers embedding, which also sends content (chunk text at indexing time, query text at search time) to whatever computes the vectors. Step 11 must define the abstraction and the identity of an index without choosing a provider.

## Problem
How does retrieval turn text into vectors without choosing a provider, and how does it keep vectors of different models from ever being compared?

## Decision
One question stays open by design: how embedding egress is authorized (§6).

1. **`Embedder` port** (`retrieval.api.Embedder`): `identity`, `embed_documents(texts)`, `embed_query(text)`; vectors are validated by retrieval (ADR-004 §2), never trusted. A wrong count, size, non-finite number or non-unit length is `embedding_invalid`, a permanent stage failure.
2. **Index identity.** `EmbeddingIdentity(provider, model, version, dimensions)` plus the fixed metric and normalization yield `index_id`, the first 32 hex characters of the SHA-256 of `provider/model/version/dimensions/cosine/l2`. Deterministic and content-free. Every row carries its `index_id`; the index record also stores the identity's parts.
3. **Compatibility.** Vectors are only ever compared within one `index_id`. Any difference in any part is a different index. Search uses the configured embedder's identity: a version that is `ready` but has no record for that identity is not searchable (it needs a rebuild), and its older versions are not served in its place (ADR-008).
4. **Re-indexing.** Running the `index` stage under a new identity records a second index for the same version beside the first (the record key includes `index_id`); a recorded version is never replaced. v1 has no rebuild job and no cleanup of an obsolete identity's rows; they are removed by the purge. This is a known limitation.
5. **Determinism.** The only implementation is `retrieval.testing.FakeEmbedder` (hashed bag of words over SHA-256; same text, same vector, in every process). It runs in process, opens no socket and needs no credentials.
6. **Egress is unresolved and is not guessed.** An embedder that leaves the process is a provider egress carrying document or query text. The indexing path runs as a system job with no user proof, so the proof model of ADR-026 does not directly apply, and no rule yet says which authorization, if any, a system job needs to send chunk text to a provider, or what a query embedding needs. Until the owner approves that rule: no real adapter, no SDK, no network call; `ai_gateway` gets no embedding use case and no new proof type; the port documents the restriction; and a boundary test pins that only the fake implements the port and that retrieval imports no network or SDK module and does not import `ai_gateway`. Step 11 therefore cannot index with a real model.

## Rationale
A narrow port plus a deterministic identity keeps the model choice out of the domain and makes incompatibility a data fact (a different `index_id`) rather than a runtime surprise. Leaving egress unresolved, instead of guessing a rule, keeps ADR-026 authoritative for provider calls.

## Alternatives considered
Reusing `ai_gateway.GenerateText` for embeddings (changes a frozen contract, invents a proof type); a real provider adapter now (no approved egress rule); one global model with no identity (silent mixing after a model change); deriving identity from the provider's response (non-deterministic).

## Consequences
Retrieval quality of the fake is only good enough to test ranking mechanics. Production use needs a follow-up ADR (embedding egress authorization) and an adapter. The DAG still allows `retrieval -> ai_gateway`, but the manifest deliberately does not declare it.

## Security implications
No embedding call leaves the process in Step 11, so no document or query text is sent anywhere. The unresolved rule is recorded as a hard restriction and pinned by tests (only the fake implements the port; no network or SDK imports; `ai_gateway` not imported).

## Operational implications
A model change means a new index built by re-running the stage; old identities' rows are not cleaned up in v1 and are removed only by the purge. No metrics or logs carry text.

## Migration implications
No migration beyond ADR-004's. Moving to another dimension needs a new column or table and a new migration; the identity then differs by construction.

## Amendment (ADR-045 D3, option (a), 2026-10-24): one offline development embedder
Approval state: the sole project owner ruled for option (a) in the conversation. That is a project-owner decision record only; it is not a team approval, a code-owner review, a CI run or a production-readiness approval.

What changes. Points 5 and 6 above said the only implementation is the test double. That is amended to exactly **two** implementations, both deterministic and in-process: the test double `retrieval.testing.FakeEmbedder`, and **one** development embedder, `DevelopmentEmbedder`, in `src/lda/modules/retrieval/application/development_embedder.py`. It is a signed hashed bag of words (identity `development` / `hashed-bag-of-words` / `1`, 256 dimensions, cosine, L2): no network, no SDK, no file, no clock, no cache, no module-level state; input text is used inside the call only.

What does not change. The egress restriction in point 6 stands in full: no real adapter, no SDK, no network call, `ai_gateway` gets no embedding use case and no new proof type, retrieval imports no network or SDK module and does not import `ai_gateway`. The amendment is an offline development exception, not an approval of embedding egress and not a production embedder. Index identity (point 2) and compatibility (point 3) are untouched: a different identity is a different `index_id` and a different index, never reused silently.

Scope and pinning. The boundary test now pins exactly these two files as the only definitions of `embed_documents`/`embed_query` under `src/` (outside the port); any further embedder needs another explicit decision. The class is exported through `retrieval.api` (additive `api.lock`), never selected by default, and built only by `lda.bootstrap.development` behind the development guard (ADR-045 D1). Its retrieval quality is only enough to test ranking and persistence mechanics.
