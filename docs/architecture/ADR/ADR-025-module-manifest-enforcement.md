# ADR-025: Module manifests, generated contracts and architecture enforcement

## Status
Accepted

## Context
Architecture documents erode unless violations fail the build. Phase 1 froze 15 contexts, an approved DAG, forbidden dependencies and design limits.

## Problem
Make boundaries, dependency direction, isolation and design quality machine-enforced with no tolerated-violation baseline.

## Decision
- **`module.toml`** per module: `[module] name, summary, owns_schema, layers` and `[dependencies] depends_on`. (`exposes` from Phase 1 is dropped: `api.lock` is the single source of truth for the public surface.)
- **`docs/architecture/module-dag.toml`** is the frozen approved DAG. Manifests may declare a subset (AG003); declared edges must be used (AG012); actual edges must be declared (AG011).
- **`tools/archguard`** (AST-based, no installed packages needed) implements rules AG000-AG090 (catalogue: `docs/architecture/enforcement.md`). Policy tables are in `tools/archguard/policy.py`.
- **import-linter** config `.importlinter` is *generated* from manifests + DAG (layers, DAG order, shared/platform isolation). CI fails if it is stale (AG004).
- Third-party purity uses an **allow-list** (stdlib only in kernel/ports/domain) rather than a deny-list, enforced by archguard because import-linter cannot allow-list stdlib and would require every banned SDK to be installed.
- **`api.lock`** snapshots each module's `__all__` and resolved signatures (AG080). It is ADR-governed.
- **ADR gate** (AG072): changes to governed files require an ADR file and `decisions-log.md` change. ADR format and log consistency are linted (AG070/071).
- **Exceptions** (`architecture-exceptions.toml`) need owner, ADR, expiry; expired or stale entries fail; AG003/AG090 are never waivable.
- Rules whose subject does not yet exist (RLS lint, repository-via-UnitOfWork, port contract-suite coverage, schema-isolation migration lint) are activated in the step that introduces the subject (step 1 onward), listed in `enforcement.md`.

## Rationale
Generated configuration cannot drift from manifests; allow-lists are strictly stronger; one policy file makes the rules reviewable.

## Alternatives considered
- import-linter only: cannot express manifest diffs, allow-lists, AST design rules.
- grimp-based graph: equivalent for this purpose; AST scanning also runs on fixture trees in tests.
- Convention + code review: rejected by requirement.
- Tolerated-violation baseline: rejected ("no future cleanup").

## Consequences
Adding a module/edge is a deliberate, ADR-backed act. Rules are tested with negative fixtures (`tests/architecture`).

## Security implications
Type-enforced authorization constructors (AG090) and isolation of SDK/ORM/config access are structural controls.

## Operational implications
`make arch` runs locally and in CI. Policy changes are CODEOWNERS-gated.

## Migration implications
New rules land with ADR + fixtures. Retired rules need an ADR amendment.

## Amendment (Step 1)
New rules: AG034 (persistence code may reference only its own schema, including SQL strings and ForeignKey), AG055 (`*Repository` classes are instantiated only in the module's `*unit_of_work*` adapter or `testing/`), and the `lda.platform.testing` restriction under AG020. Database-level checks (forced RLS, policy, tenant-leading index, no cross-schema FK, runtime role unprivileged) are enforced by integration tests in `tests/platform`.

## Amendment (Step 1 hardening)
No policy table changed. The rule engine now expands `from <package> import <submodule>` for `lda` and `lda.platform` roots so AG020 sees the submodule actually imported. Regression tests cover the bypass spellings, SDK confinement (AG031/AG033/AG032) per platform package, env access (AG040), and independent AST facts about the real repository.
