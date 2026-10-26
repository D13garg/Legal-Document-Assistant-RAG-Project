# ADR-018: AI evaluation architecture

## Status
Accepted

## Context
`architecture.md` fixes `evaluation` as a v1 context (DAG: `ai_gateway`, `prompts`), makes evaluation gates a frozen decision and orders the work as Step 12 (evaluation core) before Step 17 (full evaluation and release gating). ADR-006 left one question for Step 12: whether an evaluation workflow without document content may call a provider without a proof. Citations, grounded generation and answering do not exist yet, so there is no real system to evaluate.

## Problem
Provide the part of evaluation that can be built and proven now: what a case and a dataset are, how a result is scored, how a configuration is identified and how a release gate decides, without a real system under test, without persistence and without weakening the egress chokepoint.

## Decision
1. **Offline, tenant-less, pure.** `evaluation` (layers `domain`, `application`; `owns_schema = ""`; `depends_on = []`) has no schema, migration, clock, randomness, cache, logging or provider call. It imports no module: the DAG edges to `ai_gateway` and `prompts` stay permitted but unused.
2. **Datasets are synthetic.** A case holds a `Sensitive` question, an expected outcome (`answered` or `insufficient_evidence`), the source ids a correct answer must cite (non-empty exactly when an answer is expected: invariants 4 and 7) and source ids the asker must never receive. Customer or tenant data must not be loaded into a dataset: Step 12 has no tenant or authorization context. A dataset fingerprint (`evaluation.dataset.v1`, SHA-256) names exactly what was run; it covers question text, so it is content-derived and stays out of logs and client responses.
3. **System under test is a port.** `EvaluationTarget.observe(case) -> Observation` returns an outcome plus retrieved and cited source ids; never answer text. A raise or a malformed return is one errored case with a stable code (`evaluation_case_error`, `evaluation_observation_invalid`); the exception is discarded because it may carry content. A later adapter must run the real authorization path as a dedicated evaluation principal; the core never builds a scope, proof or `AuthorizedContext` (AG090 unchanged).
4. **Deterministic metrics over identifiers.** `outcome_accuracy`, `retrieval_recall`, `citation_precision`, `citation_recall`, `isolation`. Scores are in [0, 1], higher is better; a metric counts only the cases it applies to; an uncited answer scores 0 on precision; an errored case scores 0 on every applicable metric (fail closed). `isolation` is 0 when a forbidden source appears in retrieval or citations.
5. **Report.** Identifiers, counts, per-metric means, per-case scores and the gate decision. No text and no timestamp, so equal inputs give equal reports; time and storage belong to Step 17.
6. **No judge in Step 12 (ADR-006 question).** The proof rule is unchanged: every provider call, including one with no document content, needs a valid proof. No bypass is added and `ai_gateway` is untouched. LLM-judged metrics, which would send text through `GenerateText`, are deferred; they must obtain a proof for the exact resources judged and render through `prompts`.

## Rationale
The deterministic core is the part whose correctness can be proven by tests today and which Step 17 can extend without rework; leaving out a judge keeps the egress invariant intact without a security exception.

## Alternatives considered
- Persisted runs and an `evaluation` schema now: needs tenant and dataset-content decisions that belong with the release workflow (Step 17).
- A judge via `GenerateText` now: needs a proof source for evaluation principals, undecided.
- A contentless-call bypass in the gateway: weakens ADR-006/026 for a convenience; rejected for now.

## Consequences
Runs are reproducible and reviewable; the target adapter, judged metrics, persistence and CI wiring are explicit later work. Metrics score provenance and outcomes, not answer quality.

## Security implications
Reports and errors are content-free; the question is `Sensitive`; the isolation metric makes cross-principal leakage a hard gate. The core adds no egress path and no authorization type.

## Operational implications
Settings slice `evaluation` (`max_cases`, `max_question_characters`). No metrics exporter, job or storage.

## Migration implications
None: no schema.
