
# Two-Axis Code Review

## Contents

Sections: Overview - When to Use - Process (pin the fixed point, identify the spec source, identify the standards sources + the Fowler smell baseline table, spawn both sub-agents in parallel, aggregate) - Why two axes - Output format - Completion Criteria - Common Pitfalls - Where it fits.


## Overview

Review the diff between `HEAD` and a fixed point along **two separate axes**:

- **Standards** — does the code conform to this repo's documented coding standards + a built-in Fowler smell baseline?
- **Spec** — does the code faithfully implement the originating issue / PRD / spec?

Both axes run as **parallel sub-agents** so they don't pollute each other's context, then this skill aggregates their findings. The two reports are **never merged or re-ranked** — a change can pass one axis and fail the other, and blending lets one mask the other.

## When to Use

- Reviewing a branch, PR, or work-in-progress changes against a fixed point
- User asks to "review since X" or "review this diff"
- You want the two questions — *is it built right?* and *is it the right thing?* — answered independently
- Any code review where you want structured, axis-separated findings

**Don't use for:**
- Iterative fix → re-review loops → that is Step B of this stage (`confirm-round-protocol.md`)
- 3-model parallel review (same axis, different models) → `council-code-review` (outside skill)
- Production readiness assessment → `production-readiness-review` (outside skill)

## Process

### 1. Pin the fixed point

Whatever the user said is the fixed point — a commit SHA, branch name, tag, `main`, `HEAD~5`, etc. If they didn't specify one, ask.

Capture the diff command: `git diff <fixed-point>...HEAD` (three-dot, comparison against merge-base).
Also note commits: `git log <fixed-point>..HEAD --oneline`.

**Fail fast:** confirm the fixed point resolves (`git rev-parse <fixed-point>`) and the diff is non-empty. A bad ref or empty diff should fail here — not inside parallel sub-agents.

### 2. Identify the spec source

Look for the originating spec, in this order:

1. Issue references in commit messages (`#123`, `Closes #45`, etc.) — fetch via `gh issue view` or the issue tracker.
2. A path the user passed as an argument.
3. A PRD/spec file under `docs/`, `specs/`, `.hermes/specs/`, or `.scratch/` matching the branch name or feature.
4. If nothing found, ask the user. If they say there isn't one, the **Spec** sub-agent skips and reports "no spec available."

### 3. Identify the standards sources

Anything in the repo that documents how code should be written: `CODING_STANDARDS.md`, `CONTRIBUTING.md`, `CLAUDE.md`, `AGENTS.md`, linter configs, style guides.

On top of repo-specific standards, the **Standards** axis always carries the **smell baseline** below — a fixed set of Fowler code smells (plus two change-discipline checks) that applies even when a repo documents nothing.

### The Fowler smell baseline

Two rules bind it:

- **The repo overrides.** A documented repo standard always wins; where it endorses something the baseline would flag, suppress the smell.
- **Always a judgement call.** Each smell is a labelled heuristic ("possible Feature Envy"), never a hard violation. Skip anything tooling already enforces.

Each smell: *what it is* → *how to fix it*. Match against the diff:

| Smell | Signal | Fix |
|---|---|---|
| **Mysterious Name** | A function/variable/type whose name doesn't reveal what it does | Rename it; if no honest name comes, the design's murky |
| **Duplicated Code** | Same logic shape appears in more than one hunk or file | Extract the shared shape, call it from both |
| **Feature Envy** | A method that reaches into another object's data more than its own | Move the method onto the data it envies |
| **Data Clumps** | Same few fields/params keep travelling together | Bundle them into one type, pass that |
| **Primitive Obsession** | A primitive or string standing in for a domain concept | Give the concept its own small type |
| **Repeated Switches** | Same `switch`/`if`-cascade on the same type recurs across the change | Replace with polymorphism, or one map both sites share |
| **Shotgun Surgery** | One logical change forces scattered edits across many files | Gather what changes together into one module |
| **Divergent Change** | One file/module is edited for several unrelated reasons | Split so each module changes for one reason |
| **Speculative Generality** | Abstraction, parameters, or hooks added for needs the spec doesn't have | Delete it; inline back until a real need shows |
| **Message Chains** | Long `a.b().c().d()` navigation the caller shouldn't depend on | Hide the walk behind one method on the first object |
| **Middle Man** | A class/function that mostly just delegates onward | Cut it, call the real target direct |
| **Refused Bequest** | A subclass/implementer that ignores or overrides most of what it inherits | Drop the inheritance, use composition |
| **Overbuilt** *(coding principle 2)* | Far more code, options, layers or error handling than the change needs — handling for scenarios no entry point can reach, config nobody asked for, a 200-line answer to a 50-line problem | Cut to the minimum that passes the tests; name the lines to delete |
| **Drive-by Change** *(coding principle 3)* | Hunks that reformat, rename, reorder or refactor code unrelated to the change's purpose (as stated in the commit messages / brief), or delete pre-existing dead code nobody asked about | Revert the hunk; mention the dead code in the report instead |

