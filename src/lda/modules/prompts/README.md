# prompts

> Prompts: code-defined, versioned, deterministic prompt rendering; authorization-agnostic (ADR-017)

## Responsibility
Turns a trusted, versioned template, an untrusted user question and ordered untrusted document text into a `RenderedPrompt`: instructions plus one rendered part per context item, each keeping its own `ProofResource`. Owns template identity and versions, the deterministic trust-boundary markers and escaping, and the renderer's size limits. Does not authorize, retrieve, cite, call a provider, build gateway segments, persist, cache, log or audit.

## Invariants
- Pure and deterministic: the same template version and identical ordered input give byte-identical output. No randomness, clock, environment, hidden I/O, sorting, mutable global state or cache.
- Authorization-agnostic: a `ProofResource` is provenance, not proof. Never creates `AuthorizedContext`, `AccessScope`, proofs, decisions or `ContextSegment` (AG090 unchanged). The gateway's `ProofVerifier` is the egress gate; rendering is not.
- Fail closed: invalid types, NUL or unencodable text, blank question or context text, empty context, unknown template and oversized input raise `ValidationError` with a stable, content-free code. Nothing is truncated, dropped, merged, reordered, normalized or stripped.
- Question and document text are untrusted data inside `[[LDA:BEGIN ...]]`/`[[LDA:END ...]]` blocks; the marker inside them is escaped reversibly (odd backslash run before each occurrence). This is defense in depth, not prompt-injection prevention; ADR-022 stays deferred. Escaping changes the text the gateway hashes.
- Limits count Unicode code points (`len(str)`, no normalization) of the RENDERED text and must not exceed the gateway's (checked in `bootstrap/prompts.py`).

## Public API (`api/`)
`PromptRenderer`, `PromptRequest`, `PromptContextInput`, `RenderedPrompt`, `RenderedContextPart`, `PromptLimits`, `PromptCatalog`, `PromptTemplate`, `production_catalog()` and the codes `prompt_invalid_input`, `prompt_limit_exceeded`, `prompt_context_empty`, `prompt_template_unknown`. Snapshot: `api.lock`.

## Usage
```python
renderer = PromptRenderer(production_catalog(), limits)  # bootstrap: build_rendering(...)
rendered = renderer.render(
    PromptRequest("<id>", 1, Sensitive(question), (PromptContextInput(resource, Sensitive(text)),))
)
```
The composition layer (later `grounded_generation`) maps `rendered.instructions` and each `rendered.context` part into the gateway command, building `ContextSegment`s itself.

## Templates: production vs test fixtures
Templates live in `domain/catalog/`. `PRODUCTION_TEMPLATES` holds exactly one template, `grounded_answer` v1, approved by the project owner on 2026-10-16 for one exact SHA-256 (ADR-029 D8; not a team approval); any other selection fails with `prompt_template_unknown`. `domain/catalog/sample_template.py` is a **test-only** template (`test_only=True`), reachable only through `prompts.testing`; a production catalog refuses it. A production template needs an explicit approval of its exact wording and its own version; never edit an approved version in place (the approved hash is pinned; a wording change is a new version and a new approval). CODEOWNERS routes this directory to `ai-quality`.

## Dependencies
None (`depends_on = []`). Uses only `shared.kernel` (`Sensitive`, errors) and the neutral `ProofResource` from `shared.ports.authorization`. Importing `access_control`, `ai_gateway` or `bootstrap` is forbidden and tested.

## Owned data
None (`owns_schema = ""`, no migration, no persistence).

## Events in / out
None.

## Must NOT know about
`access_control`, `ai_gateway`, documents, chunks, retrieval, citations, tenants, any provider SDK, audit, logging.

## Configuration
Slice `prompts` (`LDA_PROMPTS__*`): `max_parts` 500, `max_part_characters` 50 000, `max_instruction_characters` 20 000, `max_question_characters` 8 000, `max_total_characters` 400 000. Each of the first, second, third and last must be no greater than the matching `ai_gateway` limit (`max_segments`, `max_segment_characters`, `max_instruction_characters`, `max_request_characters`); otherwise `build_rendering` (in `bootstrap/prompts.py`, bounds passed as `GatewayBounds` because only `bootstrap/ai_gateway.py` may import the gateway) raises `ConfigurationError`.

## Testing in isolation
Unit and application tests use only this module (`testing/` offers `catalog_with_test_only_template`, `build_prompt_request`, `build_context_input`, `default_limits`). The composition test with the gateway fakes is `tests/platform/test_prompts_composition.py`; the architecture pins are `tests/architecture/test_prompts_boundaries.py`.
