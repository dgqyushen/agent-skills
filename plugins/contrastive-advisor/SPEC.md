# SPEC — contrastive-advisor plugin v0.1.0

Advisory companion to the `contrastive-refinement` skill (user-approved
2026-10-19; per the skill-manifest convention: advisory-only, never blocks).

**Bundled skill (v0.3.0+):** this plugin is self-contained — it bundles
`contrastive-refinement` under `skills/contrastive-refinement/` and registers
it via `ctx.register_skill` as `contrastive-advisor:contrastive-refinement`.
A standalone skills-tree copy is unaffected; both copies can coexist (the
bare skill wins when present).

## Purpose

When a behavioral skill edit lands without a probe cycle, remind the agent
once, in-band, before the edit settles. The decision stays with the agent
and the user; the hook never blocks a call, never rewrites arguments, and
never judges anything with a model.

## Components

- `plugin.yaml` — name `contrastive-advisor`, v0.1.0, `provides_hooks:
  [pre_tool_call, pre_llm_call]`, config_schema, `requires_hermes: >=0.21.1`.
- `__init__.py` — `register(ctx)`: sibling-import fallback (gateway loader
  does not put the plugin dir on sys.path), wires both hooks, and registers
  the bundled skill (`skills/contrastive-refinement/`) under the qualified
  name `contrastive-advisor:contrastive-refinement`.
- `advisor.py` — pure logic, zero imports beyond stdlib (host-agnostic
  helpers) so tests run without the gateway.

## Hook 1: pre_tool_call observer

Triggers (all conditions required):
- `tool_name == "skill_manage"` with `action in {patch, create, edit}`,
  text from the single-op fields AND the `operations` array — the TEXT
  KEY UNION of both field-name families the real schema accepts
  (`old_text/new_text/content` aliases AND `old_string/new_string`):
  reading only the alias family makes the most common real edit shape
  (old_string/new_string) extract "" and stay silent — found live
  2026-10-19 (fire-test edit used `old_string`; extracted length 0);
  target NOT in
  the exclusion list; OR
- `tool_name in {patch, write_file}` whose path's BASENAME is exactly
  `SKILL.md` (backslash paths normalized; text from old_string/new_string
  and content).

Name validation (before any scan): the skill name must match
`^[a-z0-9][a-z0-9_-]{0,63}$` — traversal names, namespaced `x:y` names and
odd values are ignored silently. Namespaced names are catalog-tracked skills.

Behavioral heuristic (deterministic, no model): a BODY SECTION marker from
the markers list in the text, OR a combined text length ≥ 400 chars.
Frontmatter-only diffs and short strings stay silent.

Action on a behavioral edit: store ONE pending item in `ctx.state` key `v1`
-- fields: skill, tool, probe_dir, session_id, created_at. A fresh edit to
the same skill replaces its queued item (one nudge per skill per pending
window); the list is capped at 3. Return None ALWAYS — this hook is an
observer, never a gate.

## Hook 2: pre_llm_call injector

Return shape: `{"context": str}` (verified core contract) — never a bare
string. When pending non-empty AND not the first turn (is_first_turn True
skips; if the flag is absent, an empty conversation_history means turn 1):
serve the NEWEST valid pending item, inject a bounded advisory (cap 900):

⚠️ contrastive-advisor: behavioral edit to skill `<name>` is in flight. ...

The served item leaves the queue (one nudge), and the trimmed queue is
persisted even when everything expired (storage reclaim). TTL 30 min.

## Fire ledger (added v0.1.1)

Each DELIVERED advisory (this hook, served item path) appends one JSONL row
to `<probe_root>/fire-ledger.jsonl`: fired_at, skill, tool, session_id,
evidence status, answered_at, advisory (capped). Rationale: fires are
in-band and leave no host log line; after the TTL the pending item is gone,
so a delivered advisory would otherwise be unprovable at the trial review.
A prior open row for the same skill is closed by the next fire for that
skill when a valid cycle (probe.md + selection.md, depth <= 2) newer than
the open row's fired_at exists; evidence `found` at fire time marks the new
row answered immediately. Atomic tmp+replace write; malformed lines skipped;
failures logged-and-swallowed; `ledger_enabled: false` disables (zero I/O).
Bounded-scan rules match probe_evidence (CYCLE_SCAN_MAX_ENTRIES cap).

## Config (plugins.entries.contrastive-advisor.settings)

Keys are read RELATIVELY through ctx.get_config (`ctx.get_config("enabled")`
→ `plugins.entries.<id>.settings.enabled`) — the root `settings` is a
RESERVED config path in the current core loader (`_PLUGIN_SETTING_RESERVED_
ROOTS`: model, plugins, security, settings); requesting it raises ValueError
and logs `Rejected config path 'settings'` every call cycle while the hook
silently runs `enabled: False`. Found live 2026-10-19 (agent.log, 64+
warnings; zero pending stores). `load_settings` reads each key on its own,
guards each read individually (one rejected/absent key falls back to None
without disabling the rest), and enables ONLY on explicit `enabled: true`.

| key | default | meaning |
|-----|---------|---------|
| enabled | absent = disabled (opt-in; zero I/O); explicit false = disabled | must be `true` to arm |
| probe_root | `<hermes_home>/skill-probes` | cycle evidence root |
| excluded_skills | [contrastive-refinement] | union with defaults; a plain string wraps to a tuple |
| ttl_minutes | 30 | pending item decay |
| markers | body-section list | behavioral-signature markers |

Settings come ONLY through ctx.get_config — no filesystem/yaml fallback
(stdlib-only contract).

