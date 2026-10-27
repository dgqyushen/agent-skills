# skill-manifest

A Hermes Agent plugin that enforces the `UPSTREAM_MANIFEST.md` rule for a skill library: when a skill arrives (installed, copied in, or created), the skills root must have a manifest row acknowledging it — otherwise the plugin injects a bounded advisory listing the untracked skills. It also detects content changes in manifested skills between reviews and requires fingerprint-bound acknowledgment before drift counts as accepted.

## How it works

- **Arrival detection** — an `on_skill_lifecycle` hook watches skill installs/creations and records pending arrivals; a session drift probe sweeps the skills root for dirs with no manifest row.
- **Change tracking** — manifested skills are fingerprinted (file set + content hash, capped by `max_files_per_skill` / `max_bytes_per_skill`, with a per-probe scanning budget). Any content change since the last acknowledgment re-flags the row.
- **Acknowledgment** — cleared states are fingerprint-bound; a later edit invalidates the acknowledgment. A daily drift-clearance cron (auto-installed, configurable) reminds until rows are acknowledged. Manual edits to the manifest file carry a fingerprint header so rewrites are detected.
- **Manifest lint** — the `shared/scripts/manifest_lint.py` helper checks manifest structure (required columns, malformed rows) both from the hook and standalone.
- **Fail-soft** — malformed configs, missing Hermes core modules, or manifest-parse failures never abort the host turn; errors degrade to a no-op advisory.

## Configuration

All settings live under the plugin's `config_schema` in `plugin.yaml` (`manifest_filename`, `skills_root`, `disabled`, `max_reported`, `dismissed`, `auto_resolve`, `auto_resolve_schedule`, `auto_resolve_deliver`, `track_changes`, `scan_budget_s`, `max_files_per_skill`, `max_bytes_per_skill`). Secrets and enables come from `os.environ` only.

## Install

Copy or symlink `plugins/skill-manifest/` into `~/.hermes/plugins/` (or your profile's `plugins/`), then restart the gateway.

## Tests

```bash
python3 -m pytest plugins/skill-manifest/tests/
```

The suite runs against a synthetic skills root; an isolated `HERMES_HOME` is recommended so live installs cannot perturb assertions:

```bash
HERMES_HOME=/tmp/hermes-test pytest plugins/skill-manifest/tests -q
```

## License

[MIT](../../LICENSE) — part of the moonlight-lupin/agent-skills catalog.