# Size Tiers — how much pipeline a change gets (single home)

Every piece of work gets a tier BEFORE the first stage runs. The tier decides
which stages run, how many reviewers look at it, and how many review rounds
it may spend. Other stage skills point here; they do not restate the table.

## Pick the tier

Take the HIGHEST tier any row matches.

| Signal | T0 Trivial | T1 Small | T2 Standard | T3 High-stakes |
|---|---|---|---|---|
| Expected diff | ≤ ~20 lines | ≤ ~300 lines | > 300 lines or multi-task | any size |
| Files touched | 1-2 | ≤ ~5 | more | any |
| Behavior change | none, or one obvious one | one behavior / one seam | several behaviors or seams | any |
| Risk class | — | — | — | auth/permissions, money, data migration or deletion, secrets, concurrency/locking, public release, live deploy |
| Examples | typo, version bump, log line, rename, config value | one endpoint, one CLI flag, one bug fix with a repro | a feature with 3+ tasks, a refactor across modules | schema migration, payment path, backup/restore, anything going live to users |

## What each tier runs

| | T0 | T1 | T2 | T3 |
|---|---|---|---|---|
| `grill` | no | only if the user's ask is ambiguous | if the plan is fuzzy | yes, unless the plan is already pinned |
| `spec` spec turn | no | no — 3-6 acceptance criteria inline in the plan header | yes | yes |
| `spec` plan turn | no | short plan (1 task) | yes | yes |
| Goldfish spec review | no | no | offer, after the approved plan | offer, after the approved plan |
| `test-evals` | a test only if behavior changes | yes | yes | yes |
| `build` per-task review | — | none (Step A covers it) | ONE reviewer per task, both verdicts (spec, then quality) in one report | two separate reviewers: spec compliance, then code quality |
| Converge loop | no | no (the Spec axis of Step A is the check) | cap 2 | cap 3 |
| `review-commit` Step A (two-axis) | no | yes | yes | yes |
| Step B reviewers (lanes) | 0-1 | 1 | 2, cross-model | 3 (the three-reviewer set) |
| Step B confirm-round budget | 0 | 1 | 2 | 3 |
| Three-reviewer gate | only if deploying live | only if deploying live | only if deploying live | yes, before live deploy |
| Orchestrator may hand-edit | yes | trivial fixes only | trivial fixes only | trivial fixes only |

"Confirm-round budget" counts rounds AFTER the first review (R1). So the
longest loop any tier can run without asking the user is R1 + 3 confirm rounds
= R4, at T3 — and a live deploy adds one gate round plus at most one re-gate
outside that budget (see `review-commit/references/confirm-round-protocol.md`).
Budget rules and the stop conditions live in
`review-commit/references/confirm-round-protocol.md`.

"Trivial fix" (any tier): ≤ ~10 lines, mechanical, no design choice — a
typo, a missing import, an off-by-one the reviewer pinned with a repro.
Say so when you do it. Everything else goes to the builder lane.

## Rules

1. **Say the tier out loud.** One line to the user before starting: "Treating
   this as T1 (one endpoint, ~150 lines, no risk class)." The user can
   override it either way. At T0/T1 add the assumptions line and the
   `step → verify: check` plan from `coding-principles.md` § Plan format for
   T0/T1 — that is their intent-pinning, since they skip grill and the spec.
2. **Tiers go up, never silently down.** If the work turns out bigger or
   riskier (a migration appears, the diff passes the size line, a risk class
   shows up), raise the tier, say why, and from then on use the higher tier's
   rules. Lowering a tier needs the user's OK.
3. **Size the diff, not the ticket.** A "small" ticket that produces 800 lines
   is T2. Re-check after the build, before Step B.
4. **Risk class beats size.** A 5-line change to an auth check is T3.
5. **A user's explicit instruction wins over the table.** "Review loop 3
   times", "all three reviewers", "just commit it" — do what they said, and
   mention once if it is well outside the tier's defaults.
6. **The three-reviewer gate is tied to live deploy, not to tier.** Any change
   going live gets it, whatever its tier (user's standing instruction).
7. **Path B (single CLI agent) is a T2 exception for greenfield builds**: it
   replaces per-task review with the review-commit Step B loop. The spec turn
   and the plan turn still both run at T2; Path B never skips intent pinning.
   State the exception when you pick it.