## Probe-evidence check

`probe_root/<skill>/` is scanned at depth ≤ 2 (`*/selection.md`,
`*/*/selection.md`) — UNSORTED generator iteration, never materializing the
glob set; the terminal check runs for EVERY consumed entry (cycle or not):
entry 50 is the last one any generator is asked for, and the bound is
shared across BOTH glob patterns (`CYCLE_SCAN_MAX_ENTRIES` 50 max
consumed, `CYCLE_MAX_COUNT` 10 reported, 90-day window). A cycle counts
only when the directory holds BOTH `probe.md` and `selection.md`. Limit:
with more fresh cycles than the cap, "latest" comes from the first ones
globs yield. Unreadable root → `unknown` status + a logged warning; never
an exception.

## Bounds and hygiene (round 2)

- Advisory truncation slices to `INJECT_HARD_CAP - 1` before appending the
  ellipsis, so the text never exceeds the 900 cap (Codex: verified 901 in
  round 2; the exact-900 test uses a non-whitespace cut point).
- `markers`/`excluded_skills` pass through `_as_tuple` BOTH in
  `load_settings` and `is_behavioral_edit` — a plain string is one value,
  never char-split.
- `ttl_minutes` must be a real number > 0 (bool rejected); anything else
  falls back to 30. Accepted quirk (Opus C4): a FRACTIONAL value like 0.5
  passes the guard but truncates to 0 at `int()`, which makes every pending
  item instantly expired — treat `ttl_minutes` as integer minutes ≥ 1;
  schema stays `number` for gateways that return floats.
- The injector writes state only when the queue changed — no per-turn
  churn on clean turns.
- A SKILL.md whose path contains a `plugins` path segment is a namespaced
  plugin skill and stays silent (the same x:y rule, applied to paths).
  Accepted edge: a Hermes home installed under a dir literally named
  `plugins` is also silenced — acceptable for this advisory.
- The pending queue is PROFILE-scoped through ctx.state. `session_id` is
  recorded on each item but NOT filtered on: one profile running several
  sessions shares one queue (documented design, Opus C3 note).
- Review artifacts (`*.log`, `review-*.diff`) are gitignored, never
  committed into the plugin tree.

## Test contract (tests/)

- Heuristic: behavioral (skill_manage single-op AND operations array, long
  markerless edit), silent (frontmatter-only, short markerless, no-marker
  + short, excluded skill, plugin-namespace name, traversal names, patch
  to non-SKILL.md path, NOTSKILL.md basename), patch-tool old_string path,
  write_file content + backslash path, create action, custom
  markers/excluded_skills, args-immutability.
- Observer: returns None unconditionally; pending stored with tool and
  session fields; newest wins; max 3 items; disabled config → no store
  write at all; missing config → disabled.
- Injector: returns {"context": str} (REAL contract shape); serves the
  NEWEST item; bounded text; exactly-once (injected → cleared); TTL decay
  reclaims storage even when all expired; first-turn skip (both signals:
  is_first_turn and the history-fallback); missing probe dir names the
  exact expected path; cycle needs BOTH probe.md and selection.md; depth-
  bounded scan; long name → text still under the cap.
- Fail-open: an internal helper raising does not propagate, and logs a
  ⚠️ line (stderr) before swallowing (kanban-mirror lesson: log before
  swallow) — asserted for observer, injector, state-read and probe-scan
  failures.
- Loader fidelity: TWO subprocess drivers with clean sys.modules — one
  execs `__init__.py` AS A PACKAGE (gateway context, relative import) and
  one as a PLAIN module (fallback import path); the package driver
  registers the parent namespace AND the module itself in sys.modules
  BEFORE exec_module and ASSERTS the relative import resolved
  (`advisor.__package__ == "hermes_plugins.contrastive_advisor"`); both
  drive a behavioral edit through the wired hooks and assert the pending
  store AND the injected {"context"} advisory. The H1 coverage tests use
  MARKERLESS base args so the marker must come from the operations array
  or content — they fail if the field is never read.
- Round-2 regressions (tests/test_round2.py): cap-exactly-900 under
  truncation, 30-cycle scan count-capped, visited bound terminates,
  markers-as-string not char-split, ttl 0/bool → 30, plugins-path SKILL.md
  silent, zero state writes on clean turns.
- Fixtures: tempfile under /root (tmpfs discipline); reviewer sandboxes
  may show OSError Errno 30 on /root — count only assertion failures.

## Non-goals (explicit)

- No blocking, no argument rewriting, no model calls, no network.
- No scanning of skill CONTENT quality — that is the cycle's job.
- No manifest/catalog publication until a live trial (same rule as
  skill-manifest: ~1 week, review-date decision).

## Fire sweeper (v0.2.0)

`on_session_start` -> `ensure_sweeper(ctx)`: lazily imports the cron modules,
adopts or creates the `contrastive-advisor fire clearance` cron job, and
records `sweeper_job_id` / `sweeper_user_removed` in plugin state. User
removal of the job is honored (never re-created); re-creating a same-name
job opts back in. Config: `sweeper` (default true), `sweeper_schedule`
(default `40 8 * * *`), `sweeper_deliver` (default `telegram`). All
failures log-and-swallow; bookkeeping never breaks the advisory path.
`render()` gains an optional `setup_note` appended after the body and
inside the 900-char cap: on a skill's first-ever ledger fire, one pointer
line to the sweeper. The cron prompt classifies unanswered fires per
contrastive-refinement (mechanical / execution-evidence / provenance-gap
close; unmeasured behavioral edits stay open) and never fabricates probe
evidence.
