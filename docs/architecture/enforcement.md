# Architecture enforcement catalogue

All rules run via `python -m archguard check` (plus generated import-linter contracts). Exceptions: `architecture-exceptions.toml` (owner, ADR, expiry; AG003/AG060/AG061/AG090 never waivable).

| Rule | Enforces |
|---|---|
| AG000 | Python syntax errors in scanned code |
| AG001 | Module structure: required files, empty module `__init__`, only known layer dirs, layers match manifest |
| AG002 | `module.toml` schema (name, summary, layers, owns_schema == module name, depends_on) |
| AG003 | Module is an approved context; edges are in `module-dag.toml`; DAG acyclic |
| AG004 | `.importlinter` is stale versus manifests/DAG |
| AG010 | Cross-module imports only via `<m>.api` (`<m>.testing` from tests only); no package-root imports |
| AG011 / AG012 | Actual cross-module edges equal declared `depends_on` (undeclared / unused) |
| AG020 | Layer/zone direction (submodule-style imports such as `from lda.platform import testing` included); `lda.platform.testing` importable only from tests/`testing/` (domain<-application<-api/infra/presentation; kernel/ports/platform/bootstrap/apps) |
| AG030 | Third-party allow-list: kernel/ports/domain stdlib only; application/api/testing/config stdlib+pydantic |
| AG031 | ORM and DB drivers only in `*/infrastructure/persistence/**`, `platform/database/**` |
| AG032 | Provider SDKs only in `ai_gateway/infrastructure/adapters/**` |
| AG033 | boto3, redis, celery (banned), web frameworks, HTTP clients, LangChain: allowed locations only |
| AG034 | Persistence code references only its own schema (schema=, ForeignKey, SQL strings) |
| AG040 | `os.environ`/`os.getenv`/`BaseSettings` only in `platform/configuration` |
| AG050 | Banned names: utils/helpers/common/misc/services/manager paths; `*Manager|Helper|Util(s)|Service|Orchestrator|Coordinator` classes |
| AG051 | Constructor <= 7 params, class <= 400 lines, port <= 12 methods |
| AG052 / AG053 | Use cases: exactly one public `execute`; exactly one of `@requires`/`@public` and one of `@audited`/`@not_audited` on the class, imported from `lda.shared.kernel.declarations`, with literal shape-checked arguments; markers rejected on methods, functions and outside use cases (ADR-034) |
| AG054 | Inheritance depth <= 2 |
| AG056 | `@audited(x)` use case calls `<uow>.audit.record(AuditFact(action="x", ...))` (declared label, string literal, inline fact) and records before the `commit()` of the method that commits; a `@not_audited` use case must not emit (ADR-019) |
| AG055 | `*Repository` instantiated only in `*unit_of_work*` adapters or `testing/` |
| AG060 / AG061 | Exceptions invalid or expired / stale |
| AG070 / AG071 | ADR format; decisions-log consistency with ADR files |
| AG072 | ADR gate (`adr-gate`): governed files need an ADR + log change |
| AG080 | `api.lock` drift; `__all__` literal and resolvable |
| AG090 | `Authorized*`, `AccessScope`, `AuthorizationProof`, `ContextSegment` creatable only in their owning modules |

## Activated in later steps (their subject does not exist yet)
| Rule | Step |
|---|---|
| Every tenant table has FORCE RLS + policy + leading `(tenant_id, ...)` index; predicate-removal test; no cross-schema FK | done in step 1 (`tests/platform/test_rls_and_schema.py`, needs Postgres) |
| Repositories obtained only via `UnitOfWork`; schema isolation | done in step 1 (AG055, AG034) |
| Every `@port` Protocol has a contract suite per registered adapter | first adapter (3-5) |
| Marker decorators (pure metadata) in `shared/kernel/declarations.py`: defined and AG053-checked | done in step 2 (ADR-034) |
| `@requires` interpreted by `access_control` (decision use cases exist, step 3); wired into API middleware (later) | 3 / 16 |
| `@audited` honoured: emission checked by AG056, insert-only `audit.records` (`tests/architecture/test_audit_boundaries.py`, module tests, `tests/platform/test_audit_bootstrap.py`) | done in step 4 (ADR-019) |
| Documents boundary: no new module edge, immutable versions, tombstone + revocation in one transaction, insert-only receipts, least-privilege grants, no ingestion/search code (`tests/architecture/test_documents_boundaries.py`, module tests, `tests/platform/test_documents_bootstrap.py`) | done in step 5 (ADR-005/015/023) |
| Failed/denied attempts, authorization-decision and egress auditing (need a separate-transaction mechanism and the API layer / `ai_gateway`) | 16 / 9 |
| Purge of derived data (chunks, embeddings, search projections), downstream receipts, crypto-shredding (ADR-023 deferrals) | 6-11 / later |
| `ai_gateway` calls `ProofVerifier.verify` before every external text egress (contract in ADR-026) | 9 |
| `prompts` is pure: no `access_control`/`ai_gateway`/`bootstrap` import, no authorization type or `ContextSegment`, no mutable state, no logging, content-free errors, empty production catalog (`tests/architecture/test_prompts_boundaries.py`, ADR-017) | done in step 10 |
| `evaluation` is pure and offline: no module, provider, SDK, persistence, logging, clock or randomness import; no authorization type; content-free codes (`tests/architecture/test_evaluation_boundaries.py`, ADR-018) | done in step 12 |
| `citations` imports only `access_control.api`/`chunking.api` (Step 14: its public api is consumed by `grounded_generation`); only the evidence builder constructs `EvidenceSet`; no provider, persistence, logging, cache or `PromptRequest`/`ContextSegment`; no text field in a source or record; the audit exemption set is unchanged (`tests/architecture/test_citations_boundaries.py`, ADR-016) | done in step 13 |
| `grounded_generation` uses other modules through `.api` only, has no `chunking`/`retrieval` import, a pure domain, no logging, no bare or broad `except`; authorization precedes evidence building, rendering precedes the second authorization precedes egress, the record commits before an answer is built; only the drafter builds prompts and gateway commands; text-bearing `citations` types are confined (`tests/architecture/test_grounded_generation_boundaries.py`, `test_citations_boundaries.py`, ADR-029, ADR-016 amendment 2) | done in step 14 |
| `oasdiff`, Schemathesis, Trivy, Semgrep, container build | 16 / 18 |
