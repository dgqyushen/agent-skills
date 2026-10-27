### Verify requested command surfaces, not just canonical files

When implementing or testing a packaged tool/skill, distinguish the canonical source layout from the exact commands the user asked to run. If the user says all files must live under `category/skill-name/` but also requests repo-root commands such as `python scripts/tool.py` or `python -m pytest tests/test_tool.py`, satisfy both deliberately: keep canonical source files in the package and add a minimal root-level wrapper/symlink only if needed for the requested command to work. Prefer symlinks or tiny shims over duplicated source. For skill repos that keep tests under `category/skill-name/tests/` but the user asks for `python -m pytest tests/test_<tool>.py`, add a root-level test symlink to the canonical test file rather than duplicating the suite; note the symlink in the final file list. Create real symlinks with filesystem commands (`ln -s` via terminal or equivalent), not `write_file`: writing the target path into a `.py` file produces an invalid Python file rather than a symlink. Make executable scripts executable at the canonical path, remove generated `__pycache__` and test cache directories before reporting, then collect file lists/line counts from source files only so binary cache files do not pollute the deliverable summary. Verify the exact user-specified commands after any compatibility wrapper is added, and report compatibility wrappers/symlinks separately so they are not confused with canonical source.

If one requested verification command fails and you fix it, rerun the complete user-requested verification surface, not only the previously failing command. For skill/package creation tasks, this usually means rerunning help output, a no-source/default execution path, and the root-level pytest command before collecting final file lists and line counts.

For config-dependent CLI utilities, verify in two layers: (1) the exact requested static check (for example `python3 -m py_compile path/to/tool.py`), and (2) a smoke test with a temporary config/project root when the repo lacks a live config. Set the tool's documented config environment variable rather than creating persistent repo config. Exercise every subcommand and both human/JSON output modes when cheap. If a negative-path test needs malformed files, remember that `write_file` may refuse syntactically invalid JSON; use a one-off shell/Python write in a temp directory, then clean the temp directory before final reporting. Do not treat a missing live config as a tool failure if an example config or env override verifies the behavior.

When adding a CLI into an active repository, inspect for pre-existing tests/docs that target the same feature before finalizing, even if the user only requested a file and a `py_compile` check. Other agents may have already staged untracked tests or documentation; run the relevant discovered test file and adapt to its expected command surface rather than stopping at the narrower requested verification. If a patch tool warns that a sibling subagent modified a file after your read, re-read the affected region before continuing and avoid overwriting unrelated concurrent changes.

Pitfall: **argparse global vs subcommand options can silently drop config** — if a CLI supports both `tool --config cfg subcmd ...` and `tool subcmd ... --config cfg`, do not add the same `--config` destination to both the root parser and subparsers. The subparser default can overwrite the already-parsed global value with `None`, causing the tool to fall back to default config. Use a hidden subcommand option with a separate destination (e.g. `dest="sub_config", default=argparse.SUPPRESS`) and after parsing set `args.config = args.sub_config` only when present. Smoke-test both option positions.

For `~/agent_skills/<category>/<skill>/` tasks, treat the requested skill directory as canonical even though the repository may also have root-level CI/test entrypoints. Create the full skill package there (`SKILL.md`, `scripts/`, `references/`, `tests/`), then add only the compatibility surface needed by the user's exact verification command. Before final reporting, list both canonical files and any root-level symlinks/shims with line counts so the compatibility layer is visible.

### Verify known bugs before fixing

When returning to a project with "known bugs" from a prior session, **verify each bug still exists in the current codebase before attempting a fix.** Code may have been fixed in commits between sessions. Reading the relevant function and checking for the reported behavior saves time and avoids introducing redundant guards or re-fixing already-resolved issues.

**Pattern:**
1. `git pull` to get the latest code
2. For each "known bug": `read_file` the relevant function, check if the reported behavior still exists
3. Mark each as CONFIRMED (fix it) or ALREADY FIXED (skip)
4. Only fix CONFIRMED bugs

This session: 4 "known bugs" from a prior session were all already fixed in commits made between sessions. Verifying saved ~30 minutes of unnecessary work.

### Batch read, then batch fix

When fixing 10+ bugs across multiple files:
1. **Read all affected files first** (parallel `read_file` calls) — understand the full scope before touching anything
2. **Apply fixes in sequence** — patch each bug one at a time, verifying lint passes after each
3. **Run the full test suite once** at the end, not after each fix
4. **Write all regression tests** in a single new test file

This avoids context-switching between reading and patching, and catches placeholder-count mismatches (e.g., adding a column to an INSERT but forgetting to add a `?` to VALUES) before running tests. When you hit `OperationalError: N values for M columns`, count both the column names and `?` placeholders — they must be equal.
