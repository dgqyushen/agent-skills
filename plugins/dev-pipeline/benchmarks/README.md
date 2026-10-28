# Independent P1 Dev Pipeline evaluation

## Goal

Test whether the T0–T3 process increases *verified task quality* enough to
justify extra wall-clock time, tokens, and reviewer rounds. This work is
independent from the five-plugin P0 integration PR.

## What this change actually tests

- Runs the existing create-gate test matrix through repository CI.
- Unit-tests paired, tier-stratified outcome aggregation and bad-input rejection.
- Provides an offline analyzer for two real executions of **the same task**:
  one baseline single-agent run, and one Dev Pipeline run.

No actual Hermes/CLI agent was executed by the analyzer. The analytics tests
use synthetic cases strictly to validate calculations, **not** as evidence
that the pipeline improves quality.

## Evidence protocol

1. Prepare 3–5 reproducible tasks per tier (T0/T1/T2/T3). Use disposable
   repositories with pinned starting commits and identical acceptance tests.
   High-stakes T3 tasks must use synthetic fixtures, never real secrets/money.
2. Randomize whether baseline or pipeline runs first for each task.
   Keep model/provider, starting commit, task brief, tool access and time
   budget comparable. Record any unavoidable differences.
3. Verify both outputs with independent acceptance tests and a blinded review.
   Do not score success by whether the agent itself reports success.
4. Log one JSONL row per task and variant with:
   `task_id`, `variant` (baseline/pipeline), `tier`, `elapsed_s`,
   `input_tokens`, `output_tokens`, `success` (boolean),
   `regressions` (integer), `review_rounds` (integer).
   Numeric fields must be finite, nonnegative and at most 10**15; larger
   values are rejected as recording errors. Keep potentially sensitive
   prompts/logs **outside Git**.
5. Run:
   `python plugins/dev-pipeline/benchmarks/compare_runs.py runs.jsonl --output results.json`
6. Compare quality and costs per tier, not just overall. A baseline that
   completes a T0 task with the same quality and fewer tokens is useful
   evidence for keeping T0 lightweight.

## Acceptance targets (proposed, not validated)

- No guardrail regression in existing create-gate tests.
- No systematic T0/T1 overhead without a corresponding quality benefit.
- Reduced independently verified failures for T2/T3, or clear evidence
  that the added review burden is not justified.
- No conclusion about impact until real matched task pairs exist.

## Restrictions

Do not run the benchmark inside a live finance/production workspace.
Do not enable CLI options that permit unsandboxed writes except in an isolated
disposable environment. Do not assume review rounds are independent model
reviews unless the underlying transcript proves it.
