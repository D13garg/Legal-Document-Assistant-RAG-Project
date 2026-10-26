# conversations

> Owner-scoped, immutable, append-only message log. It links assistant turns to generation records by reference. It generates nothing.

Decision record: [ADR-039](../../../../docs/architecture/ADR/ADR-039-conversations.md); markers: ADR-034.

## Responsibility
A `Conversation` belongs to one principal of one tenant. The module stores the owner's messages and, for each
assistant turn, only the identifier of the `GenerationRecord` that documents it (owned by `citations`).
Non-responsibilities: answer generation, retrieval, prompts, provider calls, streaming, sharing, titles, any
document or version reference, any authentication or live principal-status check.

## Domain concepts
- `Conversation` (`id`, `tenant_id`, `owner`, `created_at`, `message_count`). Lifecycle: `active` -> deleted (hard).
- `Message` (immutable, `sequence` 1..n per conversation). `user`: `client_message_id` + `MessageText`.
  `assistant`: `generation_record_id` only. No other shape can be built (domain) or stored (database CHECK).
- `MessageText`: non-blank, no NUL, <= `max_text_chars` (hard ceiling 32,000); redacted in `repr`.
- `DeletionReceipt`: tenant, conversation id, time, message count, scope `conversations.messages_and_rows`.

## Invariants
- Owner-only: every use case needs a `TenantContext` and `conversation.owner == ctx.principal_id`. Missing,
  other-tenant and not-owned conversations all raise `conversation_not_found`; a repeated delete does too.
- Messages are immutable and append-only. The database refuses any message UPDATE, TRUNCATE, and any DELETE
  except the cascade from deleting the conversation; the runtime role has only SELECT/INSERT on messages.
- Idempotency (ADR-039 C6): a user message is keyed by `(conversation, client_message_id)`: same text replays the
  original (`replayed=True`, no write, no audit), different text is `conversation_message_conflict` (no content in
  the error). An assistant turn is keyed by `record_id`: same conversation replays, another conversation is
  `conversation_record_linked`. Enforced by PostgreSQL unique constraints; the row lock serializes appends.
- Bounds: `max_messages` (<= 1,000), `max_text_chars` (<= 32,000), `max_list` (<= 200), mirrored by CHECKs.
  Deletion removes at most 1,000 messages in one transaction.
- The linked record must exist, belong to the caller's tenant and principal (read through `citations.api`);
  otherwise `conversation_record_unavailable`. Records are never written or deleted by this module.

## Limitations (read before relying on it)
- **No answer text is stored.** An assistant message shows its record id and `text_stored == False`. The record is
  content-free provenance (ids, hashes, labels, citations), so the answer cannot be reconstructed from it. Showing a
  past answer again needs a later, separately approved decision (stored text implies document-purge hooks).
- **No live principal-status check.** The module has no `identity`/`access_control` dependency. Authenticating the
  caller and refusing disabled principals belong to the API boundary. `@requires` labels here are declarations only
  (ADR-014 section 11); no grants or resource kinds exist for conversations.
- **Deletion** is one transaction: conversation + messages + receipt + audit fact commit together. It does not touch
  generation records, existing audit history or identity records, and it is neither time-based retention nor
  comprehensive personal-data erasure. Backups are out of scope.
- `StartConversation` is not idempotent (each call creates a conversation). There is no title and no pagination cursor.

## Dependencies
`citations` (public `api` only: `GenerationRecordRepository`, `GenerationRecord`). The approved DAG also permits
`documents`; it is intentionally not declared because nothing imports it.

## Public API (`api/`, snapshot in `api.lock`)
| Name | Kind | Declarations (ADR-034) |
|---|---|---|
| `StartConversation` | use case | `@requires("conversations.start_conversation")`, `@audited("conversations.conversation_started")` |
| `AppendUserMessage` | use case | `@requires("conversations.append_user_message")`, `@audited("conversations.user_message_added")` |
| `AppendAssistantTurn` | use case | `@requires("conversations.append_assistant_turn")`, `@audited("conversations.assistant_turn_added")` |
| `DeleteConversation` | use case | `@requires("conversations.delete_conversation")`, `@audited("conversations.conversation_deleted")` |
| `ReadConversation`, `ListConversations` | queries | `@requires(...)`, `@not_audited(reason=...)` |
| commands, views, `ConversationId`, `ClientMessageId`, `MessageRole`, `ConversationLimits`, ports | DTOs / types | |

## Future `answering` integration (Step 16, not built here)
`answering` appends the user's question with `AppendUserMessage`, runs grounded generation, then links the resulting
record with `AppendAssistantTurn`. It must not read this schema directly.

## Persistence (schema `conversations`, branch `conversations`, revision `n0001`)
`conversations`, `messages`, `deletion_receipts`: forced RLS on `app.tenant_id`, tenant-leading keys, no foreign
key to another schema. Deferred triggers check at commit that the counter equals the message count and the
sequence is contiguous. Configuration: `LDA_CONVERSATIONS__MAX_MESSAGES`, `__MAX_TEXT_CHARS`, `__MAX_LIST`.
