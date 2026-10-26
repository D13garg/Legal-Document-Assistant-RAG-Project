# Headless development slice (ADR-045) — Step 20C

**Status: runnable locally with embedded or standalone PostgreSQL and any S3-compatible endpoint
(verified here only with an emulator, see the end).** The owner resolved ADR-045 D3 (option (a)):
one offline `DevelopmentEmbedder` is registered with the development adapter set, so
`python -m apps.worker` and `python -m apps.dev_slice` run when `LDA_WORKER__ADAPTER_SET=development`
and the development guard variables are set. Anything else is refused with exit 2. Nothing here is
production behavior.

## What each piece is (and is not)
| Piece | What it does | What it is not |
|---|---|---|
| Scanner | always CLEAN; never sees bytes | malware scanning |
| Embedder | offline hashed bag of words, identity `development/hashed-bag-of-words/1` | semantic search |
| LLM `development-extractive` | quotes one verbatim sentence from the supplied passages, or says `insufficient_evidence` | a language model; it answers poorly by design |
| Object store | any S3-compatible endpoint with explicit keys; MinIO in `docker/docker-compose.yml` | in-memory storage (refused) |

## Steps
1. `docker compose -f docker/docker-compose.yml up -d postgres minio minio-init`
   Healthchecks show server readiness only; they do not prove the bucket or the app.
2. Create `.env` from `.env.example` yourself and load it into your shell.
3. `python -m apps.provision` (admin URL; creates the runtime role, migrates, verifies RLS).
4. `python -m apps.dev_bootstrap` and record the printed principal id.
5. `python -m apps.worker` (a separate terminal; SIGTERM/SIGINT stop it cleanly).
6. `python -m apps.dev_slice` (uploads, waits for the worker, answers, verifies, shows isolation).
7. `docker compose -f docker/docker-compose.yml down` (add `-v` ONLY to discard the dev volumes).

## Verified so far (see the Step 20D report for exact commands)
The real `apps.worker` and `apps.dev_slice` commands as separate OS processes, embedded
PostgreSQL, and moto's standalone S3 server (an emulator).
Not verified: Docker Compose, MinIO (including its `If-None-Match` support), password
authentication on a standalone server, any real embedder, scanner or LLM.
