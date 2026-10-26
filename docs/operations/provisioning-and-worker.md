# Provisioning and the worker (ADR-044) — Step 20B

Status: implemented and tested against **embedded PostgreSQL only** (Unix socket, trust
authentication). Docker Compose, MinIO, password authentication and a standalone server are
**not verified**. The end-to-end development slice is described in `headless-slice.md`.

## Provision (`python -m apps.provision`)

Needs an existing database (the command never creates, drops, resets or truncates anything).

| Variable | Meaning |
|---|---|
| `LDA_PROVISIONING__ADMIN_URL` | owner/admin URL (secret). Read only by this command. |
| `LDA_DATABASE__URL` | the **runtime** URL: its user becomes the runtime role, its password the role password. Must name the same host, port and database as the admin URL and a different user. |

Steps, kept separate in code: (1) create the runtime role if absent, else only validate it and
refuse an unsafe one (superuser, bypassrls, createdb/createrole/replication, cannot log in, member of
a role, owns an object); (2) run the existing migrations for the module set in
`lda.bootstrap.provisioning.RUNTIME_MIGRATIONS`; (3) verify forced row-level security on every
tenant table and that the role has USAGE but not CREATE on each schema; (4) connect **as the runtime
role** and confirm it is unprivileged and sees no job row without a tenant. Exit `0` ok, `1` failed,
`2` refused before connecting. Output names codes only, never a URL or password. A second run
creates no role and migrates nothing new.

Not done on purpose: an unmanaged database (module schemas without an Alembic version table) is
refused rather than repaired; an existing role is never altered. The password is sent in a
`CREATE ROLE` statement and may appear in server statement logs.

## Worker (`python -m apps.worker`)

| Variable | Meaning |
|---|---|
| `LDA_WORKER__ADAPTER_SET` | name of a registered adapter set (scanner + embedder). **No default.** The only registered name is `development` (ADR-045), which also requires `LDA_DEVELOPMENT__ENVIRONMENT=development` and a local database; any other name, or a missing guard, exits `2`. |
| `LDA_JOBS__QUEUES` | must contain `documents`, `events` and `ingestion` (enforced by the builders). |
| other slices | database, object storage, access control proof key, as for the rest of the platform. |

`SIGINT`/`SIGTERM` stop claiming work; the process returns after the in-flight batch (existing
`JobWorker` behavior, no kill timeout), restores the previous handlers and disposes the pool.
Exit `0` clean stop, `1` failure while running (for example the database went away while
claiming; restart is a supervisor concern), `2` refused at start-up.
