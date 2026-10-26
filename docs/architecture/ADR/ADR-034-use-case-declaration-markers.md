# ADR-034: Use-case declaration markers

## Status
Accepted

## Context
Architecture invariant 14 requires every use case to declare `@requires`/`@public` and `@audited`/`@not_audited`. ADR-002 and archguard AG053 already assume these names, but the Step 1 baseline never defined them: AG053 only looked for a decorator *named* like a marker (a locally defined `requires`, an empty `@public()`, or `@requires` together with `@public` all passed). Identity (step 2) needs the markers before `access_control` (step 3, ADR-014) and `audit` (step 4, ADR-019) exist. ADR-014 is Planned and has no file; this decision is deliberately smaller than the authorization model and does not amend it.

## Problem
Define the minimal, uniform way a use case states its authorization and audit posture, without implementing authorization or audit, and make AG053 verify the declaration instead of a name.

## Decision
1. **Pure metadata.** `lda.shared.kernel.declarations` defines four class decorators. Each validates the shape of its arguments, attaches one frozen record to the class and returns the *same class*. Nothing wraps `execute`, nothing is checked at call time, nothing is enforced at runtime by this ADR.
2. **The markers.**
   - `@requires(*requirements)`: one or more opaque requirement strings (`Requires`). Purpose: state what a caller must satisfy.
   - `@public(*, reason)`: an explicit claim that no authorization requirement applies (`Public`). It is an auditable exemption with a mandatory reason, not the absence of a declaration.
   - `@audited(action)`: the audit action label (`Audited`).
   - `@not_audited(*, reason)`: an explicit audit exemption with a mandatory reason (`NotAudited`).
3. **Shape only.** Requirements and action labels are lowercase dotted identifiers (`[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*`, at most 128 characters, no duplicates within one `@requires`). Reasons are non-empty, printable, single-line, at most 200 characters. The kernel gives requirements no meaning: vocabulary, resolution and the decision belong to `access_control` (ADR-014). Identity only labels its use cases.
4. **Exclusivity.** Exactly one authorization marker (`requires` xor `public`) and exactly one audit marker (`audited` xor `not_audited`) per use case. A second declaration of the same kind raises `TypeError` at import.
5. **Class level only, use cases only.** Markers are valid only on public top-level classes under `application/use_cases` and `application/queries`. They are rejected on `execute()`, on other methods, functions, private/nested classes, and anywhere outside use-case files (domain, infrastructure, api, presentation, ...). Applying one to a non-class raises `TypeError`.
6. **Read access.** `authorization_of(cls)` and `audit_of(cls)` return the class's *own* record or `None`; declarations are not inherited. They are read-only accessors for future consumers and contain no policy.
7. **Ownership in `shared/kernel`.** Every module's application layer must use the markers; the kernel is the only layer all modules may import, and it stays stdlib-only (AG030). Declaring a use case must not create a dependency on `access_control` or `audit`.
8. **AG053 validation (tightened).** For every public use-case class: the marker is imported from `lda.shared.kernel.declarations` (directly, aliased, or as `declarations.<marker>`); it is called; arguments are string literals meeting the shape rules (`@public`/`@not_audited` take only `reason=`; `@audited` takes one `action`); exactly one of each kind is present; markers appear nowhere else (point 5). Shape limits live in `tools/archguard/policy.py`; a test asserts they equal the kernel's constants.
9. **Governance.** `src/lda/shared/kernel/*` joins `ADR_GOVERNED_GLOBS`: kernel changes now require an ADR and a decisions-log update (AG072).
10. **Future consumers (not implemented here).** Step 3: API middleware and `access_control` read `authorization_of(...)` and decide; `@public` reasons become reviewable exemptions. Step 4: see the Step 4 amendment below; the original wording of this point could not be implemented and is superseded.

## Rationale
A declaration that is checked by AST and readable by tooling gives uniform, reviewable intent now, without choosing an authorization model early. Pure metadata keeps the kernel free of logic and avoids a decorator that appears to protect a use case but does not. Class-level placement gives one place to read and one place for AG053 and consumers to look.

## Alternatives considered
- Amend ADR-014: the file does not exist and would pull the step-3 authorization model forward.
- Define the markers in `access_control`/`audit`: Identity would depend on modules that come later and DAG edges would be inverted.
- Runtime-enforcing decorators: a half-implemented authorization check is worse than none; enforcement belongs to ADR-014/026.
- Allow method-level markers: two places to look, ambiguous precedence.
- A typed permission enum in the kernel: encodes authorization vocabulary in the foundation.

## Consequences
- Until steps 3 and 4 nothing enforces `@requires` or records `@audited`; the markers are declarations, and `@public`/`@not_audited` reasons are free text that reviewers must read.
- The kernel gains one stdlib-only module; the kernel API is ADR-governed.
- Existing code is unaffected: no use cases exist yet.

## Security implications
Silence is a violation, and exemptions are explicit and greppable. Because nothing is enforced at runtime yet, the markers must not be described or relied on as protection before ADR-014 lands.

## Operational implications
None at runtime (no behaviour, no I/O, no configuration).

## Migration implications
None. Extending the marker vocabulary or adding fields to the records is additive but requires an ADR amendment (kernel is governed).

## Amendment (Step 4, ADR-019)
Point 10 originally said the audit module would read `audit_of(...)` and that actor, tenant, resource and outcome "come from the execution context". That is replaced by ADR-019 D1: each `@audited` use case explicitly calls `uow.audit.record(AuditFact(...))` before `commit()`; tenant, actor and request id come from the execution context, while resource and outcome come from the use case (the context carries neither). The declaration stays pure metadata and nothing is wrapped (point 1 stands); archguard rule AG056 checks that an `@audited` use case emits its declared action before commit and that a `@not_audited` use case does not emit. `audit_of(...)` remains an accessor and is not required by Audit. The kernel behaviour is unchanged.
