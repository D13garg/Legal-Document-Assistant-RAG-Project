# ADR-020: Observability and no-content telemetry

## Status
Accepted

## Context
Telemetry must never carry document content.

## Problem
How logs are structured and constrained.

## Decision
Step 1 delivers structured JSON logging: static event-name messages, an allow-list of structured fields (others dropped and counted), context binding (request/tenant/principal/job), and exception logging by type/code only (no text or traceback). Tracing and metrics export (OpenTelemetry) are deferred until the first HTTP/worker apps exist; the queue already exposes SQL metrics.

## Rationale
An allow-list fails safe: new fields must be added deliberately.

## Alternatives considered
Free-form structlog usage (easy to leak); adopting OpenTelemetry now (no producer yet).

## Consequences
Tracing/metrics will be added by an amendment when apps land; job rows will then gain a trace-context column (expand migration).

## Security implications
Reduces the chance of content in logs; does not replace code review of log call sites.

## Operational implications
Logs are JSON on stdout.

## Migration implications
Additive.

## Amendment (Step 1 hardening)
The allow-list is enforced by the formatter, not by call-site discipline: a message must match an event-name pattern (`[a-z][a-z0-9_.]*`) or it is replaced by `invalid_log_message`; `args` are never interpolated; records from loggers outside `lda.*` (botocore, urllib3, ...) emit `third_party_log` with no text, because at DEBUG they carry request headers, signatures and tenant-scoped object keys. Operators who need a specific third-party message must allow-list it deliberately (later step). A repository test requires every log call in `src/` to use a literal event name.

## Amendment (Step 6)
The log field allow-list gains `stage` and `run_status` (identifiers/labels only). Ingestion logs `ingestion_stage_finished` (`stage`, `outcome`, `error_code`, `duration_ms`) and the worker logs `jobs_reclaimed` (`count`) and `job_reclaim_failed`. Stage-run duration, attempts and outcome are persisted and exposed through `GetIngestionMetrics` (SQL-backed); there is still no metrics exporter. No document content, title or object key may appear in a log field.
