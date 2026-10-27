# Converge Loop (spec reconciliation)

## Contents

- 9. Converge Loop (spec code reconciliation)
- Consolidated: task granularity lives in `per-task-workflow.md`

### 9. Converge Loop (spec ↔ code reconciliation)

After the final integration review passes, run a converge assessment: compare the **codebase** against the spec (if one exists) and the plan, looking for gaps the per-task review loop cannot catch — requirements that drifted during implementation, spec items silently dropped, or integration behavior that doesn't match acceptance criteria. This is the `converge` phase adapted from spec-kit.

**Why a converge loop:** Per-task reviews verify each task against its own spec. The final integration review verifies cross-task consistency. Neither verifies that the **overall implementation** still satisfies the **original spec intent** after all the micro-decisions and fixes accumulate. Converge closes that gap by re-reading the spec with the finished code in hand.

```python
delegate_task(
    goal="Converge: assess codebase against spec and plan for residual gaps",
    context="""
    All tasks are complete and the final integration review passed.
    Now assess the CODEBASE against the ORIGINAL SPEC and PLAN for residual gaps.

    Read first:
    - .hermes/specs/<latest>.md (the WHAT/WHY source of truth, if it exists)
    - .hermes/plans/<latest>.md (the plan that was executed)
    - .hermes/sdd/progress.md (completed tasks and deferred findings)

    Then inspect the actual codebase:
    - Run the test suite: pytest tests/ -q
    - Read the key implementation files
    - Check the API surface / CLI behavior against spec acceptance criteria

    CHECK:
    - [ ] Every spec user story: does the code actually deliver it end-to-end?
    - [ ] Every acceptance criterion: can a user / test observe it in the running system?
    - [ ] Every NFR (performance, security, reliability): is it satisfied, not just "addressed"?
    - [ ] Every [NEEDS CLARIFICATION] that was resolved: does the resolution match what was built?
    - [ ] Every edge case in the spec: is it handled in code, not just in a test?
    - [ ] Any spec requirement that was silently dropped during implementation?

    OUTPUT:
    - If converged: report CONVERGED with a one-line summary.
    - If gaps found: list each gap as a concrete task (files, objective, verification).
      Append them to the plan's task list as new tasks (T0N...), append-only —
      do not modify existing completed tasks. Write the gap list to
      .hermes/sdd/converge-<N>.md.
    """,
    toolsets=['terminal', 'file']
)
```

**If the converge reviewer reports CONVERGED:** proceed to Step 10 (final verification + commit). Done.

**If gaps are found:** append the new tasks to the plan, then execute them through the same per-task workflow (Step 6: implementer → spec review → quality review → verify → ledger). After the new tasks are complete, re-run the converge assessment. Repeat until converge reports CONVERGED.

Track converge iterations in the ledger:

```
Converge iteration 1: 3 gaps found (T046, T047, T048) — see .hermes/sdd/converge-1.md
Converge iteration 2: CONVERGED — all gaps closed
```

**Convergence bound:** Cap at the tier's converge limit (T2: 2, T3: 3 — `orchestrator/references/size-tiers.md`; T0/T1 skip the loop). If gaps persist at the cap, the spec or plan has a structural problem — stop and ask the human to review the spec/plan rather than continuing to patch symptoms. One more round of converge on a broken spec produces code that satisfies a broken spec.

**Append-only discipline:** Converge appends new tasks. It never modifies or reopens completed tasks. If a converge gap reveals that a completed task was wrong, that's a fix dispatch against the original task (with re-review), not a converge task — converge is for *missing* work, not *incorrect* work.

Task granularity rules were consolidated into `per-task-workflow.md` (single copy) — read them there before dispatching any implementer.
