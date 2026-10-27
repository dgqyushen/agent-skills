# Release Hygiene Checklist

Run before tagging or releasing, after all review fixes are committed and CI is green. (Moved in v0.2 from the old `build-review-loop.md`, Step 12.)

This is NOT engineering. It is the mechanical release hygiene that recurs every
release. Skipping it produces a tag that points at the wrong commit, version
pins that disagree with the tag, a missing CHANGELOG, and build artifacts
committed to the repo root. The user has corrected this pattern multiple times.

Run these steps after all review fixes are committed and CI is green, BEFORE
creating or moving a tag or publishing a release:

## 1. Bump ALL version pins

Search for the old version string across ALL files, not just the obvious ones:

```bash
grep -rn "0\.4\.0" plugin.yaml pyproject.toml VERSION README.md .claude-plugin/
```

Files that typically carry version pins:
- `plugin.yaml` (version field)
- `pyproject.toml` (version field)
- the entrypoint script (VERSION constant)
- `README.md` (status line)
- `.claude-plugin/plugin.json` (version field)
- `.claude-plugin/marketplace.json` (version field)
- Any `VERSION` file if one exists

Update ALL of them in one commit. The `test_release_versions_agree` test
checks internal consistency (all pins agree with each other) but does NOT
check against the git tag. You must verify manually.

## 2. Add CHANGELOG entry

Add a new `## vX.Y.Z` section at the top of `CHANGELOG.md` (below the
`# Changelog` header). Include:
- One-line summary of the release theme
- Release posture (production-ready, beta, etc.)
- Key changes grouped by feature area
- Review summary (which models reviewed, how many findings, all fixed)

## 3. Remove ALL build artifacts from the repo root

Internal AI workflow files must NEVER be committed to the repo. These include:
- `BRIEF.md`, `BRIEF_*.md`, `FIX_BRIEF_*.md` — spec/brief files for Cursor
- `codex-review.md`, `opus-review.md`, `opus-review-raw.txt` — review outputs
- `review-prompt.md`, `review.diff` — review inputs
- Any `*.diff` files generated for review

```bash
git rm BRIEF*.md FIX_BRIEF*.md *review*.md *review*.txt *review*.diff 2>/dev/null
```

If the repo has pruned these before, it will need to prune them again. The
rule is simple: nothing in the repo root that is not part of the shipped
product. Briefs and reviews go in `/tmp/` or the Hermes workspace, not the repo.

## 4. Move the tag to the final commit

If a tag was created before the hygiene commit, it now points at the wrong
commit. Delete and recreate it:

```bash
git tag -d vX.Y.Z
git tag vX.Y.Z HEAD
git push origin vX.Y.Z --force
```

If a GitHub release exists, delete and recreate it (the API rejects
`gh release edit --target` when the commitish changes):

```bash
gh release delete vX.Y.Z --yes
gh release create vX.Y.Z --prerelease --title "..." --notes "..."
```

## 5. Verify

```bash
# Tag points at the latest commit
git rev-parse vX.Y.Z          # should equal git rev-parse HEAD
# No version drift
grep -rn "0\.4\.0" .          # should return nothing (old version gone)
# No build artifacts in repo root
ls BRIEF*.md *review*.md *review*.diff 2>/dev/null  # should be empty
# CHANGELOG has the new entry
head -3 CHANGELOG.md
# CI green
gh run list --limit 1 --json conclusion -q '.[0].conclusion'
```

## Common failure modes

| Failure | What happens | When it was caught |
|---|---|---|
| Tag points at pre-hygiene commit | Release includes old version pins | An early-beta release |
| Version pins not bumped | Software self-reports 0.4.0 while tag says 0.5.0 | An early-beta release |
| No CHANGELOG entry | Users clone the repo and see no record of what changed | An early-beta release |
| Build artifacts committed | Lead clones repo and finds 530KB diff + AI review transcripts | An early-beta release (pruned twice before) |
| test_release_versions_agree passes | It checks internal consistency, not tag alignment — false confidence | An early-beta release |

