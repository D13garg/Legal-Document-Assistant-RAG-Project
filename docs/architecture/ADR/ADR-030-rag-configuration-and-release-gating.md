# ADR-030: RagConfiguration and release gating

## Status
Accepted

## Context
Invariant 15 requires every retrieval- or generation-affecting change to yield a new `RagConfiguration` that passes gates, and the frozen decisions require evaluation gates before production configuration activation. Evaluation may not import `retrieval`, `chunking` or `grounded_generation` (DAG).

## Problem
Identify a configuration so a result can never be attributed to a different one, and decide pass or fail without room for a silent pass.

## Decision
1. **`RagConfiguration` is opaque.** Named components (labels) each hold sorted scalar parameters (short printable strings, integers, finite floats, booleans). The composition layer fills it from the real settings; credentials and content never belong. At least one component and one parameter per component are required.
2. **`config_id`** is `rag-` plus the SHA-256 of the canonical form (`evaluation.rag_configuration.v1`: sorted keys, compact separators, ASCII). Equal configurations have equal ids; changing any name or value changes it. `GenerationRecord` (ADR-016, Step 13+) will carry it.
3. **Gate policy and decision are pure and fail closed.** A policy needs at least one metric threshold (minimum in [0, 1]), `min_cases` and `max_error_rate`. The gate passes only when the run has enough cases, the error rate is within bound and every named metric was measured and meets its minimum. A metric nobody measured fails; a threshold naming a metric the runner does not know is rejected before any case runs.
4. **Out of scope here.** Persisting a configuration or run, an activation state, binding activation to a passing report, CI wiring and which thresholds apply in production are Step 17; this ADR defines identity and decision only.

## Rationale
A hash of the full description makes accidental reuse of an id impossible without evaluation importing the modules it describes. A fail-closed gate means missing evidence can never pass.

## Alternatives considered
- Typed configuration importing the owning modules: breaks the DAG.
- Caller-chosen configuration ids: allows two configurations to share an id.

## Consequences
Config components are stringly typed; their meaning is by convention until Step 17 fixes the required components.

## Security implications
The id and parameters are not secrets and must stay free of credentials; nothing here authorizes anything.

## Operational implications
None beyond ADR-018's settings.

## Migration implications
None.
