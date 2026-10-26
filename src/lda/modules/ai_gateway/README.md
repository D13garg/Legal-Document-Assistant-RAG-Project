# ai_gateway

> AI gateway: the only egress to LLM providers; proof verified before every provider call (ADR-006, ADR-026)

## Responsibility
Sends one text-generation request to an `LlmProvider` after proving, right now, that the exact set of resources in the request was authorized for this tenant, principal and action. Owns timeouts, bounded retries, tenant-qualified rate limiting, failure mapping and content-free call evidence. Does not build prompts, retrieve, cite, embed, audit or persist.

## Invariants
- No provider call before `ProofVerifier.verify` returns; every rejection (validation, empty scope, any `proof_*` code, verifier infrastructure failure) makes zero provider calls. Verification and the rate limit run again before each retry.
- The resource set is derived from the segments; tenant and principal come from the execution context, never the payload.
- Only pre-response transport failures (`UNREACHABLE`) are retried, at most `max_attempts` times; `TIMEOUT`, `REJECTED` and `AMBIGUOUS` are not. No exactly-once claim.
- The model has no tools. No content, token or credential in errors, `repr`, logs or evidence. Nothing is persisted or cached.

## Public API (`api/`)
`GenerateText`, `GenerateTextCommand`, `ContextSegment`, `GenerationParameters`, `GenerationResult`, `CallEvidence`, the `LlmProvider` port with `ProviderRequest`/`ProviderResponse`, `ProviderFailure`/`FailureKind`, `RateLimited`, `EgressPolicy`, and the stable error codes. Snapshot: `api.lock`.

## Dependencies
None (`depends_on = []`). Uses only `shared` ports: `ProofVerifier`, `RateLimiter`. `bootstrap/ai_gateway.py` injects them. Importing `access_control` or `bootstrap` is forbidden and tested.

## Owned data
None (no schema, no migration).

## Events in / out
None.

## Must NOT know about
`access_control`, Identity, documents, chunks, prompts, citations, any provider SDK (SDKs belong only under `infrastructure/adapters/`, empty in Step 9), audit.

## Configuration
Slice `ai_gateway` (`LDA_AI_GATEWAY__*`): `provider_timeout_seconds` 30, `max_attempts` 3, `backoff_seconds` 0.2, `backoff_cap_seconds` 2, `rate_capacity` 60, `rate_refill_per_second` 1, `max_segments` 500, `max_segment_characters` 50 000, `max_request_characters` 400 000, `max_instruction_characters` 20 000, `max_output_tokens` 4096.

## Testing in isolation
`testing/` offers `FakeLlmProvider` (deterministic, scriptable failures, simulated latency) and `FakeProofVerifier`. `tests/contract/` holds the provider contract suite a future adapter must subclass. Real-Postgres tests with the real `EpochCheckedProofVerifier` live in `tests/platform/test_ai_gateway_egress.py`; the architecture pins in `tests/architecture/test_ai_gateway_boundaries.py`.
