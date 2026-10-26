# ADR-009: Reranking strategy

## Status
Accepted

## Context
Step 11 returns ranked results from vector similarity only. Lexical search, hybrid fusion and cross-encoder reranking were considered for this step.

## Problem
Does Step 11 need a second ranking signal beyond vector similarity?

## Decision
The decision is to defer reranking.

1. **No reranking and no lexical or hybrid search in Step 11.** The rank is the cosine similarity of the chunk to the query, within the authorized, current-version rows (ADR-008). There is no `tsvector`, no text column to build one from (ADR-004 §4) and no second ranking signal; a boundary test pins their absence.
2. **A reranker is a later, separate decision.** A model-based reranker reads chunk text and is therefore a provider egress; it needs the embedding/egress authorization rule left open by ADR-007 §6 first. A purely lexical reranker needs chunk text at query time, which only `chunking.api.ReadChunks` provides, under the eligibility and integrity checks of ADR-008, and would still have to avoid caching the text (ADR-032).
3. **Constraints on any future reranker.** It reranks only the already authorized candidates of one search (never widens the scope), preserves provenance (document, version, chunk id, ordinal, text hash), is deterministic when its model is, degrades to the vector order when unavailable, and keeps text out of logs, errors, metrics and audit.
4. **Hybrid search.** Needs its own storage decision (a text search structure over chunk text contradicts ADR-038 §8 unless that ADR is amended) and its own ADR.

## Rationale
Deferring keeps Step 11 within its approved boundaries: a lexical or model-based signal needs either chunk text at query time or a provider call, and both depend on decisions that are still open (ADR-007 §6, ADR-038 §8).

## Alternatives considered
A cross-encoder reranker now (provider egress with no rule); `tsvector` hybrid search now (needs text in Postgres, contradicting ADR-038 §8); an in-process lexical reranker over text read through `ReadChunks` (viable later, needs its own bounds and no-cache guarantees).

## Consequences
Results are explainable (one similarity score) and cheap. Recall for exact legal phrases and defined terms will be weaker than hybrid search; this is accepted for Step 11.

## Security implications
No new data is stored or sent. Any future reranker must keep the candidate set bounded by the authorized scope and keep text out of logs, errors and caches.

## Operational implications
None: results are ordered by the SQL query itself.

## Migration implications
None.
