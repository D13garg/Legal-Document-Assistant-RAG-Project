# ADR-002: Hexagonal layering, module surfaces and repository layout

## Status
Accepted

## Context
Domain logic must be independent of FastAPI, SQLAlchemy, provider SDKs and other infrastructure, and every module must be understandable without reading unrelated modules.

## Problem
Define the layers inside a module, what other modules may see, and where code lives.

## Decision
1. Each module (`src/lda/modules/<name>/`) has layers `domain`, `application`, `infrastructure`, `presentation` (the last two optional by manifest), plus two surfaces: `api/` (the only package other modules may import) and `testing/` (public fakes/builders), a `tests/` tree, and an optional `config.py` settings slice.
2. Direction inside a module: `presentation | infrastructure | api` may import `application`; `application` may import `domain`; `domain` imports only itself and `shared.kernel`. Exact tables live in `tools/archguard/policy.py`.
3. `application/use_cases/` and `application/queries/` hold use cases (one public `execute`, declared `@requires`/`@public` and `@audited`/`@not_audited`). Input/output DTOs live in `application/dto/`.
4. Cross-module imports target only `<module>.api` (and `<module>.testing` from tests). Adapters are composed only in `bootstrap`.
5. **Repository layout correction:** all importable code lives under one root package `lda` (`src/lda/{modules,shared,platform,bootstrap}`). Phase 1 placed `platform/` at top level of `src/`, which would shadow the Python standard-library `platform` module and prevents a proper src-layout package. The logical structure is unchanged; only the package prefix is added.
6. `shared/kernel` is pure types; `shared/ports` is a small capped protocol set; `platform/` is technology plumbing; `bootstrap/` is wiring.

## Rationale
Directions keep volatile technology at the edge. A manifest-declared public surface turns "understand a feature by reading only its API and fakes" into a checkable property.

## Alternatives considered
- Global layer folders (`services/`, `repositories/`): rejected (god modules).
- Layers without `api/`: rejected (consumers couple to internals).
- Top-level `platform` package: rejected (stdlib shadowing).

## Consequences
Some ceremony per module (mitigated: `presentation`/`infrastructure` optional, scaffolded by `new-module`). Module `__init__.py` must be empty.

## Security implications
Narrow public surfaces limit where authorization-sensitive types can be created (AG090).

## Operational implications
All rules run in CI (ADR-025).

## Migration implications
Imports are `lda.modules.<name>.api`. A future extraction replaces the `api/` import with a remote adapter.

## Amendment (Step 1)
`lda.platform.testing` holds in-memory doubles of the shared ports. Only tests and module `testing/` packages may import it (AG020). `bootstrap` constructs concrete adapters through platform factories; it may not import SDKs/ORM directly (AG031/AG033). `platform/jobs` code that needs SQL lives under `platform/database/` because ORM imports are confined there.

## Amendment (Step 1 hardening)
Bug fixes inside existing ports; no new port, no change of responsibility. `TenantScopedKey` validates with `fullmatch` (the previous `$` anchor accepted a trailing newline) and rejects rendered keys over the S3 limit (1024). `JobSpec` rejects a blank `unique_key` (it would silently suppress every later job of the tenant) and a naive `run_at`. `RateLimiter.acquire` adapters reject `cost` outside `1..capacity` (a negative cost minted tokens). The AG020 import check now classifies submodule-style imports (`from lda.platform import testing`, `from lda.shared import ports`, `from lda import bootstrap`), which previously bypassed it.
