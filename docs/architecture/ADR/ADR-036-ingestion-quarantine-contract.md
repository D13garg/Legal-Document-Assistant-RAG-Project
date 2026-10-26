# ADR-036: Ingestion quarantine contract

## Status
Accepted

## Context
ADR-022 (security model and untrusted-content handling) is deferred by owner decision, so no approved security contract describes malware handling. Step 6 needs the smallest contract that makes a malware outcome distinguishable and unable to enter the normal processing path, without choosing a scanner product.

## Problem
Define the scanner port, the verdict mapping and what quarantine does and does not mean in Step 6.

## Decision
1. `ContentScanner.scan(ScanRequest) -> ScanVerdict` with verdicts `clean`, `infected`, `error`. `ScanRequest` carries identifiers only (tenant, document, version, run).
2. The scan is the mandatory first stage (`scan`) of every run. `clean` -> the stage succeeds and the next stage runs. `infected` -> the stage and the run become `quarantined`, no later stage runs, and `IngestionQuarantined(error_code="malware_detected")` is published. `error` (and an exception from the scanner) -> retryable failure `scan_error`.
3. Documents records `quarantined` as a terminal version status (database-enforced); a quarantined version can never become `ready`, `failed` or `pending` again, and a replay can never reach it.
4. The original object is left exactly where it is. Step 6 adds **no release path, no object movement, no scanner integration and no scanner product choice**; only a fake scanner exists (`ingestion.testing`). Deleting the document purges the original through the normal ADR-023 flow.
5. Out of scope and not decided: how a real scanner obtains bytes (ADR-010 §13), signature updates, size limits, notification of quarantine.

## Rationale
A three-valued verdict with a terminal outcome is the minimum that keeps malware out of the pipeline and out of `READY` while leaving product and release decisions open.

## Alternatives considered
Treating infected content as a plain failure (loses the distinction); scanning inside Documents (cross-concern); deleting infected originals (destroys evidence without a decided policy).

## Consequences
Until a real scanner is adopted and wired, quarantine is exercised only by tests.

## Security implications
Fail closed: any verdict other than `clean` stops the run; scanner errors retry and finally dead-letter rather than pass.

## Operational implications
`quarantined` versions and runs are countable (`GetIngestionMetrics`, Documents status).

## Migration implications
Documents migration `d0002` admits the `quarantined` status.
