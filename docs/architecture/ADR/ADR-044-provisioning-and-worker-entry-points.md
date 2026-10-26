# ADR-044: Non-destructive provisioning command and the worker process entry point

## Status
Accepted

## Context
Approval state (2026-10-22): the sole project owner authorized Step 20B (provisioning, then the worker) in the conversation. That is a project-owner decision record only; it is not a team approval, a code-owner review, a CI run or a production-readiness approval. The owner also provisionally accepted the ADR-043 `apps/` allow-list on the condition that it admits only explicitly approved entry points.

Facts established from source at the Step 20A checkpoint:

- `migrate.upgrade(admin_url, *, app_role, extra_locations, target)` runs Alembic with the **admin** URL; every module owns an independent branch and schema (no cross-branch dependencies, so location order does not matter); migrations `GRANT` to `app_role`, which must already exist.
- Role creation exists only in the test helper (`provisioned_database`: `DROP DATABASE`, `DROP ROLE`, `CREATE ROLE`); it is destructive and is not promoted.
- Queues are configuration (`LDA_JOBS__QUEUES`), not database state. `require_queues` runs inside `build_docs` (`documents`, `events`), `build_processing`, `build_chunking` and `build_index` (`events`); `build_ingestion` enforces `ingestion` with an equivalent inline check and the same error code shape. Required set for a full worker: `documents`, `events`, `ingestion`.
- `JobWorker.run_until(stop)` finishes the in-flight batch before returning, and does not catch an exception raised while claiming.
- Ports `ContentScanner` and `Embedder` have no non-test implementation yet.

## Problem
The headless slice needs a database that exists in a safe state and a process that actually runs jobs. Today the only role creation is a destructive test helper, and the only worker is constructed inside tests. Neither may be promoted as-is, and neither may weaken the runtime role, RLS, queue validation or the job semantics.

## Decision
### Provisioning (`python -m apps.provision`)
1. **Admin and runtime credentials stay separate.** `LDA_PROVISIONING__ADMIN_URL` (secret) is read only by this command. The runtime role name and password come from `LDA_DATABASE__URL`, the same URL the worker uses, so there is one source of truth. The two URLs must name the same host, port and database, and different users, or the command refuses.
2. **The database must already exist.** The command never creates, drops, resets or truncates a database, schema or table.
3. **Additive role creation only.** A missing runtime role is created `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT` with a server-side quoted password (`format(%I, %L)`). An existing role is never altered: its attributes are validated and any deviation (superuser, createdb, createrole, replication, bypassrls, cannot login, member of any role, owns any object) refuses the run before any migration.
4. **Migrations** are assembled explicitly in `lda.bootstrap.provisioning.RUNTIME_MIGRATIONS` from the existing module constants (identity, access control, audit, documents, ingestion, processing, chunking, retrieval, citations, conversations), the same set the answering end-to-end test provisions. Evaluation is intentionally excluded: nothing in the worker or the slice uses it. No migration, grant or policy is changed.
5. **Fail on unknown state.** A module schema present without an Alembic version table is refused (`unmanaged_schema_present`); migration errors are reported by code only (the underlying message can carry connection details).
6. **Post-checks (admin connection):** every migrated tenant table (a `tenant_id` column) has row-level security enabled **and forced**; the runtime role has USAGE and not CREATE on each schema, is not a superuser/owner/bypassrls. **Runtime check (runtime connection):** connects as the runtime role, confirms `current_user`, its attributes, and that it sees no `platform.jobs` row without a tenant context.
7. **Repeatable:** a second run creates no role and migrates nothing new. Output names steps and codes, never a URL or a password.

### Queue validation
It stays where Step 18 put it: inside the builders the worker calls. Provisioning does not duplicate or bypass it; the worker's tests cover every required queue.

### Worker (`python -m apps.worker`)
1. `lda.bootstrap.worker.build_worker_runtime` composes the same builders the answering end-to-end test uses for the document-to-index half (identity, access control, original reader, processing, chunking, retrieval index, ingestion, documents), merges their `job_handlers` and **refuses duplicate handler keys** (a plain dict merge would overwrite silently).
2. **Adapters are explicit.** The worker needs a scanner and an embedder. `LDA_WORKER__ADAPTER_SET` names a registered adapter set; the registry is **empty in this step**, so the command refuses (`worker_adapter_set_unavailable`) rather than default to a fake. The development adapters (Step 20C) will register themselves behind the ADR-043 style environment guard.
3. `run_worker` installs SIGINT/SIGTERM handlers that only set the stop event, restores the previous handlers, runs `JobWorker.run_until`, and disposes the pool in a `finally`. Exit status `0` clean stop, `2` start-up refused, `1` runtime failure. Shutdown waits for in-flight jobs (existing `JobWorker` semantics); no kill-timeout is invented.
4. A database outage while claiming ends the process with status `1` (existing `JobWorker` behavior, unchanged); restart is a supervisor concern.

### Allow-list
`ALLOWED_APP_ENTRY_POINTS` gains exactly `apps/provision.py` and `apps/worker.py`. Anything else under `apps/` is still rejected.

## Rationale
Reuse the migration runner and builders as they are; make privilege decisions fail closed; put database-touching logic in `platform/database` (generic: locations and schemas are parameters) and composition in `bootstrap`.

## Alternatives considered
- Promote the test helper: rejected (destructive).
- `ALTER ROLE` to repair an unsafe role: rejected (silent repair of unknown state).
- Default to the test fakes when no adapter set is configured: rejected (a fake would become the silent default).
- Wrap `run_until` in a retry loop: rejected (changes frozen job semantics).

## Consequences
Two thin `apps/` files, one `platform/database` module, two `bootstrap` modules. No module, migration, `api.lock` or dependency-DAG change.

## Security implications
The admin URL is a more powerful secret than anything the runtime holds; it is read only by the provisioning command, never logged, never passed to the worker. Passwords reach the server in a `CREATE ROLE` statement and can appear in server statement logs if `log_statement` is enabled; operators of a shared database should set a throwaway password and rotate it.

## Operational implications
Docker, Compose, real password authentication and a standalone PostgreSQL are not verified by the embedded-Postgres tests (which use trust authentication over a Unix socket).

## Migration implications
None.
