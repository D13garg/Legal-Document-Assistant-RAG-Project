# Legal Document Assistant

Production-grade legal document RAG platform. Architecture: `docs/architecture/architecture.md`.

```bash
python -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
make check                                   # lint, strict types, architecture, import-linter, tests
python -m archguard new-module documents     # scaffold an approved context
python -m archguard api-lock --write         # refresh api.lock after an intended API change
```

`make check` needs no Docker: the Postgres integration tests start an embedded PostgreSQL 16
(`pgserver`, part of `[dev]`). To use your own server set `LDA_TEST_POSTGRES_URL` to a *superuser*
SQLAlchemy URL (e.g. `postgresql+psycopg://lda_admin:lda_dev_only@localhost:5432/postgres`); the tests
create and drop their own `lda_test` database and `lda_app` role. CI does this with a service container.

Architecture rules fail CI; see `docs/architecture/enforcement.md`.
