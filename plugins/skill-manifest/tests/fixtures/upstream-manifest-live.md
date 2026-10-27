# Upstream Tracking Manifest

<!-- Single source of truth for adapted and published skills. -->
<!-- Maintain at <your-skills-root>/UPSTREAM_MANIFEST.md -->
<!-- Audit monthly or when prompted. Update last-checked dates after each audit. -->

## Layer 1 — External → Local (adapted skills)

Skills adapted from external repos. Upstream may add features, fix bugs, or
change workflows. Check periodically and sync.

### Whole skills

| Skill | Source repo | Upstream path | Local ver | Upstream ver | Last sync | Status | Verbatim files | Adapted files | Sync method | License | PORT_NOTES |
|-------|------------|---------------|-----------|-------------|-----------|--------|---------------|--------------|-------------|---------|------------|
| `research/reddit-keyless-search` | [mvanhorn/last30days-skill](https://github.com/mvanhorn/last30days-skill) | skills/last30days/scripts/lib/reddit_keyless.py | 1.0.0 | 3.14.0 | 2026-07-14 | ✅ current | scripts/reddit_keyless.py (adapted) | SKILL.md | diff keyless modules, cherry-pick fixes | MIT | ❌ |
| `research/xiaohongshu-search` | [xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) | README.md | 1.2.0 | 2.0.0 | 2026-07-14 | ✅ current | — | SKILL.md | check tools/list schema vs SKILL.md docs | MIT | ❌ |
| `email/himalaya` | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (bundled) | skills/email/himalaya/SKILL.md | 1.2.0 | 1.1.0 | 2026-08-25 | ⚠️ local ahead | references/*.md (verbatim) | SKILL.md | `hermes skills diff himalaya`; merge upstream changes, preserve send guardrail section | MIT | ❌ |
| `software-development/harness-engineering` | [walkinglabs/learn-harness-engineering](https://github.com/walkinglabs/learn-harness-engineering) | skills/harness-creator/ | 1.1.0-local | 1.0.0 | 2026-10-02 | ✅ current | scripts/*.mjs, scripts/lib/harness-utils.mjs, templates/*, evals/evals.json (verbatim) | SKILL.md (full rewrite), references/course-notes.md (distilled from 14 lectures), references/gotchas.md (light adaptation) | GitHub API: compare `skills/harness-creator/**` file hashes + `metadata.json` version | MIT | ❌ |
| `creative/impeccable` | [pbakaus/impeccable](https://github.com/pbakaus/impeccable) | 4.5.0 (SKILL.md) | dea6cff (git) | 2026-10-05 | ✅ synced verbatim | Adapted (Apache-2.0, upstream NOTICE.md carries sub-licenses) | Check upstream releases on audit | Apache-2.0 |
| `software-development/hermes-agent-skill-authoring` | [hermes-agent builtin](https://github.com/NousResearch/hermes-agent) (bundled v2.0.0) | 2.1.0 (SKILL.md) | bundled tree | 2026-10-05 | ✅ rebased onto upstream v2.0.0 + local layer (degrees of freedom, reference hygiene + audit script, Pocock 2026-10 revisions, pitfalls 11-22) | Adapted (local override of a bundled skill; pre-rebase copy archived 2026-10-05) | Diff against `/usr/local/lib/hermes-agent/skills/...` on audit | MIT |

### Engine dependencies

Skills that wrap a third-party CLI or pip package. The skill itself is
original, but the engine can go stale.

| Skill | Tool | Installed ver | Upstream URL | Type | License | Audit method |
|-------|------|--------------|-------------|------|---------|-------------|
| `research/xiaohongshu-search` | `xiaohongshu-mcp-linux-amd64` | 2.0.0 | [xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp/releases/latest) | binary | MIT | `curl -sL https://api.github.com/repos/xpzouying/xiaohongshu-mcp/releases/latest \| grep tag_name` |
| `monid` (top-level) | `monid` CLI (@monid-ai/cli) | 0.1.7 | https://monid.ai/SKILL.md + `npm view @monid-ai/cli version` | npm | UNVERIFIED (no LICENSE in package) | `monid --version` vs `npm view @monid-ai/cli version`; skill file re-fetched from monid.ai/SKILL.md when CLI updates |
| `research/website-scraping` | `scrapling` | 0.4.14 | [D4Vinci/Scrapling](https://github.com/D4Vinci/Scrapling) | pip | BSD-3-Clause | `pip show scrapling` |

## Layer 2 — Local → Published Repo

Skills developed locally, then published (debranded + generalized) to a
shared repo. The local version is the source of truth.

| Skill | Repo category | Ver (both) | Repo path | Local path | Status |
|-------|-------------|------------|-----------|-----------|--------|
| `reddit-keyless-search` | research/ | 1.0.0 | `research/reddit-keyless-search/` | `research/reddit-keyless-search/` | ⚠️ local-only (not yet published) |
| `xiaohongshu-search` | research/ | 1.2.0 | `research/xiaohongshu-search/` | `research/xiaohongshu-search/` | ⚠️ local-only (not yet published) |

## Audit log

| Date | Layer(s) | Actions taken |
|------|----------|---------------|
| 2026-07-14 | all | Manifest created. reddit-keyless-search adapted from last30days v3.14.0. xiaohongshu-search updated for MCP v2.0.0 API drift. |
| 2026-08-13 | 2 | Published endpoint-probe; refreshed model-compare (embedding benchmark), claude-plugin-converter, library-rag (Nemotron migration), file-organizer. Public repo now spec-compliant (skills-ref) with CI validation job. Use `publish_skill.py` (skill-curation scripts) for future publishes — it copies, migrates frontmatter, scrubs, validates. |
| 2026-08-13 | 2 | library-rag: added `--prune-missing` + `--vacuum` (DB growth control) → both repos. Deleted local `~/.hermes/skills/note-taking/library-rag` (retired — migrated to Tablina). Lives on only in the repos as a standalone portable RAG for non-Tablina users. |
| 2026-08-19 | 1 | Installed scrapling 0.4.14 (parser-only) as engine dependency for `website-scraping`. Ported 3 concepts from D4Vinci/Scrapling agent skill into `references/scrapling.md`: `--ai-targeted` prompt injection protection, adaptive element relocation, CLI escalation ladder. Added step 0a (LLM sanitization) to SKILL.md verification section. Did NOT adopt the 24KB Scrapling agent skill — too large, duplicates our methodology. |
| 2026-08-25 | 1 | Added `email/himalaya` to manifest. Bundled skill (v1.1.0) patched with send guardrail section (draft-before-send protocol). Local v1.2.0, upstream v1.1.0. Status: local ahead. `hermes skills list-modified` tracks the modification; `hermes skills diff himalaya` shows the delta. |
| 2026-10-02 | 1(+plugin) | Created `software-development/harness-engineering` from walkinglabs/learn-harness-engineering (see Whole-skills row). Patched skill-retrieval plugin bm25_retriever.py: frontmatter triggers + hermes tags now join the BM25 indexed text (ranking only; prompt injection unchanged). Test suite: identical failure set pre/post (16 pre-existing); retrieval probes verified top-3 firing on target phrases without displacing specialist skills. | 0.1.7 (@monid-ai/cli) + skill SKILL.md verbatim at `skills/monid/`. Code audit: single bundled file, no child_process/eval, network only to api.monid.ai, credentials in ~/.config/monid/ (mode 600). API key added (label main). Trial policy applies: 2-week review (≈2026-10-12), bounded trial — exclude load-bearing paths, existing stack (DonSeTch, DataForSEO, direct APIs) takes precedence per skill's own precedence rules. |

## Audit checklist

1. Read the manifest — note last-checked dates, identify stale rows
2. Layer 1 (external → local): fetch upstream version, compare to pinned
   version. If different, run the Layer 1 sync workflow
3. Layer 2 (local → repo): diff local SKILL.md against repo copy. If local
   is ahead, re-publish
4. Layer 3 (local → standalone repo): check if the repo's workflow/CLI changed
5. Engine dependency scan: list installed CLI binaries and pip packages,
   compare versions, cross-reference with manifest
6. Update the manifest — record last-checked date, new upstream version, sync
   actions taken
7. Third-party skill scan: fetch upstream README/latest SKILL.md for adapted
   skills, compare concepts and features
