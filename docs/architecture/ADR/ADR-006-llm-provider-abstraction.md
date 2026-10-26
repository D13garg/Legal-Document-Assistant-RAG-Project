# ADR-006: LLM provider abstraction and the egress chokepoint

## Status
Accepted

## Context
Invariant 1 says text reaches a provider only inside an authorized context whose proof is verified at egress. ADR-026 §9 left a contract for Step 9: every `ai_gateway` operation that sends text to an external provider must call `ProofVerifier.verify` before the provider call, and Step 9 owns the architecture test. The frozen DAG gives `ai_gateway` no dependency (`depends_on = []`), `access_control` mints and verifies proofs, and `bootstrap` injects the verifier typed as the shared `ProofVerifier` port. No provider SDK, credential or prompt exists yet.

## Problem
Provide one place through which generation requests leave the system, such that nothing is sent without a proof that is valid right now, a failing provider cannot take the system down or leak content, and the property is enforced by tests rather than by convention.

## Decision
1. **Scope: text generation only.** `ai_gateway` exposes one use case, `GenerateText` (`@public`, `@not_audited`). Embeddings (ADR-007), prompts (ADR-017), retrieval, citations and `GenerationRecord` (ADR-016) are other steps.
2. **No `AuthorizedContext` in the gateway.** The gateway's security boundary is proof verification through `ProofVerifier`. The rule "prompt construction accepts only `AuthorizedContext`" belongs to the step that owns prompt construction (Step 10 onwards). The gateway does not import `access_control`.
3. **Request shape.** `GenerateTextCommand(proof_token, action, model, instructions, segments, parameters)`. Every `ContextSegment` carries a mandatory source `ProofResource(kind, id)` and `Sensitive[str]` text. Tenant and principal come from the `TenantContext` the caller passes in, never from the payload. All text, including the proof token, is `Sensitive`, so `repr`, `str`, formatting and pickling cannot leak it. There is no field that could carry tools (invariant 10).
4. **Exact order (a security property).** (1) validate shape, types and sizes; (2) derive the exact resource set from the segments (an order-insensitive set, a fresh `ProofResource` per segment); an empty set fails closed with `egress_scope_empty` and the verifier is not asked, because Access Control never mints a proof for an empty scope and the gateway does not weaken that for any workflow; (3) `ProofVerifier.verify(token, ProofExpectation)`; (4) any failure, including an infrastructure failure while verifying, ends the request with zero provider calls and the verifier's own `proof_*` code; (5) tenant-qualified rate limit; (6) the provider call; (7) return the completion as `Sensitive` with content-free `CallEvidence`. Resource-set tampering (an added, removed or swapped segment source) changes the derived set and is rejected by the verifier's binding (`proof_mismatch`).
5. **Verification and rate limiting run before every provider attempt.** A retry after a backoff re-verifies, so a proof revoked, expired or whose principal was disabled during the wait stops the retry. The cost is two more reads per retry (ADR-026 consequences), paid only on the failure path.
6. **Failure isolation (no circuit breaker).** `LlmProvider.generate` returns a response or raises `ProviderFailure(kind)`; adapters translate every SDK error and never copy SDK messages into it. Kinds: `UNREACHABLE` (nothing reached the provider: the only retryable kind), `TIMEOUT`, `REJECTED` (permanent) and `AMBIGUOUS` (the request may have been received or processed). The gateway retries only `UNREACHABLE`, at most `max_attempts` (default 3, at most 5) times with deterministic capped exponential backoff and no jitter, and never retries validation, proof, rate-limit, permanent or ambiguous failures. It does not claim exactly-once execution: a `TIMEOUT` or `AMBIGUOUS` outcome may mean the provider did process the request. The per-attempt timeout is passed to the adapter in `ProviderRequest.timeout` and the adapter must enforce it; the worst-case duration is `max_attempts x timeout` plus backoffs. A provider error is re-raised as a fresh `ProviderFailure` with no chained cause. Any other exception from an adapter is a defect, is not caught and is not retried. An in-process circuit breaker is not implemented: with several workers its state would be per process and misleading.
7. **Rate limiting.** One token bucket per tenant (`RateLimitKey(tenant, "ai_gateway:egress")`, defaults 60 burst and 1/s), charged per provider attempt, so retries cannot bypass it. Exhaustion raises `RateLimited` (`egress_rate_limited`, with advisory `retry_after_seconds`). A request rejected earlier (validation, proof) costs nothing.
8. **Call evidence, deferred auditing.** Egress auditing is deferred (ADR-019 D3): no separate-transaction audit mechanism is invented. The result carries `CallEvidence`: provider id, model, generation parameters, SHA-256 of the canonical request (`ai_gateway.request.v1`: sorted keys, compact separators, ASCII-escaped JSON of provider, model, parameters, instructions and the ordered `(kind, id, text)` segments), the sorted resource set the proof was verified against, and the attempt count. Nothing in it is content. The digest is content-derived: a short, guessable prompt could be confirmed by hashing a guess, so it must stay out of logs and client responses. Persisting it is the provenance step's job (ADR-016).
9. **Errors and redaction.** Stable codes: `egress_invalid_request`, `egress_limit_exceeded`, `egress_scope_empty`, `egress_rate_limited`, `provider_unreachable`, `provider_timeout`, `provider_rejected`, `provider_failed_ambiguous`, plus the verifier's `proof_*` codes. No message, `detail`, `repr` or cause contains content, a token or a credential. The module has no logging, no audit emission and no persistence.
10. **Providers: fake only in Step 9.** `LlmProvider` is the port; `FakeLlmProvider` (testing) is deterministic, scriptable and enforces the timeout; `LlmProviderContract` is the contract suite a future adapter must pass. No SDK, credential or network call is added. Provider SDKs belong only under `ai_gateway/infrastructure/adapters/` (AG032), which is empty.
11. **Wiring and configuration.** `bootstrap/ai_gateway.py` builds `GenerateText` from the shared verifier port, a caller-supplied provider, the platform rate limiter and the `ai_gateway` slice (ADR-031: timeout, attempts, backoff, rate, size limits, output-token ceiling). No schema, no migration.

