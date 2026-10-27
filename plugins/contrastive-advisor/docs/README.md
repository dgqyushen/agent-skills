# contrastive-advisor

Advisory companion to the `contrastive-refinement` skill — **self-contained as
of v0.3.0**: the plugin bundles the skill under `skills/contrastive-refinement/`
and registers it with `ctx.register_skill` under the qualified name
`contrastive-advisor:contrastive-refinement`. A standalone skills-tree copy
(`skills/agent-ops/contrastive-refinement/` or equivalent) is unaffected — both
copies can coexist; the bare one keeps its identity and the bundled one only
appears when no bare copy is installed.

**What it does:** when a behavioral skill edit lands — a `skill_manage`
patch/create whose text touches skill body sections, or a patch/write_file
against a live `SKILL.md` — the plugin observes the call (it never blocks or
rewrites it) and injects ONE reminder on the next turn: run the probe cycle
from `contrastive-refinement`, and here is the state of the probe-evidence
directory for that skill (`~/.hermes/skill-probes/<skill>/<cycle>/` with
`probe.md` + `selection.md`). Both skill_manage shapes are observed: the
top-level single-op shape and the pure batch shape (`operations` entries
carrying per-op `name`+`action`; a delete op never fires). A markerless
edit stays silent under 400 chars of changed text, with or without a marker.

**What it never does:** block calls, rewrite arguments, call a model, judge
content quality. Advisory-only, per the skill-manifest convention.

Config (`plugins.entries.contrastive-advisor.settings`): the plugin is
OPT-IN — if no config entry exists, both hooks return None with zero I/O.
Within an entry: `enabled` (default true), `probe_root` (default
`<hermes_home>/skill-probes`), `excluded_skills` (UNION with the built-in
default: the refinement skill itself — a custom list does not replace it),
`ttl_minutes` (default 30; an invalid or non-positive value falls back to
30 — use integer minutes ≥ 1, a fractional value truncates to 0 and
expires everything), `markers` (a custom list REPLACES the default marker
list; a plain string is treated as one marker), `ledger_enabled` (default
true).

**Fire ledger (v0.1.1+):** every DELIVERED advisory appends one JSONL row to
`<probe_root>/fire-ledger.jsonl` — `fired_at`, `skill`, `tool`,
`session_id`, `evidence` (the probe-evidence status the advisory carried),
`answered_at`, `advisory` (text, capped). Fires are in-band only: without
the ledger, each advisory is unprovable once its 30-minute TTL expires. A
prior OPEN row for the same skill is closed (answered_at + cycle path set)
on the next fire for that skill if a cycle (both probe.md + selection.md)
with a selection.md mtime AFTER the open row's fired_at now exists. A fire
whose evidence status starts with `found` is answered at fire time. Writes
are atomic (tmp + replace), malformed ledger lines are skipped, all
failures log-and-swallow — bookkeeping never breaks the advisory path. Set
`ledger_enabled: false` to disable. Rows written by hand before v0.1.1
carry `"ledger_source": "backfill"`; live rows carry `session_id` and no
such key.

Tests: `python3 -m pytest tests/ -q` from the plugin dir.

## Fire sweeper (v0.2.0)

Fires are in-band and expire from their queue after `ttl_minutes`, but the
ledger keeps them as unanswered rows until something answers them. Without
help, a backlog of open rows grows and each row re-fires its advisory on
every future session that touches its skill. The plugin therefore
SELF-INSTALLS one clearance cron at session start (pattern ported from
skill-manifest's `Skill-manifest drift clearance`):

- **Job name:** `contrastive-advisor fire clearance`
- **Schedule:** daily (config `sweeper_schedule`, default `40 8 * * *`)
- **Deliver:** config `sweeper_deliver` (default `telegram`)
- **Disable:** config `sweeper: false`; or delete the job — the plugin
  records the removal (`sweeper_user_removed`) and never re-creates it.
  Hand-recreating a job of the same name opts back in.

The sweeper run takes the oldest 4 unanswered ledger rows per run and, per
`contrastive-refinement`, reconstructs each edit from the session store and
CLASSIFIES it: mechanical edit (no behavioral claim — close),
real-environment execution evidence — close; unresolved provenance — close
with a documented gap; a behavioral edit with no live evidence is LEFT OPEN
and escalated for an interactive probe cycle. It never fabricates probe
evidence. Classification rules follow harness^2 (arXiv:2610.14224):
KEEP_BASE is a real answer; lessons without evidence enter no library; the
sweeper is the library-consolidation work harness2 defers to future work
(Appendix G).

On the FIRST fire for a skill, the advisory text adds one pointer line to
this sweeper so a fresh install announces its cleanup mechanism.
