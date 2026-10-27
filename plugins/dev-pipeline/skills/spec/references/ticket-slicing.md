# Ticket slicing

Break a settled spec, plan, or conversation into **tracer-bullet tickets** — vertical slices that cut through every layer (schema, API, UI, tests), each declaring its blocking edges — for the dev-pipeline spec stage.

The task-decomposition doctrine (tracer bullets, blocking edges, expand-contract, "The Tracer Bullet", "Vertical slice rules", "The wide-refactor exception") is defined once in `task-doctrine.md`; this file applies it to tickets.

## Contents

- When to Use
- Overview
- Process
  - 1. Gather context
  - 2. Explore the codebase (optional)
  - 3. Draft vertical slices
  - 4. Quiz the user
  - 5. Publish the tickets
  - 6. Work the frontier
- Ticket file format and template
- Content rules
- Completion Criteria
- Common Pitfalls
- Where it fits

## When to Use

- After the spec or plan is settled and you need to break it into implementation tickets
- User says "break this into tickets", "slice this up", "create tickets", "what are the work units"
- You want parallelizable work units with explicit dependency edges
- The work is large enough that doing it all in one context window would lose track

**Don't use for:**

- Single-task features that fit in one plan task — just plan it
- Research or exploration (no tickets needed)
- Post-hoc review

## Overview

Break a spec, plan, or the current conversation into a set of **tickets** — tracer-bullet vertical slices, each declaring the tickets that **block** it. Each ticket is sized to fit in a single fresh context window and is demoable or verifiable on its own.

## Process

### 1. Gather context

Work from whatever is already in the conversation. If the user passes a reference (spec path, issue number, URL), fetch and read its full body and comments.

### 2. Explore the codebase (optional)

If you haven't already explored the codebase, do so to understand the current state. Ticket titles and descriptions should use the project's domain vocabulary.

Look for **prefactoring opportunities** — "make the change easy, then make the easy change." Prefactoring tickets go first.

### 3. Draft vertical slices

Break the work into tracer-bullet tickets following the vertical slice rules in `task-doctrine.md`. Give each ticket its **blocking edges** — the other tickets that must complete before it can start. A ticket with no blockers can start immediately.

### The wide-refactor exception, applied to tickets

A wide refactor must not be forced into a tracer bullet — sequence it as expand-contract (definition in `task-doctrine.md`). Ticket-specific option: when even the migrate batches can't stay green alone, they share an integration branch that all block a final **integrate-and-verify** ticket — green is promised only there.

### 4. Quiz the user

Present the proposed breakdown as a numbered list. For each ticket:

- **Title**: short descriptive name
- **Blocked by**: which other tickets (if any) must complete first
- **What it delivers**: the end-to-end behaviour this ticket makes work

Ask the user:

- Does the granularity feel right? (too coarse / too fine)
- Are the blocking edges correct — does each ticket only depend on tickets that genuinely gate it?
- Should any tickets be merged or split further?

Iterate until the user approves the breakdown.

### 5. Publish the tickets

Write one file per ticket under `.hermes/tickets/<feature-slug>/`:

```
Ticket file: .hermes/tickets/<feature-slug>/NN-<slug>.md
```

Numbered from `01` in dependency order (blockers first). Each file's "Blocked by" lists the numbers/titles it depends on.

### 6. Work the frontier

The **frontier** is any ticket whose blockers are all done (defined once in `task-doctrine.md`). For a purely linear chain, that means top to bottom. For a DAG, multiple tickets may be unblocked simultaneously — they can be worked in parallel by fresh subagents.

## Ticket file format and template

### Local ticket template

```markdown
# <NN> — <Ticket title>

**What to build:** the end-to-end behaviour this ticket makes work, from the user's perspective — not a layer-by-layer implementation list.

**Blocked by:** the numbers/titles of the tickets that gate this one, or "None — can start immediately".

**Status:** ready-for-agent

- [ ] Acceptance criterion 1
- [ ] Acceptance criterion 2
```

### Content rules

- **No specific file paths or code snippets** — they go stale fast. Describe behaviour, not implementation.
- **Exception:** if a prototype produced a snippet that encodes a decision more precisely than prose (state machine, reducer, schema, type shape), inline it and note it came from a prototype. Trim to the decision-rich parts.
- Each ticket declares **what to build** (behaviour) and **what blocks it** (dependency edges).

## Completion Criteria

- Every ticket is a vertical slice (cuts through all layers, demoable on its own)
- Every ticket has explicit blocking edges (or "None — can start immediately")
- Each ticket fits in a single fresh context window
- User has approved the breakdown
- Tickets are saved to `.hermes/tickets/<feature-slug>/`
- Ticket files are numbered in dependency order (blockers first)

## Common Pitfalls

1. **Horizontal slicing.** "Build all the schema, then all the API, then all the UI" — nothing works until every layer lands. Slice vertically instead.

2. **No blocking edges.** A flat list of tasks with no dependencies. Without edges, you can't identify the frontier or run tickets in parallel.

3. **Too coarse.** A ticket that takes 3 context windows to implement. Split it until each fits in one fresh context.

4. **Too fine.** 20 tickets for a 3-feature change. Merge tickets that always run together and can't be independently rejected.

5. **Forcing wide refactors into tracer bullets.** A rename that touches 50 files can't land green in a vertical slice. Use expand-contract.

6. **Including code snippets.** "Create `src/models/user.py` with `class User: ...`" — the path and code will be stale by the time the ticket is picked up. Describe behaviour, not implementation.

## Where it fits

Ticket slicing sits between the plan and execution: the spec captures WHAT/WHY, the plan captures HOW (files, code, tasks), tickets slice the plan into parallelizable work units with dependency edges, and `subagent-driven-development` executes each ticket with a fresh subagent. The whole-pipeline flow map lives in the pipeline's orchestrator stage.
