---
name: orchestrator
description: "Use when the user asks how to approach building a feature or says 'build X', 'implement X', 'add feature X' without naming a phase ('SDD it', 'spec-driven development', 'run the dev pipeline', 'what phase comes next') — the dev-pipeline stage router: size tier first (T0-T3), then grill → spec (+clarify, plan, tickets) → test-evals → build → review-commit (two-axis pass, budgeted confirm rounds, commit/deploy gate). Owns the size tiers and the model lanes."
metadata:
  version: 2.0.0
  author: moonlight-lupin
  license: MIT
  platforms: [linux, macos, windows]
  hermes:
    tags: [sdd, spec-driven-development, orchestrator, dev-pipeline, routing, stages, workflow, size-tiers, lanes, kanban, converge]
    related_skills: [grill, spec, test-evals, build, review-commit, review-lessons, kanban-orchestrator, model-routing, improve-codebase-architecture, doubt-driven-development, cursor-cli, codex, claude-code]
---

# Orchestrator — Spec-Driven Development (SDD) Router

A **router**: it picks the size tier and the stage, then you load that stage
skill. It holds no stage content itself. It owns three single-home files every
stage points to:

- `references/size-tiers.md` — how much pipeline a change gets (T0-T3)
- `references/lanes.md` — which model builds and which review (the only
  place model names live)
- `references/coding-principles.md` — how code gets written (think first,
  simplicity, surgical changes, goal-driven), the T0/T1 plan format, the
  unreachable-scenario definition, and the builder block every brief carries

The chain diagram lives in `references/flow-map.md`.

## When to use

- User says "build X", "implement X", "add feature X" and doesn't name a phase
- User asks "how should we approach building X?" or "what's next?"
- You need to decide how much process a change deserves

**Don't use** when the user already named a phase ("write a spec for X",
"review this diff") — load that stage skill directly.

## Step 0 — pick the tier (always)

Read `references/size-tiers.md`, pick T0 / T1 / T2 / T3, and tell the user in
one line with the reason. At T0/T1, follow it with an assumptions line and a
`step → verify: check` plan (`references/coding-principles.md` § Plan format
for T0/T1). The tier decides which rows of the table below run,
how many reviewers look, and how many review rounds the loop may spend.
Tiers go up when the work turns out bigger or riskier; never silently down.

| Tier | Path (summary — table in `size-tiers.md`) |
|---|---|
| **T0 Trivial** | tier + assumptions line + `step → verify` plan → edit → test if behavior changed → commit. No stages. |
| **T1 Small** | assumptions line + short `step → verify` plan with inline acceptance criteria → `test-evals` → one implementer → `review-commit` (Step A + R1 + ≤ 1 confirm round, one reviewer) |
| **T2 Standard** | full chain + Goldfish offered after the approved plan; one reviewer per build task; two reviewer lanes; ≤ 2 confirm rounds; converge cap 2 |
| **T3 High-stakes** | full chain + grill + Goldfish offered after the approved plan; two-stage review per task; three reviewer lanes; ≤ 3 confirm rounds; converge cap 3 |

Any live deploy needs the three-reviewer gate, whatever the tier.

## The stages

| Stage | Produces | Load when |
|---|---|---|
| **`grill`** | hardened plan — every decision branch settled with the user | plan is fuzzy (T2/T3; T1 only if the ask is ambiguous) |
| **`spec`** — spec turn | user stories, acceptance criteria, `[NEEDS CLARIFICATION]` markers, seam sketch + seam notes → `.hermes/specs/` | intent not pinned (T2/T3) |
| `spec` — clarify | markers resolved and folded back | markers remain after the spec turn |
| **`spec`** — plan turn | tasks with tracer-bullet slices, blocking edges, file paths, interfaces, the test cases each task must pass → `.hermes/plans/` | spec exists (or clear requirements), need the HOW |
| `spec` — tickets turn (optional) | tickets with blocking edges → `.hermes/tickets/` | want parallel work units |
| **`test-evals`** | the RED tests for the plan's test cases, each citing its seam note, run to failure | after the plan, before `build` |
| **`build`** | working code; per-task review per tier; final integration review; converge loop | plan (or tickets) ready and tests RED |
| **`review-commit`** | two-axis pass → R1 → budgeted confirm rounds → commit/deploy gate | after the build, through commit and deploy |

**Test ownership:** the plan names each test case (behavior, seam, expected
value and its source); `test-evals` writes those tests and runs them RED;
`build` implementers make them GREEN and don't rewrite them. An implementer
adds a test only for behavior the plan missed (a bug found mid-build) — under
TDD, and listed in its report.

## Two execution paths

**Path A — subagent-driven (default, T2/T3):** `grill → spec (spec → clarify → plan → tickets) → test-evals → build → review-commit`.
A fresh implementer subagent per task with per-task review.

**Path B — single CLI agent (T1, or T2 greenfield under `size-tiers.md` rule 7):** `spec (spec and plan turns) → test-evals → builder lane CLI → review-commit`.
The spec feeds one self-contained implementation brief; `review-commit`'s
loop is the quality gate instead of per-task review.

## Routing rules

1. **Tier first.** Step 0, always — even when the user says "just build it".
2. **Plan fuzzy?** `grill`. Skip if the plan is clear.
3. **No spec, T2/T3?** `spec` spec turn. At T1, put 3-6 acceptance criteria in
   the plan header instead.
4. **Spec, no plan?** `spec` plan turn (template: `spec/references/plan-template.md`).
5. **Want parallel units?** `spec` tickets turn (`spec/references/ticket-slicing.md`). Optional.
6. **Plan, no tests?** `test-evals` — RED tests for the plan's test cases.
7. **Tests RED, not built?** `build`.
8. **All tasks done?** Converge loop (`build/references/converge-loop.md`), within the tier's cap.
9. **Need a review of a diff?** `review-commit`.
10. **Periodic architecture check?** `improve-codebase-architecture` (outside skill).
11. **"Just build it"** — that's T0 or T1: skip grill and the spec turn, and
    say so. The review steps still run at the tier's level.

## Quick start

1. Step 0: pick and state the tier.
2. Fuzzy plan → `grill`.
3. `spec` spec turn → `.hermes/specs/` (T2/T3), clarify if markers remain.
4. `spec` plan turn → `.hermes/plans/` (tickets optional).
5. `test-evals` → RED tests for the plan's test cases.
6. `build` → tasks with per-task review per tier → final review → converge.
7. `review-commit` → Step A → R1 → confirm rounds within budget → gate.

Load one stage skill at a time — each is separate for context economy.

## Outside-skill pointers

- **Kanban orchestration** — `kanban-orchestrator` (devops) owns spreading
  tickets across worker lanes.
- **Model pins** — `references/lanes.md` here. The outside `model-routing`
  skill covers provider and budget choices; it was older than `lanes.md` at
  v0.2 and should be reconciled with it.
- **Architecture health** — `improve-codebase-architecture` (outside skill).
- **Mid-build doubt** — `doubt-driven-development` (outside skill).
- **Lessons from past loops** — `review-lessons` (this plugin; load one file
  by case, on demand).
