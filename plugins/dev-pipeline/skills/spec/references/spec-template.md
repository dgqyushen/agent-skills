# Spec document template

Full document structure for a spec artifact (the spec turn of the dev-pipeline spec stage). Save to `.hermes/specs/YYYY-MM-DD_HHMMSS-<slug>.md`.

## Contents

- Template
- Section notes (Implementation / Testing decisions, NFRs, constraints)
- Common Rationalizations
- Red Flags

## Template

```markdown
# [Feature Name] Specification

**Goal:** [One sentence — what this builds and why it exists]

## User Stories

### US-1: [Story name]
As a [who], I want to [what], so that [why].
**Acceptance criteria:**
- [ ] [Testable, measurable condition]
- [ ] [Testable, measurable condition]

### US-2: ...

## Implementation Decisions

A list of implementation decisions already settled during the conversation:
- The modules that will be built/modified
- The interfaces of those modules
- API contracts
- Schema changes
- Architectural decisions

Do NOT include specific file paths or code snippets — they go stale fast.
Exception: if a prototype produced a snippet that encodes a decision more
precisely than prose can (state machine, reducer, schema, type shape),
inline it within the relevant decision and note it came from a prototype.
Trim to the decision-rich parts, not a working demo.

## Testing Decisions

- Which seams the feature will be tested at (from the seam sketch)
- What makes a good test (test external behavior, not implementation details)
- Which modules will be tested
- Prior art for the tests (similar test types in the codebase)

## Non-Functional Requirements

- **Performance:** [e.g. "Search results in < 200ms at 10k records" — target, not tool]
- **Security:** [e.g. "All user inputs validated; no raw SQL"]
- **Reliability:** [e.g. "No data loss on ungraceful shutdown"]

## Constraints

- [Organizational / compliance / platform constraints the user stated as requirements — verbatim where possible]

## Out of Scope

- [What this change deliberately does NOT cover, to keep the spec bounded]

## Edge Cases & Error Behavior

- [What happens from the user's perspective when X fails / is empty / is invalid]

## Open Questions

- [NEEDS CLARIFICATION: specific question]
- [NEEDS CLARIFICATION: specific question]
```

## Section notes

- The WHAT/WHY boundary table (which sections belong in spec vs plan) is in the SKILL.md body — consult it before writing any of these sections.
- Implementation Decisions and Testing Decisions record what the conversation already settled; they are intent records, not designs. File paths and code snippets go stale — the prototype exception above is the only case for inlining code.
- Non-Functional Requirements are targets, not tools: "< 200ms", "all inputs validated".
- Open Questions are the `[NEEDS CLARIFICATION]` markers the clarify phase resolves.

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "This is simple, I don't need a spec" | Simple tasks don't need long specs, but they still need acceptance criteria. A two-line spec is fine. |
| "I'll write the spec after I code it" | That's documentation, not specification. The spec's value is in forcing clarity before code. |
| "The spec will slow us down" | A 15-minute spec prevents hours of rework. Waterfall in 15 minutes beats debugging in 15 hours. |
| "Requirements will change anyway" | That's why the spec is a living document. An outdated spec is still better than no spec. |
| "The user knows what they want" | Even clear requests have implicit assumptions. The spec surfaces those assumptions. |
| "I'll figure out the edge cases during implementation" | Edge cases discovered during implementation are 10x more expensive than edge cases named in the spec. |
| "Non-functional requirements can stay vague" | "Fast" and "secure" are not requirements. "< 200ms" and "all inputs validated" are. Measure it or drop it. |

## Red Flags

- Starting to write code without any written requirements
- Asking "should I just start building?" before clarifying what "done" means
- Implementing features not mentioned in any spec or task list
- Making architectural decisions without documenting them
- Skipping the spec because "it's obvious what to build"
- No `[NEEDS CLARIFICATION]` markers in a spec for a non-trivial feature — means assumptions are being silently filled in
- Tech stack leaking into the spec ("use React", "store in PostgreSQL") — that belongs in `plan`
- Acceptance criteria that say "works correctly" instead of measurable conditions
- Speculative features with no user story or constraint tracing them
