---
name: input-token-overheads
description: "Use when context window is filling up too fast or input token cost is too high. Audits overhead sources."
license: MIT
metadata:
  version: 1.8.1
  author: moonlight-lupin
  platforms: [linux, macos, windows]
  tags: [tokens, overhead, context, optimization, agent-ops]
  related_skills: [skill-maintainer, input-token-analysis]
---

# Input Token Overheads

Audit every source of per-turn input token cost on a Hermes Agent instance. Measure each, rank by cost, act on the top consumers.

Three-way measurement (v1.8), category by category:

1. **Measured** (ground truth): `scripts/audit_overheads.py` starts a REAL prompt (`hermes -z "Reply with OK" --usage-file ...`) and reads the provider's actual `input_tokens` for a fresh session. Providers report totals only.
2. **Independent** (per category): the script builds Hermes' own offline inspection agent, renders the real system prompt parts, and measures each category with the build's rough tokenizer — system fixed text, skills index, memory, user profile, builtin/MCP/subagent tool schemas. It re-derives categories from the rendered prompt; it does not read prompt-size output.
3. **Budget**: `hermes prompt-size --json` byte accounting (bytes/4 estimator), used only as the comparison column.

Plus the context-doctor port (jzOcb/context-doctor, MIT; concepts only, script native): injection-cap checks — `TRUNCATED` (over cap, silently cut every turn), `MISSING` (broken reference). Exit 1 when any injected file needs attention.

## When to Use

- User says "token overhead", "context too large", "why is input so expensive"
- Model output quality degrades from context dilution
- Cost optimization — fewer input tokens per turn means lower API spend
- After adding skills, plugins, or tools — verify the overhead delta

## The Overhead Map

Verified on a live build (2026-10), cli surface, 44 tools:

