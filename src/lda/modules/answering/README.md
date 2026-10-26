# answering (Step 16, ADR-040)

One question in, one validated, cited, conversation-linked answer out. **Implemented and validated; ADR-040 is Accepted and Step 16 was accepted by the project owner on 2026-10-18 (project-owner approval only).** Owns no data: no schema, no migration, no infrastructure layer.

## Public interface (`answering.api`)
`Answer.execute(ctx: TenantContext, AnswerCommand) -> AnsweredTurn`

- `AnswerCommand(conversation_id, client_message_id, question, candidate_document_ids)`: `candidate_document_ids` is a non-empty tuple of distinct UUIDs, at most `max_candidates`. They are untrusted; searching "everything I may read" is **not supported**.
- `AnsweredTurn(outcome, statements, user_message, record_id, assistant_message, rejection)`: `outcome` is `answered`, `insufficient_evidence` or `rejected`. `statements` (validated, each with its resolved citation) is the **only copy of the answer text**; it is not stored and cannot be recovered later. `record_id` and `assistant_message` are both `None` exactly when nothing was generated.
- Declarations: `@requires("answering.answer_question", "documents.read_document")` (declarations grant nothing), `@not_audited` (no fact of its own).

## Order (a security property)
validate -> one Access Control decision (granted subset + proof) -> `AppendUserMessage` -> `SearchIndex` -> refuse hits outside the scope -> `GenerateGroundedAnswer` -> `AppendAssistantTurn` -> return. Nothing is written before authorization; nothing is returned before the link succeeded.

## Failure behaviour (each tested)
| Failure | State left | Result |
|---|---|---|
| shape, candidates, question, not authorized, conversation error | nothing written | stable code (`answering_*` or `conversation_*`); all denials are `answering_not_authorized` |
| search or generation fails | user message stays | callee's stable code; retry with same `client_message_id` + text replays it |
| no hits | user message only | `insufficient_evidence`, no generation, no record, no assistant turn |
| link fails after generation | record exists, **unlinked** | `answering_turn_not_recorded` (cause category kept, cause code in `detail`); answer withheld |
| retry / resubmission | another provider call and record | **no exactly-once generation** |

Rejected and model-refused turns have a record and are linked; they carry no statements.

## Boundaries and limits
- Authentication and disabled-principal rejection belong to the outer boundary (no HTTP layer exists). Access Control denying a disabled principal is not a substitute.
- Grounding and citation validation do not establish legal correctness or completeness; prompt-injection defenses are mitigations.
- `config_id` is operator-declared (`LDA_ANSWERING__CONFIG_ID`, shape `rag-<64 hex>`), not a computed fingerprint of the runtime configuration. Step 17 must bind it.
- Orphan generation records are content-free and immutable; retention and erasure are unresolved (ADR-039).
- Tested with a scripted provider and the deterministic fake embedder only; no real provider was exercised.

## Configuration (`LDA_ANSWERING__*`)
`config_id` (required), `max_candidates` (100, <= 10 000), `max_question_characters` (2 000), `max_sources` (8). `bootstrap.answering.build_answering` refuses limits a collaborator would reject.

## Tests
Module: `tests/unit`, `tests/application` (validation and every denial with zero side effects, each decision guard in isolation), `tests/contract`. Cross-module: `tests/platform/test_answering_workflow.py` (real Access Control, Identity, conversations, `GenerateGroundedAnswer`; in-memory twins; scripted search and provider), `tests/platform/test_answering_end_to_end.py` (real PostgreSQL, real pgvector search, real record and conversation tables), `tests/platform/test_answering_bootstrap.py`, `tests/architecture/test_answering_boundaries.py`.
