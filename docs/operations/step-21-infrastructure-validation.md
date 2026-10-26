# Step 21 — Infrastructure validation record

**Status: PARTIALLY IMPLEMENTED (BLOCKED BY ENVIRONMENT for the real-infrastructure layers). See Step 21A below.**
Offline fixes and gates are done and verified on the final tree. The Docker Compose, MinIO and
standalone-PostgreSQL layers were **not run** because the execution environment had no Docker,
no Compose, no standalone PostgreSQL or MinIO binary, and no route to the Docker registries.
Nothing below claims production readiness.

## Starting checkpoint

`legal-document-assistant-step20d-development-embedder.zip`, SHA-256
`45e4a6761e37ed5b18dce5b6b5a48c8ccbe3b8e20bac8e67f62a47ec513b2857` (matched the expected value).
ZIP integrity OK; 1,127 entries; no duplicate paths, traversal or absolute paths, symlinks,
suspicious secret-like filenames or caches.

## Defects found and fixed

| # | Defect | Evidence | Fix |
|---|--------|----------|-----|
| 1 | `pyproject.toml` allowed `boto3>=1.34`, but `S3ObjectStore.put` sends `IfNoneMatch="*"` for create-only writes. botocore models that parameter only from 1.35.2. | Bisected: botocore 1.35.1 lacks it, 1.35.2 has it. Under botocore 1.34.162 the call raises `ParamValidationError` (a `BotoCoreError`), which the adapter reports as the misleading `object_store_unavailable`. | Floor raised to `boto3>=1.35.2`. Tests: `tests/platform/test_s3_conditional_write_contract.py` (floor test was red before the fix). |
| 2 | `docker/docker-compose.yml` published Postgres as `"5432:5432"` (all interfaces) while MinIO was loopback-bound. The database uses a known development password. | Guard test failed on the original file listing `['5432:5432']`. | Now `127.0.0.1:5432:5432`. Tests: `tests/platform/test_compose_ports_loopback.py` (fails closed on unparsable `ports:` lines; shown to fail again when the fix is reverted). |

No source under `src/`, no migration, no `api.lock`, `module-dag.toml`, `.importlinter`, ADR or
existing test was changed.

## What was and was not verified

| Area | Result |
|------|--------|
| Static gates (ruff, mypy x3, archguard, import-linter, generated import-linter, api-lock) | PASS on final tree |
| Full pytest suite | PASS on final tree: 4,012 passed (4,007 baseline + 5 new), exit 0 |
| Dependency audit (`pip-audit --local --skip-editable`) | PASS: no known vulnerabilities, exit 0 (installed environment, not a lock file; none exists) |
| Cross-process worker + `dev_slice` commands, database RLS-backed assertions | **EMULATOR VERIFIED** only: embedded PostgreSQL (pgserver) and the moto standalone S3 server, via `tests/platform/test_dev_cli_slice.py` |
| Docker Compose execution, image pulls, health checks, bucket init | **BLOCKED BY ENVIRONMENT** |
| MinIO compatibility, `mc ready` health check, conditional-write (`If-None-Match`) behaviour | **BLOCKED BY ENVIRONMENT / NOT VERIFIED** |
| Provisioning and workflow against a standalone PostgreSQL server | **NOT VERIFIED** |
| Real embedding, LLM or scanner providers | Out of scope; not verified |
| Worker shutdown semantics beyond the emulator-level SIGTERM test | NOT VERIFIED (shutdown code not re-audited this step) |

## Findings recorded, not changed

* **Image tags unverified.** The Compose file itself says the MinIO and `mc` tags were never pulled.
  `pgvector/pgvector:pg16` is a floating tag, not an exact pin. Pin only after a successful pull
  (consider digests); do not change tags without compatibility evidence.