| Category | Independent (rough tok) | Budget (bytes/4 tok) | Notes |
|---|---|---|---|
| Tool schemas | ~17.9k | ~17.9k | **Largest block.** Full JSON schema per enabled tool; builtin/mcp/subagent split available independently |
| Skills index | ~5.4k | ~5.4k | Full index, always-on in the **volatile** tier (moved there so skill edits don't invalidate the cached stable prefix). NOT top-K — top-K tuning does not reduce it |
| System fixed text | ~4.2k | ~4.1k | Identity/guidance; fixed cost |
| User profile | ~0.8k | ~0.8k | Injection cap 3,000 chars — over-cap tail is **silently cut every turn** |
| Memory (own facts) | 0 | 0 | 0 B when Mnemosyne owns recall; its per-turn recall injections sit outside the prompt-size budget — the live measured total includes them |
| Provider-measured input | — | ~25.1k tok | ~3.2k BELOW the sum of estimates — the provider tokenizer counts the JSON tool schemas tighter than chars/4 |

**On-demand (not per-turn):** full SKILL.md via `skill_view`, deferred tool schemas via `tool_describe`, reference files via `skill_view(file_path=...)`.

## Health Ratio

The health metric is **overhead ratio**: measured (or estimated) overhead tokens divided by the model's context window. The absolute number matters for cost; the ratio matters for quality.

| Ratio | Rating | Notes |
|-------|--------|-------|
| < 5% | Excellent | Most of the window available for conversation |
| 5-15% | Healthy | Normal for a capable agent with tools, skills, memory |
| 15-25% | Acceptable | Approaching the limit. Consider trimming. |
| > 25% | Unhealthy | Eats conversation capacity. Cost and quality risk. |

**Why the ratio matters:** Three studies confirm that input length degrades model performance independent of content quality:

1. **Lost in the Middle** (Liu et al., TACL 2023) — Models follow a U-shaped curve: best recall at the start and end of context, severe degradation in the middle. Overhead sits at the top of every turn, but it pushes conversation history into the degradation zone. [arxiv.org/abs/2307.03172](https://arxiv.org/abs/2307.03172)

2. **Same Task, More Tokens** (Levy et al., ACL 2024) — Reasoning performance degrades at input lengths far shorter than the model's stated maximum. The degradation appears even when the extra tokens are padding with no distracting content. The model's technical context window is not its effective context window. [aclanthology.org/2024.acl-long.818](https://aclanthology.org/2024.acl-long.818/)

3. **Context Length Alone Hurts** (Du et al., EMNLP 2025) — Performance degrades 14-85% as input length increases, even when retrieval is perfect, irrelevant tokens are replaced with whitespace, or all tokens except relevant ones are masked. The sheer length of the input is itself a limitation. [aclanthology.org/2025.findings-emnlp.1264](https://aclanthology.org/2025.findings-emnlp.1264)

**Cost compounding:** Overhead is paid every turn. At 10k tokens over 100 turns, that is 1M input tokens spent on overhead alone. Reducing overhead by 2k tokens saves 200k tokens per 100-turn session.

**Mitigations from the research:**

| Finding | Source | Action |
|---------|--------|--------|
| Models recall start and end of context best; middle degrades | Liu et al. 2023 | Keep overhead at the top (Hermes already does this). Avoid pushing critical conversation history into the middle — lower compression threshold if history is being compressed too aggressively |
| Reasoning degrades well below the stated context window maximum | Levy et al. 2024 | Treat the effective context window as 50-70% of the advertised maximum. Target an overhead ratio under 10% of the advertised window, not the effective one |
| Sheer input length hurts even with perfect retrieval and no distraction | Du et al. 2025 | Reduce overhead aggressively. Every 1k tokens of overhead removed improves reasoning quality, not just cost. The study's mitigation: prompt the model to recite key evidence before solving — equivalent to Hermes compression summarizing relevant context |
| Tool calling degrades 7-85% as tool catalog grows from 8k to 120k tokens | LongFuncEval (arxiv 2505.10570) | Keep the enabled toolset count low. Prefer deferred tools (loaded on demand) over always-on schemas. Disable unused toolsets |

## Procedure

### 1. Measure each overhead source

```bash
python3 scripts/audit_overheads.py     # from the input-token-overheads skill dir
```

One run: starts the live one-shot prompt (one inference call), then prints the category-by-category comparison table:

```
category                     indep est    budget    delta
system fixed text                4,177     4,065      112
skills index                     5,379     5,357       22
memory (own facts)                   0         0        0
user profile                       820       816        4
tool schemas (all)              17,885    17,884        1
TOTAL                           28,261    28,124      137
provider measured                   25,077
```

- **indep est** — re-derived from the rendered prompt with the build's rough tokenizer (no prompt-size output involved)
- **budget** — `hermes prompt-size` bytes/4
- **provider measured** — the real fresh-session `input_tokens`; the honest denominator for the health ratio
- Per-category deltas over a few hundred tokens = drift between what you think ships and what ships — investigate that category
- Tool split (builtin/mcp/subagent) prints under the table

Flags: `--platform cli|telegram|discord|...` (inspection + budget surface), `--skip-live` (no inference call; measured row reads "unmeasured"), `--json`, `--chart out.png`, `--ctx <window>`, `--live-timeout`. Exit 1 = an injected file needs attention.

Also measure what neither estimate sees: memory-provider recall size per turn (in-session: read a fresh turn's injected recall blocks) — it rides on top of the measured fresh-session total and grows with facts.

**Done:** category table read; health ratio computed from the provider-measured total; every injected-file status checked.

### 2. Rank by cost

The table ranks. Typical ranking:

1. **Tool schemas** — largest block. Scales with enabled toolset count.
2. **Skills index** — always-on full index. Scales with indexed-skill count.
3. **System fixed text** — fixed stable tier.
4. **User profile** — capped, but usually near budget on long-lived agents.
5. **Memory-provider recall** — dynamic, per-turn, outside the budget; measure in-session, trim facts if it grows.

**Done:** top 3 identified — these are the optimization targets.

### 3. Act on top consumers

**Tool schemas (largest block):**
- Prefer deferred tools (loaded on demand) over always-on tools — each deferral replaces a full JSON schema with a ~150 B catalog row
- Disable unused toolsets; check `requires_toolsets` in every skill's frontmatter before removing a toolset a skill depends on
- Use `platform_toolsets.<platform>` in config.yaml to control per-surface access (a Telegram surface needs fewer tools than the CLI)

**Truncated files (script exit 1):**
- A TRUNCATED user profile is a correctness problem, not a cost problem — the tail is lost every turn. Trim under cap (batch-shorten or remove stale entries) and re-run until `OK`

**Skills index:**
- Disable unused skills in `config.yaml` under `skills.disabled` — each removal drops ~200-300 chars from the always-on index
- Trim descriptions over ~200 chars — they waste index budget without improving routing

**Memory provider (if installed):**
- Run consolidation to move working to episodic, reducing the recall budget per turn
- Invalidate stale facts; lower the recall `limit` if context is tight

**Compression:** load `skill_view(name='hermes-compression-tuning')` for threshold and target-ratio tuning.

**Done:** at least one optimization applied to each top-3 source, every truncated file trimmed under cap.

### 4. Verify the delta

Re-run the audit script (live). Compare each category's delta and the provider-measured total before/after; confirm all injected files read `OK`.

**Done:** before/after per-category delta reported. If no meaningful reduction, the remaining overhead is structural (tool schemas + skills index) and needs config changes, not content trims.

## Common Pitfalls

| Problem | Cause | Fix |
|---------|-------|-----|
| Measured row reads "unmeasured" | Run with `--skip-live`, or the one-shot failed to write its usage file | Run without `--skip-live`; check the live-run error line |
| Skills index independent estimate reads 0 while budget shows thousands | The render's `<available_skills>` block was searched in the wrong tier | The script searches volatile first, then stable (current layout keeps the index in volatile) |
| Provider measured ~3k below the sum of estimates | Tokenizer difference: chars/4 and the rough estimator both over-count JSON tool schemas | Trust the provider number for ratios; use category deltas for drift detection, not absolute truth |
| Memory block 0 B but recall text arrives every turn | Provider (Mnemosyne) injects recall outside the prompt-size budget | Measure recall in-session; trim memory facts, not the block |
| Skills index cost ignores top-K settings | Current builds inject the full index | Optimize via `skills.disabled` and description length, not top-K |
| User profile flagged TRUNCATED | Over the 3,000-char injection cap | Trim under cap; until then the tail is lost every turn |
| Disabling a toolset breaks a workflow | A skill depends on that toolset | Check `requires_toolsets` in the skill's frontmatter before disabling |
| Memory pruning removes a needed fact | Aggressive removal without checking last-used | Check recall_count and last_recalled before removing |
| Compression triggers too early / too late | `threshold` set too low / too high | Tune with `hermes-compression-tuning`; compression summaries themselves cost tokens |
| Mandatory skill overhead seems unavoidable | Configured in behavioral rules | Accept the cost, or remove the mandatory load requirement in config |

## Verification

- Audit script (live) prints a full category table with a provider-measured TOTAL row
- Re-run audit script — per-category deltas and the measured total dropped
- `hermes tools` — confirm only needed toolsets enabled
- Monitor next session: quality should not degrade from reduced context