The last two rows come from `orchestrator/references/coding-principles.md`
(single home); they are judgement calls like the rest of the baseline.

### 4. Spawn both sub-agents in parallel

Dispatch two `delegate_task` sub-agents. Send a single message with both calls so they run concurrently.

**Standards sub-agent prompt** — include:
- The full diff command and commit list.
- The list of standards-source files found in step 3, **plus the smell baseline above** pasted in full — the sub-agent has no other access to it.
- The brief: *"Report — per file/hunk where relevant — (a) every place the diff violates a documented standard: cite the standard (file + the rule); and (b) any baseline smell you spot: name it and quote the hunk. Distinguish hard violations from judgement calls — documented-standard breaches can be hard, but baseline smells are always judgement calls, and a documented repo standard overrides the baseline. Skip anything tooling enforces. Under 400 words."*

**Spec sub-agent prompt** — include:
- The diff command and commit list.
- The path or fetched contents of the spec.
- The brief: *"Report: (a) requirements the spec asked for that are missing or partial; (b) behaviour in the diff that wasn't asked for (scope creep); (c) requirements that look implemented but where the implementation looks wrong. Quote the spec line for each finding. Under 400 words."*

If the spec is missing, skip the Spec sub-agent and note this in the final report.

### 5. Aggregate

Present the two reports under `## Standards` and `## Spec` headings, verbatim or lightly cleaned. Do **not** merge or rerank findings — the two axes are deliberately separate.

End with a one-line summary: total findings per axis, and the worst issue *within each axis* (if any). Don't pick a single winner across axes — that's the reranking the separation exists to prevent.

## Why two axes

A change can pass one axis and fail the other:

- Code that follows every standard but implements the wrong thing → **Standards pass, Spec fail.**
- Code that does exactly what the issue asked but breaks the project's conventions → **Spec pass, Standards fail.**

Reporting them separately stops one axis from masking the other.

## Output format

```markdown
## Code Review: <branch> vs <fixed-point>

**Diff:** `git diff <fixed-point>...HEAD` (<N> commits)

### Standards

<Standards sub-agent findings, per file/hunk>

### Spec

<Spec sub-agent findings, or "No spec available — Spec axis skipped.">

### Summary

- Standards: <N> findings, worst: <smell or violation>
- Spec: <N> findings, worst: <missing requirement / scope creep / wrong implementation>
```

## Completion Criteria

- Fixed point is pinned and confirmed (non-empty diff)
- Both sub-agents dispatched in parallel (or Spec skipped with reason)
- Standards findings cite either a repo standard or a named smell
- Spec findings quote the spec line for each finding
- Reports presented under separate headings, never merged
- Per-axis summary with worst finding

## Common Pitfalls

1. **Merging the axes.** "Overall the code looks good" — no. Standards and Spec are separate questions with separate answers. Don't blend them.

2. **Skipping the fail-fast check.** A bad ref or empty diff discovered inside sub-agents wastes two parallel dispatches. Check `git rev-parse` and diff non-emptiness first.

3. **Hard violations for smells.** Fowler smells are always judgement calls. Only documented-standard breaches can be hard violations. A repo standard that endorses what a smell would flag overrides the smell.

4. **Inventing spec requirements.** If no spec exists, say "no spec available." Don't infer requirements from the diff and then check the diff against them — that's circular.

5. **Re-ranking across axes.** "The worst finding overall is..." — no. The worst finding *within each axis*. Don't pick a winner across axes.

6. **Forgetting the repo override.** If `CONTRIBUTING.md` says "use dataclasses for all models" and the diff uses dataclasses, don't flag Primitive Obsession. The repo standard wins.

## Where it fits

This is **Step A** of the `review-commit` stage: one cheap, axis-separated
pass before any fix round. Each finding gets a severity label
(`confirm-round-protocol.md`) and enters Step B as a named finding. At T1
(`orchestrator/references/size-tiers.md`) the Spec axis also stands in for the
converge loop: a missing requirement found here is a MAJOR finding.