* **Verifier exposure (residual).** Provisioning never sends the plaintext password, but the
  `CREATE ROLE ... PASSWORD '<SCRAM verifier>'` statement text contains the verifier. A server that
  logs DDL statements could record it. This reduces, and does not eliminate, credential exposure.
* **Documentation staleness.** The head of `HANDOFF.md` still describes Step 18 as current and says
  `apps/` is empty. Not edited here; the rest of the file was not audited.
* **Monitoring/alerting** remains a future production-readiness item.

## Reproducing on a Docker-capable machine (completes the blocked layers)

Run from the repository root after `uv venv && uv pip install -e ".[dev]"`. Supply secrets yourself
(see `.env.example`); never commit them.

```
docker compose -f docker/docker-compose.yml config -q
docker compose -f docker/docker-compose.yml up -d postgres minio minio-init
docker compose -f docker/docker-compose.yml ps          # expect postgres/minio healthy, minio-init exited 0
docker compose -f docker/docker-compose.yml logs minio-init
python -m apps.provision                                # run twice: second run must change nothing
python -m apps.dev_bootstrap
python -m apps.worker &                                 # then, in another shell:
python -m apps.dev_slice                                # expect "outcome answered", exit 0
# stop the worker with SIGTERM and confirm exit 0
docker compose -f docker/docker-compose.yml down        # add -v ONLY to discard dev volumes
```

Record, for each command, the exact exit status. A failure at any stage (image pull, health check,
conditional write, `CREATE EXTENSION`, provisioning, slice) should be reported by stage with the
sanitized error code, and Step 21 may then be re-assessed.

---

# Step 21A — Owner execution sequence (Docker-capable machine)

> Step 21B: still no owner-run results received. Everything below remains **NOT RUN**.

**Status of Step 21A: BLOCKED BY ENVIRONMENT.** The remote execution environment has no Docker CLI,
no daemon socket, and no route to `registry-1.docker.io`, `quay.io` or `ghcr.io` (checked once; not
retried). Nothing in sections A–D below has been executed by Claude. Run it yourself and keep the
exit codes. Stop at the first failure and diagnose; do **not** change image tags first.

## 0. Preconditions

```
docker version && docker compose version
docker volume ls | grep -E 'pgdata|miniodata'   # pre-existing dev volumes? if yes: STOP and decide
lsof -nP -iTCP:5432 -iTCP:9000 -iTCP:9001 -sTCP:LISTEN   # expect no output
df -h .                                                  # a few GB free
uv venv && . .venv/bin/activate && uv pip install -e ".[dev]"
cp .env.example .env     # edit: PROOF_KEY (generate), keep dev-only passwords; never commit .env
set -a; . ./.env; set +a # .env.example now loads cleanly (Step 21A fix); verify:
python -c "import os,json;print(json.loads(os.environ['LDA_JOBS__QUEUES']))"
```

An existing `pgdata` volume keeps its *old* credentials; a stale one would fail provisioning for
reasons unrelated to the code. Do not delete volumes without a deliberate decision.

## A. Compose and images

```
C="docker compose -f docker/docker-compose.yml"
$C config -q; echo "config exit=$?"
$C pull; echo "pull exit=$?"       # a failure here: record the exact error (tag missing? auth? network?)
$C up -d postgres minio minio-init; echo "up exit=$?"
$C ps -a                           # expect postgres+minio healthy, minio-init Exited (0)
$C logs minio-init | tail -5       # expect bucket created or already present
docker image inspect --format '{{index .RepoTags 0}} {{index .RepoDigests 0}}' \
  pgvector/pgvector:pg16 quay.io/minio/minio:RELEASE.2024-10-13T13-34-11Z quay.io/minio/mc:RELEASE.2024-10-08T09-37-26Z
$C exec -T postgres psql -U lda_admin -d lda -Atc "select version()"
$C exec -T minio minio --version
$C port postgres 5432; $C port minio 9000    # expect 127.0.0.1:... only
```