## Known concern recorded, not solved
ADR-007 (embeddings) will also send text to an external provider and must pass the same proof check, yet `retrieval` (which will embed) depends on `ai_gateway` while the proof for index-time embedding has no principal-bound `AuthorizedContext` equivalent. How embedding egress is authorized (and whether query-time and index-time differ) is for the step that owns it. Likewise, whether evaluation workflows without document content may ever call a provider is reconsidered at Step 12; until then every provider call, including one with no document content, requires a valid proof.

## Rationale
One use case, one verification statement before one provider call, and failure kinds that encode retry safety make the chokepoint small enough for an AST test to prove. Keeping `ai_gateway` free of `access_control` preserves the DAG; the shared port is the only contract.

## Alternatives considered
- `AuthorizedContext` in the gateway: duplicates a type owned by Access Control and mixes prompt rules into transport.
- Verify once per request: leaves a window in which a revoked proof could still send on a retry.
- Retry on any 5xx or timeout: could send the same legal text twice; rejected without exactly-once support.
- A process-local circuit breaker: misleading across workers; rate limiting and bounded retries already bound the load.
- A proof-less path for contentless calls: would weaken invariant 1 for every future workflow.

## Consequences
- A provider call costs an epoch read, a principal read and a rate-limit decision; the database must be available for egress (ADR-026).
- Callers must finish within the 60-second proof lifetime including retries or re-authorize.
- Until an adapter exists no real model can be reached; the gateway is verified only against the fake.

## Security implications
Unauthorized content cannot reach a provider unless the proof check is bypassed in code; the architecture test `test_ai_gateway_boundaries.py` proves the verify statement precedes the only provider call, that only the egress use case calls the port, that the module imports no `access_control`, `bootstrap`, SDK, persistence, logging, or tool vocabulary, and that no error is built from content.

## Operational implications
Optional `LDA_AI_GATEWAY__*` variables tune timeouts, retries and budgets; all have safe defaults. No new secrets.

## Migration implications
None (no schema). Adding an adapter later is additive (`infrastructure/adapters`, its own configuration slice and an ADR amendment).
