# ingestion

The ingestion **engine** (ADR-010, ADR-011, ADR-036). It runs a document version through an ordered
pipeline of stages as asynchronous, tenant-bound jobs on the Postgres queue, records every stage
run, classifies failures, dead-letters exhausted work, supports explicit replay, recovers stalled
runs, and reports the outcome. It does **not** parse, OCR, normalize, detect structure, chunk,
embed, index, retrieve or generate; those are later modules that plug in as `PipelineStage`s.

## Ownership

| Concern | Owner |
|---|---|
| Document/version lifecycle and `versions.status` | **Documents** (`pending -> ready | failed | quarantined`, database-enforced) |
| Runs, stage runs, job handling, retries, dead letters, replay, recovery, outcomes | **Ingestion** (schema `ingestion`) |

The DAG edge is `documents -> ingestion`; ingestion imports no module. Documents reacts to
ingestion's outcome events and calls its public use cases; later stage modules implement
`PipelineStage` / `ContentScanner` from `ingestion.api`.

## Lifecycle

```
DocumentVersionAdded --(Documents handler)--> job ingestion:run (queue `ingestion`)
  run: scan -> stage_1 -> ... -> stage_n          (strictly in order, each stage idempotent)
    all succeeded            -> run completed   -> IngestionCompleted   -> version ready
    permanent failure        -> run failed      -> IngestionFailed      -> version failed
    scan found malware       -> run quarantined -> IngestionQuarantined -> version quarantined
    retryable, budget left   -> stage pending, worker backs off, tries again
    retryable, budget spent  -> run dead_lettered (job dead)   -> version STAYS pending
    document deleted         -> run cancelled
  Documents applies the outcome, audits it, then acknowledges it to ingestion.
```

`READY` means: scan clean **and** every stage of the run's frozen plan succeeded
(`IngestionRun.complete` refuses otherwise). It never means "the job finished".

## Execution and safety

* Each stage is two short transactions around the stage call: *start* (lock run, count attempt,
  verify the job's fencing token) and *record* (lock run, compare-and-set on the stage attempt,
  verify the fence, publish any terminal event in the **same** transaction).
* Duplicate delivery is inert; a crash between the transactions re-runs only the interrupted stage.
* A worker that lost its lease cannot commit (ADR-012 amendment, `uow.fence.verify`).
* Tenant comes only from the job row; payloads carry opaque ids and a generation.
* Failure codes are identifiers (`[a-z][a-z0-9_.]{0,63}`), never text. Logs/metrics carry
  identifiers and metadata only; Step 6 moves no bytes.

## Recovery

A delayed per-run **watchdog** job: a `running` run with no progress for
`watchdog_delay_seconds` is dead-lettered (`watchdog_stalled`, never a hidden replay); an outcome
Documents has not acknowledged is re-published (new event id, at most
`max_outcome_redeliveries`). `ReplayIngestion` (audited, declared `ingestion.replay_ingestion`;
who may call it is the API layer's job, Step 16) starts the next generation of a dead-lettered run.

## Surface (`ingestion.api`)

`ExecuteRun`, `RunWatchdog`, `ReplayIngestion`, `AcknowledgeOutcome`, `CancelIngestion`,
`PurgeIngestionRecords`, `GetIngestionStatus`, `GetIngestionMetrics`, `Pipeline`, `PipelineStage`,
`ContentScanner`, `ScanVerdict`, the stage outcome types, `ingestion_job_spec`, the job handlers
and the three outcome events. Fakes for tests: `ingestion.testing`.

## Configuration (`LDA_INGESTION__*`)

`WATCHDOG_DELAY_SECONDS` (default 1800; must exceed the worker lease + backoff cap and the longest
single stage attempt) and `MAX_OUTCOME_REDELIVERIES` (default 5). The retry budget is the queue's
`max_attempts` (the `JobSpec` default), shared by all stages of a run. Configure the `events`,
`documents` and `ingestion` queues in `LDA_JOBS__QUEUES`.

## Not in Step 6 (decided or deliberately open)

No real stage, scanner product, quarantine release path or bytes handling (how a stage obtains
originals is decided with Step 7); no `apps/worker` entry point; no metrics exporter; no replay
authorization (API layer); a job claimed after a purge may recreate an inert run that Documents'
handler then removes.