If `minio` is `unhealthy`: `docker inspect --format '{{json .State.Health}}' $(docker compose -f docker/docker-compose.yml ps -q minio)`
and `$C logs minio`. The health check runs `mc ready local`; whether `mc` exists in that image tag is
one of the unverified assumptions. Diagnose before editing.

## B. Provisioning, RLS, SCRAM

```
python -m apps.provision; echo "provision#1 exit=$?"      # expect 0
python -m apps.provision; echo "provision#2 exit=$?"      # expect 0, nothing new created
env -u LDA_PROVISIONING__ADMIN_URL python -m apps.provision; echo "missing admin url exit=$?"   # expect 2 (fail closed)
```

Catalog evidence (the admin role is the oracle; it is **not** the application's runtime role):

```
$C exec -T postgres psql -U lda_admin -d lda -v ON_ERROR_STOP=1 <<'SQL'
select extname, extversion from pg_extension order by 1;
show password_encryption;
select rolname, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolreplication, rolinherit,
       rolcanlogin, left(rolpassword,14) as pw_prefix from pg_authid where rolname in ('lda_admin','lda_app');
select count(*) as runtime_owned_objects from pg_class c join pg_roles r on r.oid=c.relowner where r.rolname='lda_app';
select count(*) as runtime_memberships from pg_auth_members m join pg_roles r on r.oid=m.member where r.rolname='lda_app';
select n.nspname||'.'||c.relname as tenant_table_without_forced_rls from pg_class c join pg_namespace n on n.oid=c.relnamespace
 where c.relkind in ('r','p') and n.nspname not in ('pg_catalog','information_schema')
   and exists (select 1 from pg_attribute a where a.attrelid=c.oid and a.attname='tenant_id' and not a.attisdropped)
   and not (c.relrowsecurity and c.relforcerowsecurity);
select n.nspname, has_schema_privilege('lda_app',n.nspname,'USAGE') usage, has_schema_privilege('lda_app',n.nspname,'CREATE') "create"
  from pg_namespace n where n.nspname not like 'pg\_%' and n.nspname<>'information_schema' order by 1;
SQL
```

Note: the embedded-PostgreSQL test helper creates its role with a bare `CREATE ROLE ... LOGIN PASSWORD`
(so it shows `rolinherit=t`); only the production provisioner issues `NOINHERIT`. Judge the real server, not that.
The SQL below was syntax-checked against embedded PostgreSQL only (emulator-level).

Expected: `lda_app` has `rolsuper=f`, `rolbypassrls=f`, `rolinherit=f`, `pw_prefix=SCRAM-SHA-256$`; zero owned
objects; zero memberships; the "without forced RLS" query returns no rows; `usage=t, create=f` on module schemas.

SCRAM over the published port, from the host (wrong password must fail, right one must work; nothing secret is printed):

```
python - <<'PY'
import os, sqlalchemy as sa
from sqlalchemy.engine import make_url
good = make_url(os.environ["LDA_DATABASE__URL"])
for label, url in (("right password", good), ("wrong password", good.set(password="definitely-wrong"))):
    try:
        with sa.create_engine(url).connect() as c:
            print(label, "->", c.execute(sa.text("select current_user")).scalar_one())
    except Exception as e:
        print(label, "-> refused:", type(e).__name__)
PY
```

## C. MinIO and the object-store adapter (separate processes)

```
cat > /tmp/s3probe.py <<'PY'
import os, sys
from uuid import UUID
from lda.platform.configuration.settings import ObjectStorageSettings
from lda.platform.storage.factory import create_object_store
from lda.shared.kernel.ids import TenantId
from lda.shared.ports.object_store import TenantScopedKey
e = os.environ
store = create_object_store(ObjectStorageSettings(
    bucket=e["LDA_OBJECT_STORAGE__BUCKET"], endpoint_url=e["LDA_OBJECT_STORAGE__ENDPOINT_URL"],
    access_key_id=e["LDA_OBJECT_STORAGE__ACCESS_KEY_ID"], secret_access_key=e["LDA_OBJECT_STORAGE__SECRET_ACCESS_KEY"]))
key = TenantScopedKey(TenantId(UUID(int=0x21A)), "probe/object1")
try:
    match sys.argv[1]:
        case "put": store.put(key, b"probe-bytes", content_type="text/plain"); print("put ok")
        case "get": print("get matches:", store.get(key) == b"probe-bytes")
        case "delete": store.delete(key); print("deleted")
except Exception as exc:
    print(type(exc).__name__, str(exc)[:100], "| provider code:", getattr(exc, "detail", None)); sys.exit(1)
PY
python /tmp/s3probe.py put;    echo "put#1 exit=$?"   # expect ok
python /tmp/s3probe.py get;    echo "get   exit=$?"   # new process: expect matches: True
python /tmp/s3probe.py put;    echo "put#2 exit=$?"   # expect ConflictError object_exists, exit 1
```

**put#2 is the key result.** If it prints `put ok`, this MinIO ignored `If-None-Match` and silently
overwrote the object: report it, do not work around it (it is an architectural question for the owner).

Failure paths (each should fail clearly, exit non-zero, and print no credential or URL):

```
LDA_OBJECT_STORAGE__BUCKET=no-such-bucket python /tmp/s3probe.py get                 # missing bucket
LDA_OBJECT_STORAGE__SECRET_ACCESS_KEY=wrong python /tmp/s3probe.py get               # denied / bad signature
LDA_OBJECT_STORAGE__ENDPOINT_URL=http://127.0.0.1:9 python /tmp/s3probe.py get       # unreachable
python /tmp/s3probe.py delete     # remove only the probe object this section created
```

## D. Worker and full slice

```
python -m apps.dev_bootstrap                         # prints a principal id; then:
export LDA_DEV_OPERATOR__PRINCIPAL_ID=<printed id>
python -m apps.worker > /tmp/worker.log 2>&1 & WPID=$!
python -m apps.dev_slice > /tmp/slice.out 2>&1; echo "slice exit=$?"; cat /tmp/slice.out   # expect "outcome answered; …", "other tenant denied: True", exit 0
kill -TERM $WPID; wait $WPID; echo "worker exit=$?"  # expect 0 (clean stop)
# leak check (fixed strings; the secret is read from a pipe, never placed on a command line). Markers follow
# tests/platform/test_dev_cli_slice.py: the object-store secret, a connection string, and the sample text.
grep -Fc -f <(printf '%s\n' "$LDA_OBJECT_STORAGE__SECRET_ACCESS_KEY" "postgresql" "ninety days") /tmp/worker.log /tmp/slice.out
# expect "…:0" for both files. If non-zero, do NOT paste the logs; report only the count and the file.
```

Persisted-state oracle (admin role), after the slice:

```
$C exec -T postgres psql -U lda_admin -d lda -Atc "
select 'vectors', count(*) from retrieval.chunk_vectors
union all select 'vectors_other_index', count(*) from retrieval.chunk_vectors where index_id <> (select index_id from retrieval.chunk_vectors limit 1)
union all select 'generation_records', count(*) from citations.generation_records
union all select 'versions_ready', count(*) from documents.versions where status='ready'
union all select 'dead_jobs', count(*) from platform.jobs where status='dead'
union all select 'read_grants', count(*) from access_control.grants where action='documents.read_document'
union all select 'second_tenant_documents', count(*) from documents.documents where tenant_id <> (select tenant_id from access_control.grants limit 1)
union all select 'second_tenant_messages', count(*) from conversations.messages where tenant_id <> (select tenant_id from access_control.grants limit 1)"
```

Expected: vectors ≥ 1, `vectors_other_index` 0, one generation record, one ready version, 0 dead jobs,
one read grant, second-tenant rows 0. Run the slice with the worker stopped to check the timeout path
(`LDA_DEV_SLICE__TIMEOUT_SECONDS=5 python -m apps.dev_slice` → exit 1, `slice_timeout`).

**What the slice does NOT prove.** `apps.dev_slice` grants read access *inside* its own run, and no CLI revokes
a grant and retries. So section D does **not** show missing-grant denial on real infrastructure. Section E covers
that with the existing tests, on a standalone server.

## E. Existing DB-backed tests on a standalone PostgreSQL server (throwaway container)

The project's own convention (`README.md`, `.github/workflows/ci.yml`) is to run the suite against a
`pgvector/pgvector:pg16` server through `LDA_TEST_POSTGRES_URL`. HANDOFF records that CI has never run, so this has
not been done. It adds password-authenticated TCP PostgreSQL evidence for RLS, grants, queues and the answering workflow.

**DANGER — never point `LDA_TEST_POSTGRES_URL` at the Compose `postgres` service or any database you keep.** The
test helper runs `DROP DATABASE IF EXISTS <test db> WITH (FORCE)` and `DROP ROLE IF EXISTS lda_app` (the same role name
the dev provisioner creates) and needs a superuser. Use a separate, disposable container on another port:

```
TESTPW=$(openssl rand -hex 12)
docker run -d --rm --name lda-s21-testpg -e POSTGRES_USER=lda_admin -e POSTGRES_PASSWORD="$TESTPW" \
  -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
for i in $(seq 1 30); do docker exec lda-s21-testpg pg_isready -U lda_admin -q && break; sleep 2; done
docker exec lda-s21-testpg pg_isready -U lda_admin; echo "ready exit=$?"          # expect 0
export LDA_TEST_POSTGRES_URL="postgresql+psycopg://lda_admin:${TESTPW}@127.0.0.1:55432/postgres"
export PYTHONPATH=src:tools:tests/architecture:tests/platform
pytest -q -p no:cacheprovider tests/platform/test_answering_end_to_end.py tests/platform/test_retrieval_end_to_end.py \
  tests/platform/test_answering_workflow.py tests/platform/test_grounded_generation_composition.py   # missing-grant denial
pytest -q -p no:cacheprovider; echo "full suite exit=$?"                         # expect 4014 passed
docker stop lda-s21-testpg                                                          # --rm removes it; touches nothing else
unset LDA_TEST_POSTGRES_URL TESTPW
```

Label the result **STANDALONE-PG VERIFIED (tests)**. It is not Compose evidence, and `tests/platform/test_dev_cli_slice.py`
still uses the moto emulator for object storage there. A failure is real information (CI has never exercised this):
report the test id and the sanitized assertion, not the whole log.

## Reporting results without sharing secrets

Never paste `.env`, a connection string, the proof key, MinIO credentials, or an unfiltered log. Capture each stage
through this filter, then paste the filtered text:

```
redact() { sed -E 's#(://[^:/@[:space:]]+:)[^@[:space:]]+@#\1***@#g; s#(PASSWORD|SECRET|KEY|TOKEN)([A-Z_]*=)[^[:space:]]+#\1\2***#g; s#([Ss]ecret|[Pp]assword)[^[:space:]]*[:=] *[^[:space:]]+#\1=***#g'; }
python -m apps.provision 2>&1 | redact; echo "exit=${pipestatus[1]:-${PIPESTATUS[0]}}"   # zsh / bash
```

Always include: the stage letter, the exact command, its exit status, the lines marked "expect", the image digests,
and `docker version` / `docker compose version`. Stop and ask before any destructive step (`down -v`, `docker volume rm`,
`DROP`): none is required by this runbook.

## Cleanup

```
$C down            # keeps volumes; add -v ONLY to discard dev data you created for this run
```

## Report back

For every numbered command: exit status and the output lines marked "expect" above, plus the image
digests from section A. Step 21 may be marked COMPLETE only when A–D all pass as expected.
