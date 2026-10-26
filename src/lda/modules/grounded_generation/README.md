# grounded_generation

Grounded generation (Step 14; ADR-029): a question plus untrusted source claims in, a validated, cited answer (or an
explicit refusal/rejection) out. Pure core (domain, application) plus two adapters (`infrastructure/`): the Access
Control bridge and the PostgreSQL unit of work. Owns no schema. No provider adapter exists yet (fake-only since Step 9).

## Public surface (`grounded_generation.api`)
- `GenerateGroundedAnswer.execute(ctx, GenerateGroundedAnswerCommand(question, claims, config_id)) -> GroundedAnswer`.
  `@requires("documents.read_document")`, `@audited("grounded_generation.generation_recorded")`.
- `GroundedAnswer.outcome`: `ANSWERED` (statements, each with one verified citation), `INSUFFICIENT_EVIDENCE` (the model
  said so, or no sources were supplied: then `record_id` is None and nothing was authorized, read, sent or recorded),
  `REJECTED` (the provider reply broke the answer contract; only a stable `rejection` code survives).
- Errors are stable codes (`grounded_*`); provider failures propagate unchanged and produce no answer and no record. Adapters must raise only `ProviderFailure`; anything else also propagates unchanged (no answer, no record).

## Flow (order is a security property)
1. Context must be a principal's (`grounded_requires_principal` otherwise); command shape and `config_id` are checked.
2. Fresh decision for exactly `documents.read_document` over exactly the cited documents. ALL or nothing.
3. `TextEvidenceBuilder` (citations): proof again; then, per distinct cited version, the lifecycle gate (`ReadyVersionContent`:
   Documents must report the version `ready`, else the single `citations_source_unavailable`, before any text is touched; `ReadChunks`
   itself also serves `pending` versions, so this gate is what keeps unready text from a provider); then chunk store, claim and
   passage-hash checks. First chunk read happens here. Eligibility is `ready`, not currency: an older `ready` version is still eligible.
4. Prompt rendering (`prompts`): trusted instructions; question and passages as untrusted blocks.
5. A second fresh decision immediately before egress; the gateway verifies it again before the provider call.
6. The reply is parsed against the answer contract and each quote is matched to the cited chunk text.
7. One transaction records the `GenerationRecord` and its audit fact.
8. For an `ANSWERED` outcome, a third fresh decision; if access was revoked meanwhile the statements are withheld (the record stays).

## Answer contract (ADR-029 section 3)
`{"outcome":"answered","statements":[{"text","citation":"S<n>","quote"}]}` or
`{"outcome":"insufficient_evidence","statements":[]}`. One source per statement; the quote must occur verbatim
(whitespace-insensitive) in the cited chunk. This proves the source exists and contains those words, NOT that the
statement is entailed by them.

## Rules
- No text, quote, prompt or provider reply is logged, audited, recorded or placed in an error.
- Prompt-injection defense is structural (authorization, trust-bounded blocks, strict output contract, citation and quote
  verification, no tools), never wording alone.
- The production prompt catalog serves exactly `grounded_answer` v1, approved by the project owner on 2026-10-16 (D8; not a team
  approval). It lives in `prompts` (`test_only=False`). Any other template id or version fails closed with
  `grounded_template_unavailable` before any provider call. The exact wording, its hash and the approval record are in
  `docs/ai/grounded-answer-v1-review-request.md`; changing the wording needs a new explicit owner approval.
- Approval state (ADR-029, `Accepted` 2026-10-16): D1-D7 owner-approved 2026-10-15; D4 outage semantics confirmed; module deliberately unrouted in `CODEOWNERS` (owner decision); D8 owner-approved 2026-10-16. All are project-owner approvals; none is a team approval.
- Tests: doubles live in `tests/support.py`; real-Postgres composition in `tests/platform/test_grounded_generation_composition.py`.

## Neutral claim input (Step 16, ADR-040 D1; additive)
`ClaimReference(document_id, version_id, chunk_id, ordinal, text_hash)` (plain UUIDs, a bounded ordinal, a 64-hex digest, validated at construction) and `claims_from_references(refs) -> tuple[SourceClaim, ...]` let a caller that may not depend on `citations` (`answering`) build the command's `claims`. The conversion stays here. A reference is untrusted and is not proof of authorization: every provenance, proof and lifecycle check still runs. Nothing existing changed.

