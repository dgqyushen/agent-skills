# Task decomposition doctrine

Canonical, single home of the tracer-bullet / blocking-edges / expand-contract task-decomposition doctrine for the dev-pipeline spec stage (plan turn) and its ticket-slicing application (references/ticket-slicing.md).

## Contents

- Overview — write for an implementer with zero context
- Scope Check
- File Structure
- Tracer-Bullet Task Decomposition
  - Vertical slices, not horizontal layers
  - Blocking edges
  - Wide refactors — the expand-contract exception
  - Sizing for fresh context windows
- Task Right-Sizing
  - Pre-agreed seams
- Bite-Sized Step Granularity
- When a Full Implementation Plan Helps

## Overview

Write comprehensive implementation plans assuming the implementer has zero context for the codebase and questionable taste. Document everything they need: which files to touch, complete code, testing commands, docs to check, how to verify. Give them bite-sized tasks. DRY. YAGNI. TDD. Frequent commits.

Assume the implementer is a skilled developer but knows almost nothing about the toolset or problem domain. Assume they don't know good test design very well.

**Core principle:** A good plan makes implementation obvious. If someone has to guess, the plan is incomplete.

## Scope Check

If the spec covers multiple independent subsystems, suggest breaking it into separate plans — one per subsystem. Each plan should produce working, testable software on its own.

## File Structure

Before defining tasks, map out which files will be created or modified and what each one is responsible for. This is where decomposition decisions get locked in.

- Design units with clear boundaries and well-defined interfaces. Each file should have one clear responsibility.
- You reason best about code you can hold in context at once, and your edits are more reliable when files are focused. Prefer smaller, focused files over large ones that do too much.
- Files that change together should live together. Split by responsibility, not by technical layer.
- In existing codebases, follow established patterns. If the codebase uses large files, don't unilaterally restructure — but if a file you're modifying has grown unwieldy, including a split in the plan is reasonable.

This structure informs the task decomposition. Each task should produce self-contained changes that make sense independently.

## Tracer-Bullet Task Decomposition

### Vertical slices, not horizontal layers

Each task should be a **tracer bullet** — a thin vertical slice that cuts through all integration layers (schema, API, UI, tests) end-to-end. A completed slice is demoable or verifiable on its own.

**BAD (horizontal):** "Task 1: Build all schema. Task 2: Build all API. Task 3: Build all UI."
Nothing works until every layer lands.

**GOOD (vertical):** "Task 1: User can create an account (schema + API + UI + test). Task 2: User can log in (schema + API + UI + test)."
Each task works independently.

### Blocking edges

Give each task explicit **blocking edges** — the other tasks that must complete before it can start. A task with no blockers can start immediately. This enables:

- **Parallel execution** — tasks with no blocking edges can run in parallel via fresh subagents
- **Frontier scheduling** — the "frontier" is any task whose blockers are all done; work those next
- **Dependency clarity** — the user can see the critical path at a glance

Prefactoring belongs here too: any prefactoring should be done first, as its own task with no blockers — "make the change easy, then make the easy change."

### Wide refactors — the expand-contract exception

A **wide refactor** (rename a column, retype a shared symbol) has a blast radius that fans across the whole codebase — no vertical slice can land green. Don't force it into a tracer bullet; sequence it as **expand-contract**:

1. **Expand** — add the new form beside the old so nothing breaks (own task, no blockers)
2. **Migrate** — move call sites over in batches sized by blast radius (per package, per directory), each batch its own task blocked by the expand. CI stays green because the old form still exists
3. **Contract** — delete the old form once no caller remains (task blocked by every migrate batch)

When even the batches can't stay green alone, they share an integration branch that all block a final **integrate-and-verify** ticket — green is promised only there (see references/ticket-slicing.md).

### Sizing for fresh context windows

Each task should fit in a **single fresh context window** — a subagent can read the task brief and complete the task without running out of context. If a task needs 3+ subagent dispatches to finish, it's too coarse — split it.

## Task Right-Sizing

A task is the smallest unit that carries its own test cycle and is worth a fresh reviewer's gate. When drawing task boundaries: fold setup, configuration, scaffolding, and documentation steps into the task whose deliverable needs them; split only where a reviewer could meaningfully reject one task while approving its neighbor. Each task ends with an independently testable deliverable.

### Pre-agreed seams

Choose the **seam** — the stable interface a feature is tested at — before any code is written. Tests target the seam, not the implementation, so the code underneath can move without the tests moving. The seams should have been sketched during the spec turn; the plan locks them in.

- Prefer existing seams to new ones
- Use the highest seam possible (public API > internal helpers)
- The fewer seams, the better — ideally one per feature

## Bite-Sized Step Granularity

**Each STEP = 2-5 minutes of focused work.** A task is a few steps — one test cycle, one deliverable, one reviewer gate (Task Right-Sizing above). Sizing tasks at 2-5 minutes puts a full review on every step.

Every step is one action:

- "Write the failing test" — step
- "Run it to make sure it fails" — step
- "Implement the minimal code to make the test pass" — step
- "Run the tests and make sure they pass" — step
- "Commit" — step

**Too big:**

```markdown
### Task 1: Build authentication system
[50 lines of code across 5 files]
```

**Right size:**

```markdown
### Task 1: Create User model with email field
[10 lines, 1 file]

### Task 2: Add password hash field to User
[8 lines, 1 file]

### Task 3: Create password hashing utility
[15 lines, 1 file]
```

## When a Full Implementation Plan Helps

**Always use before:**

- Implementing multi-step features
- Breaking down complex requirements
- Delegating to subagents via the `build` stage

**Don't skip when:**

- Feature seems simple (assumptions cause bugs)
- You plan to implement it yourself (future you needs guidance)
- Working alone (documentation matters)
