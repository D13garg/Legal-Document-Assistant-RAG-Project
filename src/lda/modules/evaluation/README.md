# evaluation

Evaluation core (Step 12; ADR-018, ADR-030). Runs an explicit dataset against an injected system under test,
scores it with deterministic metrics, names the configuration it ran on and applies a fail-closed release gate.
The Step 12 core (domain, application) stays offline and pure: no provider call, no clock, no randomness.
**Step 17 (ADR-041, Accepted by the project owner 2026-10-18)** adds a typed configuration identity, stored run records and fail-closed release
eligibility; persistence is confined to `infrastructure/persistence` (schema `evaluation`, migration `v0001`, global
insert-only tables, synthetic data only).

## Public surface (`evaluation.api`)
- `EvaluationCase`, `EvaluationDataset` (synthetic, non-customer material; the question is `Sensitive`).
- `EvaluationTarget` port (`observe(case) -> Observation`; identifiers and outcome only, never text).
- `Metric` and `default_metrics()`: `outcome_accuracy`, `retrieval_recall`, `citation_precision`,
  `citation_recall`, `isolation`. Scores are in [0, 1]; an errored case scores 0.0 on every applicable metric.
- `RagConfiguration` (opaque named components of scalars) with a deterministic `config_id` (invariant 15).
- `GatePolicy` / `MetricThreshold` / `GateDecision`: a gate passes only if every criterion holds; an unmeasured
  metric, too few cases or too high an error rate fails.
- `RunEvaluation.execute(RunEvaluationCommand) -> EvaluationReport` (ids, counts, scores, decision).

## Rules
- Imports no other module (`depends_on = []`). No judge, no `ai_gateway` call, no prompt rendering: every
  provider call needs a valid proof (ADR-006); a judge belongs to a later step and needs one.
- The core never builds an `AccessScope`, proof or context. The real target (later step) must run the real
  authorization path as a dedicated evaluation principal.
- Errors and reports carry codes, labels and numbers only. A target exception is discarded (it may carry content).
- Not implemented (Step 17): persisted runs, activation of a configuration, CI gating, LLM-judged metrics.

## Step 17: configuration identity and release eligibility (ADR-041, Accepted 2026-10-18)
- `EffectiveConfiguration`: six required typed components (`prompt`, `generation`, `retrieval`, `chunking`,
  `citations`, `answering`); `config_id` is computed from them (ADR-030 canonical JSON, SHA-256), never declared.
  `bootstrap/rag_configuration.py` fills it from the real settings and the approved template hash; a declared
  `LDA_ANSWERING__CONFIG_ID` must equal it or start-up fails.
- `EvaluateConfiguration`, `RecordEligibility`, `CheckEligibility`: store a run (pass or fail), record eligibility for
  a verified run, and answer "eligible now". All take an `EffectiveConfiguration`, never a bare id.
- `verify_run` trusts nothing stored: hash, canonical text, configuration, dataset, policy, age (inclusive boundary),
  completeness, recomputed metrics and decision, passing. Any failure means not eligible; a failing run in the age
  window blocks.
- `RELEASE_DATASET` (14 synthetic cases, fingerprint pinned by a test) and `RELEASE_GATE_POLICY` (proposed thresholds).
- Eligibility is a recorded fact about evidence. It deploys nothing and proves neither production readiness nor legal
  correctness. Metrics see identifiers and outcomes, not answer text.
