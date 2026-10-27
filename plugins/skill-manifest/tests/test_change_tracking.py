"""Change tracking + fingerprint-bound acknowledgment (SPEC v0.3.0, section 10).

Reuses the FakeCtx/state patterns from tests/test_hooks.py. All fixtures
live in tmp_path; nothing is written under the real ~/.hermes.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from pathlib import Path

import pytest

import acks as acks_mod
import hooks
import manifest_db

PLUGIN = Path(__file__).resolve().parent.parent
_LINE = "Advisory only. Do not act on this unless the user asks."


class FakeState:
    def __init__(self):
        self._d = {}
        self.keys = []

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self.keys.append(key)
        self._d[key] = value


class FakeCtx:
    def __init__(self):
        self.config = {}
        self.hooks = []
        self.cli = []
        self.commands = []
        self.state = FakeState()

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value

    def register_hook(self, hook_name, callback):
        self.hooks.append((hook_name, callback))
        return callback

    def register_cli_command(self, name, *args, **kwargs):
        self.cli.append((name, args, kwargs))

    def register_command(self, name, *args, **kwargs):
        self.commands.append((name, args, kwargs))
        return None


@pytest.fixture(autouse=True)
def _reset_hooks():
    hooks._reset_runtime()
    hooks._session_signatures.clear()
    hooks._sessions_loaded = False
    hooks.configure(None)
    yield
    hooks._reset_runtime()
    hooks._session_signatures.clear()
    hooks._sessions_loaded = False
    hooks.configure(None)


@pytest.fixture(autouse=True)
def _isolate_acks(tmp_path, monkeypatch):
    """_probe must never read the real ~/.hermes acks.json in tests."""
    if hooks.acks is None:
        yield
        return
    monkeypatch.setattr(
        hooks.acks, "default_acks_path", lambda *a, **k: tmp_path / "acks.json"
    )
    yield


def _plain_init():
    sys.path.insert(0, str(PLUGIN))
    name = "skill_manifest_plain_init_change_tests"
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN / "__init__.py", submodule_search_locations=None
    )
    mod = sys.modules.get(name)
    if mod is None:
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def _wire(ctx: FakeCtx) -> dict:
    mod = _plain_init()
    ctx.hooks.clear()
    mod.register(ctx)
    hooks.configure(ctx)
    return {event: cb for event, cb in ctx.hooks}


def _skill(root: Path, name: str, version=None, body: str = "hello") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    front = "---\n"
    if version is not None:
        front += f"version: {version}\n"
    front += "---\n"
    (d / "SKILL.md").write_text(front + body + "\n", encoding="utf-8")
    return d


def _manifest(root: Path, names: list[str], versions=None, extra: str = "") -> None:
    versions = versions or {}
    if versions:
        header = "| Skill | Source | Local ver |\n| --- | --- | --- |\n"
        rows = "\n".join(f"| {n} | src | {versions.get(n, '')} |" for n in names)
    else:
        header = "| Skill | Source |\n| --- | --- |\n"
        rows = "\n".join(f"| {n} | src |" for n in names)
    (root / "UPSTREAM_MANIFEST.md").write_text(
        header + rows + "\n" + extra, encoding="utf-8"
    )


def _scan(ctx: FakeCtx, cbs: dict, sid: str = "s") -> dict:
    cbs["on_session_start"](session_id=sid)
    return ctx.state.get("v1")


def _reconcile(
    state: dict,
    root: Path,
    names: list[str] | None = None,
    *,
    now: float = 1000.0,
    budget: float = 5.0,
    max_files: int = 400,
    max_bytes: int = 8_000_000,
    acks_dict=None,
) -> dict:
    """Drive hooks.reconcile_changes with explicit options (no _probe)."""
    import fingerprint as fp

    dirs = fp.list_skill_dirs(root)
    doc = manifest_db.load_manifest(root / "UPSTREAM_MANIFEST.md")
    manifested = set(names) if names is not None else set(doc.skills)
    hooks.reconcile_changes(
        state,
        dirs,
        manifested,
        options={
            "max_files": max_files,
            "max_bytes": max_bytes,
            "scan_budget_s": budget,
            "now": now,
            "rows": dict(getattr(doc, "rows", {}) or {}),
            "acks": dict(acks_dict or {}),
        },
    )
    return state


def _fresh_state(**overrides) -> dict:
    state = hooks._empty_state()
    state.update(overrides)
    return state


def _assert_pending_findings_invariant(state: dict) -> None:
    """SPEC section 3: pending names and findings entries match 1:1, except
    dismissed skills and manifested-and-acked skills (which hold neither)."""
    findings = state.get("findings") or {}
    pending = state.get("pending") or []
    dismissed = {
        d.lower() for d in (state.get("dismissed") or []) if isinstance(d, str)
    }
    fps = state.get("fingerprints") or {}
    for name in pending:
        assert isinstance(name, str)
        assert name.lower() in findings, name
    for key, entry in findings.items():
        assert isinstance(entry, dict)
        assert any(p.lower() == key for p in pending if isinstance(p, str)), key
        if entry.get("reason") in ("changed", "version-mismatch"):
            assert key not in dismissed, key
        if entry.get("reason") == "changed":
            # A changed finding is, by definition, not yet acknowledged.
            # (version-mismatch findings sit exactly on the acked digest.)
            acked = fps.get(key, {}).get("ack") if isinstance(fps.get(key), dict) else ""
            assert entry.get("digest") != (acked or ""), key


def test_01_baseline_adoption_produces_zero_findings(tmp_path):
    """3 manifested skills, first scan: 0 findings, change_baseline_done true."""
    root = tmp_path / "skills"
    for name in ("alpha", "beta", "gamma"):
        _skill(root, name)
    _manifest(root, ["alpha", "beta", "gamma"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    state = _scan(ctx, cbs)
    assert state["findings"] == {}
    assert state["pending"] == []
    assert state["change_baseline_done"] is True
    assert set(state["fingerprints"]) == {"alpha", "beta", "gamma"}
    for entry in state["fingerprints"].values():
        assert entry["ack"] == entry["digest"] and entry["digest"]
    _assert_pending_findings_invariant(state)


def test_02_skill_md_replacement_reports_changed(tmp_path):
    """SKILL.md content replacement on a manifested skill -> changed."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    before = _scan(ctx, cbs)
    old_digest = before["fingerprints"]["alpha"]["digest"]
    (_skill(root, "alpha")).joinpath("SKILL.md").write_text(
        "---\n---\ncompletely different content\n", encoding="utf-8"
    )
    after = _scan(ctx, cbs, sid="s2")
    finding = after["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "changed"
    assert finding["digest"] != old_digest
    assert "alpha" in after["pending"]
    _assert_pending_findings_invariant(after)


def test_03_reference_only_change_reports_changed(tmp_path):
    """A references/ edit with SKILL.md untouched still reports changed."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    ref = root / "alpha" / "references"
    ref.mkdir()
    (ref / "notes.md").write_text("v1\n", encoding="utf-8")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (ref / "notes.md").write_text("v2 with more detail\n", encoding="utf-8")
    after = _scan(ctx, cbs, sid="s2")
    assert after["findings"].get("alpha", {}).get("reason") == "changed"
    _assert_pending_findings_invariant(after)


def test_04_script_only_change_reports_changed(tmp_path):
    """A scripts/ edit with SKILL.md untouched still reports changed."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    scripts = root / "alpha" / "scripts"
    scripts.mkdir()
    (scripts / "run.sh").write_text("#!/bin/sh\necho one\n", encoding="utf-8")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (scripts / "run.sh").write_text("#!/bin/sh\necho two\n", encoding="utf-8")
    after = _scan(ctx, cbs, sid="s2")
    assert after["findings"].get("alpha", {}).get("reason") == "changed"
    _assert_pending_findings_invariant(after)


def test_05_added_and_removed_files_report_changed(tmp_path):
    """An added file reports changed; a removed file also reports changed."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    extra = root / "alpha" / "extra.md"
    extra.write_text("bonus\n", encoding="utf-8")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "added.md").write_text("new\n", encoding="utf-8")
    added = _scan(ctx, cbs, sid="s2")
    assert added["findings"].get("alpha", {}).get("reason") == "changed"
    # Ack the added-file state through the file, then remove a file.
    live = dict(added["fingerprints"]["alpha"])
    ack_path = tmp_path / "acks.json"
    result = acks_mod.acknowledge(ack_path, root / "alpha", "alpha", live["digest"][:8])
    assert result["ok"] is True
    state = _fresh_state(
        fingerprints={"alpha": dict(live, ack=live["digest"])},
        change_baseline_done=True,
        dismissed=[],
    )
    extra.unlink()
    (root / "alpha" / "added.md").unlink()
    _reconcile(state, root, ["alpha"], now=2000.0)
    assert state["findings"].get("alpha", {}).get("reason") == "changed"


def test_06_content_change_without_version_bump_reports_changed(tmp_path):
    """Body edit with no version anywhere still reports changed (not version)."""
    root = tmp_path / "skills"
    _skill(root, "alpha", body="original body")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    _skill(root, "alpha", body="edited body, still no version")
    after = _scan(ctx, cbs, sid="s2")
    finding = after["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "changed"
    _assert_pending_findings_invariant(after)


def test_07_version_bump_plus_manifest_update_plus_ack_clears(tmp_path):
    """Bump SKILL.md + Local ver, ack the new digest -> finding clears."""
    root = tmp_path / "skills"
    _skill(root, "alpha", version="1.0.0")
    _manifest(root, ["alpha"], versions={"alpha": "1.0.0"})
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    _skill(root, "alpha", version="1.1.0", body="new stuff")
    _manifest(root, ["alpha"], versions={"alpha": "1.1.0"})
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    live = changed["fingerprints"]["alpha"]["digest"]
    ack_path = tmp_path / "acks.json"
    assert acks_mod.acknowledge(ack_path, root / "alpha", "alpha", live[:12])["ok"] is True
    cleared = _scan(ctx, cbs, sid="s3")
    assert "alpha" not in cleared["findings"]
    assert "alpha" not in cleared["pending"]
    _assert_pending_findings_invariant(cleared)


def test_08_manifest_version_edit_without_content_change(tmp_path):
    """Local ver bump with identical content -> version-mismatch (not changed)."""
    root = tmp_path / "skills"
    _skill(root, "alpha", version="1.0.0")
    _manifest(root, ["alpha"], versions={"alpha": "1.0.0"})
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    _manifest(root, ["alpha"], versions={"alpha": "1.1.0"})
    after = _scan(ctx, cbs, sid="s2")
    finding = after["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "version-mismatch"
    _assert_pending_findings_invariant(after)


def test_09_unparseable_manifest_version_cell_is_silent(tmp_path):
    """A non-semver Local ver cell (e.g. git hash) never files a version finding."""
    root = tmp_path / "skills"
    _skill(root, "alpha", version="1.0.0")
    _manifest(root, ["alpha"], versions={"alpha": "1.0.0"})
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    _manifest(root, ["alpha"], versions={"alpha": "dea6cff (git)"})
    after = _scan(ctx, cbs, sid="s2")
    assert "alpha" not in after["findings"]
    assert "alpha" not in after["pending"]
    _assert_pending_findings_invariant(after)


def test_10_wrong_digest_ack_refused_and_finding_stays(tmp_path):
    """Ack with the wrong digest refuses, writes nothing, finding stays."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nchanged!\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    live = changed["findings"]["alpha"]["digest"]
    ack_path = tmp_path / "acks.json"
    result = acks_mod.acknowledge(ack_path, root / "alpha", "alpha", "0" * 16)
    assert result["ok"] is False and result["code"] == "stale"
    assert live in result["message"]
    assert not ack_path.exists()
    still = _scan(ctx, cbs, sid="s3")
    assert still["findings"].get("alpha", {}).get("reason") == "changed"
    _assert_pending_findings_invariant(still)


def test_11_correct_prefix_ack_clears_and_next_scan_silent(tmp_path):
    """Ack with the correct 8-char prefix clears; the next scan stays silent."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nchanged!\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    live = changed["findings"]["alpha"]["digest"]
    ack_path = tmp_path / "acks.json"
    result = acks_mod.acknowledge(ack_path, root / "alpha", "alpha", live[:8])
    assert result["ok"] is True and result["code"] == "acked"
    cleared = _scan(ctx, cbs, sid="s3")
    assert "alpha" not in cleared["findings"]
    assert cleared["pending"] == []
    silent = _scan(ctx, cbs, sid="s4")
    assert silent["findings"] == {}
    _assert_pending_findings_invariant(silent)


def test_12_second_skill_ack_does_not_clear_first(tmp_path):
    """Acking beta leaves alpha's finding (and pending) intact."""
    root = tmp_path / "skills"
    for name in ("alpha", "beta"):
        _skill(root, name)
    _manifest(root, ["alpha", "beta"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    for name in ("alpha", "beta"):
        (root / name / "SKILL.md").write_text(
            f"---\n---\nchanged {name}\n", encoding="utf-8"
        )
    changed = _scan(ctx, cbs, sid="s2")
    assert set(changed["findings"]) == {"alpha", "beta"}
    beta_live = changed["findings"]["beta"]["digest"]
    ack_path = tmp_path / "acks.json"
    assert acks_mod.acknowledge(ack_path, root / "beta", "beta", beta_live[:8])["ok"]
    after = _scan(ctx, cbs, sid="s3")
    assert set(after["findings"]) == {"alpha"}
    assert after["pending"] == ["alpha"]
    _assert_pending_findings_invariant(after)


def test_13_unrelated_manifest_row_edit_keeps_finding(tmp_path):
    """Adding a row elsewhere (gamma) neither clears alpha nor files gamma."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _skill(root, "gamma")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nchanged!\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    _manifest(root, ["alpha", "gamma"])
    after = _scan(ctx, cbs, sid="s3")
    assert after["findings"].get("alpha", {}).get("reason") == "changed"
    assert "gamma" not in after["findings"]
    assert after["pending"] == ["alpha"]
    _assert_pending_findings_invariant(after)


def test_14_dismissed_skill_with_changed_content_is_silent(tmp_path):
    """A config-dismissed skill never produces findings, even when changed."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    ctx.config["dismissed"] = ["alpha"]
    (root / "alpha" / "SKILL.md").write_text("---\n---\nchanged!\n", encoding="utf-8")
    after = _scan(ctx, cbs, sid="s2")
    assert after["findings"] == {}
    assert after["pending"] == []
    _assert_pending_findings_invariant(after)


def test_15_quick_signature_short_circuits_content_hash(tmp_path, monkeypatch):
    """An unchanged tree performs zero content hashes on the second pass."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    state = _fresh_state()
    _reconcile(state, root, ["alpha"], now=1000.0)
    assert state["change_baseline_done"] is True
    calls = {"n": 0}
    real = hooks.fingerprint.content_digest

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(hooks.fingerprint, "content_digest", counting)
    _reconcile(state, root, ["alpha"], now=2000.0)
    assert calls["n"] == 0
    assert state["findings"] == {}


def test_16_scan_budget_zero_defers_and_preserves_findings(tmp_path):
    """Budget 0: complete=False, deferred>0, pre-existing findings preserved.

    Removing alpha from the manifest no longer stalls (H2): the trusted pass
    drops its `changed` finding (H10) while the fingerprint survives, so a
    re-added row resumes against the old ack.
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    state = _fresh_state()
    _reconcile(state, root, ["alpha"], now=1000.0)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nchanged!\n", encoding="utf-8")
    _reconcile(state, root, ["alpha"], now=2000.0)
    assert state["findings"].get("alpha", {}).get("reason") == "changed"
    ok_ts = state["scan"]["last_ok_ts"]
    _reconcile(state, root, ["alpha"], now=3000.0, budget=0.0)
    scan = state["scan"]
    assert scan["complete"] is False
    assert scan["deferred"] > 0
    assert scan["last_ok_ts"] == ok_ts
    assert state["findings"].get("alpha", {}).get("reason") == "changed"
    live_ack = state["fingerprints"]["alpha"]["ack"]
    _reconcile(state, root, [], now=4000.0, budget=0.0)
    scan = state["scan"]
    assert scan["complete"] is True
    assert scan["error"] == ""
    assert scan["last_ok_ts"] == 4000.0
    assert "alpha" not in state["findings"]
    assert state["fingerprints"]["alpha"]["ack"] == live_ack


def test_17_truncated_fingerprint_surfaces_in_advisory(tmp_path):
    """A 1-file cap marks truncated=True and the advisory line says so."""
    root = tmp_path / "skills"
    skill = _skill(root, "alpha")
    (skill / "second.md").write_text("more\n", encoding="utf-8")
    state = _fresh_state()
    _reconcile(state, root, ["alpha"], now=1000.0, max_files=1)
    assert state["fingerprints"]["alpha"]["truncated"] is True
    # "AAA.md" sorts before "SKILL.md", so it shifts the 1-file window and
    # moves the (truncated) digest.
    (skill / "AAA.md").write_text("window shift\n", encoding="utf-8")
    _reconcile(state, root, ["alpha"], now=2000.0, max_files=1)
    finding = state["findings"].get("alpha")
    assert finding is not None and finding["truncated"] is True
    text = hooks._advisory(["alpha"], "UPSTREAM_MANIFEST.md", 6, {}, state["findings"])
    assert "(truncated fingerprint)" in text
    assert finding["digest"][:8] in text
    assert text.splitlines()[-1] == _LINE


def test_18_advisory_bounds_and_untracked_sentence():
    """Change advisory: <=899 chars, advisory-only last, sha present,
    untracked sentence verbatim."""
    digest = "ab12cd34" * 8
    findings = {
        "alpha": {
            "name": "alpha",
            "reason": "changed",
            "digest": digest,
            "files": 2,
            "truncated": False,
            "first_seen_ts": 1.0,
        }
    }
    text = hooks._advisory(
        ["alpha", "beta"], "UPSTREAM_MANIFEST.md", 6, {}, findings
    )
    assert len(text) <= 899
    assert text.splitlines()[-1] == _LINE
    assert "ab12cd34" in text
    assert "1 skill changed since their last acknowledged review:" in text
    assert (
        "1 skill missing from UPSTREAM_MANIFEST.md: beta. "
        "Add a manifest row to UPSTREAM_MANIFEST.md, or add "
        "the skill to the skill-manifest plugin's dismissed config list."
    ) in text


def test_19_session_dedupe_key_tracks_digest():
    """Same findings twice -> None; a new digest injects again."""
    digest_one = "ab12cd34" * 8
    digest_two = "99aa0011" * 8
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "pending": ["alpha"],
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": True,
            "findings": {
                "alpha": {
                    "name": "alpha",
                    "reason": "changed",
                    "digest": digest_one,
                    "files": 1,
                    "truncated": False,
                    "first_seen_ts": 1.0,
                }
            },
        },
    )
    hooks.configure(ctx)
    hooks._probed = True
    first = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert first is not None and "ab12cd34" in first["context"]
    assert hooks.on_pre_llm_call(
        session_id="s", user_message="hi", is_first_turn=False
    ) is None
    state = ctx.state.get("v1")
    state["findings"]["alpha"]["digest"] = digest_two
    ctx.state.set("v1", state)
    third = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert third is not None and "99aa0011" in third["context"]
    assert hooks.on_pre_llm_call(
        session_id="s", user_message="hi", is_first_turn=False
    ) is None


def test_20_status_output_fields(capsys):
    """Status shows last-ok ts, complete flag, error field, reason+sha8 rows."""
    digest = "ab12cd34" * 8
    state = _fresh_state(
        pending=["alpha", "beta"],
        dismissed=["old"],
        findings={
            "alpha": {
                "name": "alpha",
                "reason": "changed",
                "digest": digest,
                "files": 1,
                "truncated": False,
                "first_seen_ts": 1.0,
            },
            "beta": {
                "name": "beta",
                "reason": "untracked",
                "digest": "",
                "files": 0,
                "truncated": False,
                "first_seen_ts": 1.0,
            },
        },
        scan={
            "last_attempt_ts": 2000.0,
            "last_ok_ts": 1999.5,
            "complete": True,
            "error": "",
            "checked": 2,
            "deferred": 0,
        },
    )
    text = hooks.render_status(state, manifest_path="/r/UPSTREAM_MANIFEST.md", missing=1)
    assert "1999.5" in text
    assert "complete" in text
    assert "error" in text
    assert "changed" in text and "ab12cd34" in text
    assert "untracked" in text
    ctx = FakeCtx()
    ctx.state.set("v1", state)
    hooks.configure(ctx)
    import argparse

    args = argparse.Namespace(skill_manifest_cmd="status")
    assert hooks.cli_handler(args) == 0
    out = capsys.readouterr().out
    assert "skill-manifest status" in out


def test_21_concurrent_ack_of_two_skills(tmp_path, monkeypatch):
    """Two concurrent acks persist both entries (locked merge, no drop).

    The read inside the lock sleeps ~50ms so a missing lock would reliably
    lose an entry; the flock serializes the merge instead.
    """
    import fingerprint as fp

    root = tmp_path / "skills"
    for name in ("alpha", "beta"):
        _skill(root, name)
    ack_path = tmp_path / "acks.json"
    live = {
        name: fp.content_digest(root / name)["digest"] for name in ("alpha", "beta")
    }
    real_load = acks_mod.load_acks

    def slow_load(path):
        import time as _time

        _time.sleep(0.05)
        return real_load(path)

    monkeypatch.setattr(acks_mod, "load_acks", slow_load)
    barrier = threading.Barrier(2)
    results = {}

    def _ack(name):
        barrier.wait()
        results[name] = acks_mod.acknowledge(
            ack_path, root / name, name, live[name][:10]
        )

    threads = [threading.Thread(target=_ack, args=(name,)) for name in ("alpha", "beta")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results["alpha"]["ok"] is True
    assert results["beta"]["ok"] is True
    doc = acks_mod.load_acks(ack_path)
    assert set(doc["acks"]) == {"alpha", "beta"}
    assert doc["acks"]["alpha"]["digest"] == live["alpha"]
    assert doc["acks"]["beta"]["digest"] == live["beta"]


def test_22_loader_registers_cli_and_slash():
    """Package-exec register() wires hooks + CLI + slash without raising."""
    import types

    parent = sys.modules.get("hermes_plugins")
    name = "hermes_plugins.skill_manifest_ct"
    pkg = types.ModuleType("hermes_plugins")
    pkg.__path__ = []
    pkg.__package__ = "hermes_plugins"
    sys.modules["hermes_plugins"] = pkg
    try:
        spec = importlib.util.spec_from_file_location(
            name, PLUGIN / "__init__.py", submodule_search_locations=[str(PLUGIN)]
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__path__ = [str(PLUGIN)]
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        ctx = FakeCtx()
        mod.register(ctx)
        assert {event for event, _cb in ctx.hooks} == {
            "on_skill_lifecycle",
            "on_session_start",
            "pre_llm_call",
        }
        assert [entry[0] for entry in ctx.cli] == ["skill-manifest"]
        assert [entry[0] for entry in ctx.commands] == ["skill-manifest-ack"]
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)
        if parent is None:
            sys.modules.pop("hermes_plugins", None)
        else:
            sys.modules["hermes_plugins"] = parent


def test_23_state_size_bound(tmp_path):
    """300 manifested skills x 12 files serializes under 200 KB."""
    root = tmp_path / "skills"
    names = [f"skill-{i:03d}" for i in range(300)]
    for name in names:
        d = root / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\n---\nbody\n", encoding="utf-8")
        for i in range(11):
            (d / f"file-{i}.md").write_text(f"content {i}\n", encoding="utf-8")
    _manifest(root, names)
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    state = _scan(ctx, cbs)
    assert state["findings"] == {}
    size = len(json.dumps(state))
    assert size < 200_000, size


def test_24_probe_list_skill_names_shape_preserved(tmp_path):
    """list_skill_dirs/list_skill_names keep original-case leaves + first-wins."""
    import fingerprint as fp
    import probe as probe_mod

    root = tmp_path / "skills"
    (root / "email" / "himalaya").mkdir(parents=True)
    (root / "email" / "himalaya" / "SKILL.md").write_text("---\n", encoding="utf-8")
    (root / "solo-skill").mkdir()
    (root / "solo-skill" / "SKILL.md").write_text("---\n", encoding="utf-8")
    (root / ".archive" / "ghost").mkdir(parents=True)
    (root / ".archive" / "ghost" / "SKILL.md").write_text("---\n", encoding="utf-8")
    for rel in ("a/Widget", "b/widget"):
        d = root / rel
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\n", encoding="utf-8")
    assert probe_mod.list_skill_names(root) == {"himalaya", "solo-skill", "Widget"}
    assert probe_mod.list_skill_names(root / "definitely-missing") == set()
    dirs = fp.list_skill_dirs(root)
    assert set(dirs) == {"himalaya", "solo-skill", "widget"}
    assert dirs["widget"].name == "Widget"
    assert fp.declared_version(root / "solo-skill" / "SKILL.md") == ""
    versioned = root / "solo-skill" / "SKILL.md"
    versioned.write_text("---\nname: x\nversion: 2.0.1\n---\nbody\n", encoding="utf-8")
    assert fp.declared_version(versioned) == "2.0.1"


def test_25_missing_manifest_preserves_findings_and_fingerprints(tmp_path):
    """F1: manifest removed mid-life keeps the changed finding and prints.

    `complete=False`, `error="manifest-unavailable"`, `last_ok_ts` frozen.
    When the manifest returns, the pre-existing ack resumes comparison and
    the earlier edit is still reported.
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nchanged!\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    ok_ts = changed["scan"]["last_ok_ts"]
    ack_before = changed["fingerprints"]["alpha"]["ack"]
    (root / "UPSTREAM_MANIFEST.md").rename(root / "UPSTREAM_MANIFEST.md.hidden")
    after = _scan(ctx, cbs, sid="s3")
    assert after["scan"]["complete"] is False
    assert after["scan"]["error"] == "manifest-unavailable"
    assert after["scan"]["last_ok_ts"] == ok_ts
    assert after["findings"].get("alpha", {}).get("reason") == "changed"
    assert after["fingerprints"]["alpha"]["ack"] == ack_before
    assert after["pending"] == ["alpha"]
    _assert_pending_findings_invariant(after)
    (root / "UPSTREAM_MANIFEST.md.hidden").rename(root / "UPSTREAM_MANIFEST.md")
    back = _scan(ctx, cbs, sid="s4")
    assert back["scan"]["complete"] is True
    assert back["findings"].get("alpha", {}).get("reason") == "changed"
    assert back["fingerprints"]["alpha"]["ack"] == ack_before
    _assert_pending_findings_invariant(back)


def test_26_missing_root_records_unavailable_without_state_loss(tmp_path):
    """F1: a skills root that is not a directory keeps state and reports.

    `complete=False`, `error="root-unavailable"`, `last_attempt_ts` set,
    findings and fingerprints untouched.
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nchanged!\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    ok_ts = changed["scan"]["last_ok_ts"]
    attempt_ts = changed["scan"]["last_attempt_ts"]
    ctx.config["skills_root"] = str(tmp_path / "no-such-dir")
    after = _scan(ctx, cbs, sid="s3")
    assert after["scan"]["complete"] is False
    assert after["scan"]["error"] == "root-unavailable"
    assert after["scan"]["last_attempt_ts"] >= attempt_ts
    assert after["scan"]["last_ok_ts"] == ok_ts
    assert after["findings"].get("alpha", {}).get("reason") == "changed"
    assert "alpha" in after["fingerprints"]
    assert after["pending"] == ["alpha"]
    _assert_pending_findings_invariant(after)


def test_27_first_scan_without_manifest_defers_baseline(tmp_path):
    """F1: with no manifest file, adoption waits; it runs when it appears."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    first = _scan(ctx, cbs)
    assert first["change_baseline_done"] is False
    assert first["fingerprints"] == {}
    assert first["scan"]["error"] == "manifest-unavailable"
    _manifest(root, ["alpha"])
    second = _scan(ctx, cbs, sid="s2")
    assert second["change_baseline_done"] is True
    assert second["findings"] == {}
    assert set(second["fingerprints"]) == {"alpha"}
    (root / "alpha" / "SKILL.md").write_text("---\n---\nchanged!\n", encoding="utf-8")
    third = _scan(ctx, cbs, sid="s3")
    assert third["findings"].get("alpha", {}).get("reason") == "changed"
    _assert_pending_findings_invariant(third)


def test_28_deferred_baseline_resumes_without_readopting(tmp_path, monkeypatch):
    """F3: a partial baseline adopts only missing keys; an edit in between reports."""
    import time as _time

    import fingerprint as fp

    root = tmp_path / "skills"
    for name in ("a", "b"):
        _skill(root, name)
    _manifest(root, ["a", "b"])
    real_monotonic = _time.monotonic
    script = iter([0.0, 0.0])

    def fake_monotonic():
        try:
            return next(script)
        except StopIteration:
            return real_monotonic()

    monkeypatch.setattr(hooks.time, "monotonic", fake_monotonic)
    state = _fresh_state()
    _reconcile(state, root, ["a", "b"], now=1000.0, budget=5.0)
    assert set(state["fingerprints"]) == {"a"}
    assert state["change_baseline_done"] is False
    assert state["scan"]["complete"] is False
    assert state["scan"]["deferred"] == 1
    assert state["scan"]["checked"] == 1
    ack_a = state["fingerprints"]["a"]["ack"]
    ts_a = state["fingerprints"]["a"]["ack_ts"]
    assert ack_a == state["fingerprints"]["a"]["digest"] and ack_a
    (root / "a" / "SKILL.md").write_text("---\n---\nedited between scans\n", encoding="utf-8")
    _reconcile(state, root, ["a", "b"], now=2000.0, budget=5.0)
    # Resumed: b adopted, a examined in the same pass (H1: no global
    # baseline gate), so the between-scans edit is reported immediately.
    assert set(state["fingerprints"]) == {"a", "b"}
    assert state["fingerprints"]["a"]["ack"] == ack_a
    assert state["fingerprints"]["a"]["ack_ts"] == ts_a
    assert state["change_baseline_done"] is True
    resumed = state["findings"].get("a")
    assert resumed is not None and resumed["reason"] == "changed"
    assert resumed["digest"] != ack_a
    assert "b" not in state["findings"]
    # The finding persists on the next pass with the live digest.
    _reconcile(state, root, ["a", "b"], now=3000.0)
    finding = state["findings"].get("a")
    assert finding is not None and finding["reason"] == "changed"
    assert finding["digest"] != ack_a
    assert "b" not in state["findings"]
    assert fp.content_digest(root / "a")["digest"] == finding["digest"]


def test_29_ack_with_configured_caps_matches_advisory_digest(tmp_path):
    """F5: the CLI ack fingerprints with the hook's configured caps."""
    root = tmp_path / "skills"
    skill = _skill(root, "alpha")
    (skill / "second.md").write_text("more\n", encoding="utf-8")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    ctx.config["max_files_per_skill"] = 1
    _wire(ctx)
    hooks.configure(ctx)
    cbs_state = ctx.state.get("v1")
    assert cbs_state is None
    hooks.on_session_start(session_id="s")
    state = ctx.state.get("v1")
    hook_digest = state["fingerprints"]["alpha"]["digest"]
    assert state["fingerprints"]["alpha"]["truncated"] is True
    text, rc = hooks._do_ack("alpha", hook_digest[:8])
    assert rc == 0, text
    assert text == f"acked alpha {hook_digest[:8]}"
    hooks.on_session_start(session_id="s2")
    state2 = ctx.state.get("v1")
    assert "alpha" not in state2["findings"]


def test_30_corrupt_acks_backed_up_and_entries_preserved(tmp_path):
    """F6: an unparseable acks.json is backed up; merges keep every entry."""
    import fingerprint as fp

    root = tmp_path / "skills"
    for name in ("alpha", "beta"):
        _skill(root, name)
    ack_path = tmp_path / "acks.json"
    ack_path.write_text("{not valid json", encoding="utf-8")
    live_alpha = fp.content_digest(root / "alpha")["digest"]
    result = acks_mod.acknowledge(ack_path, root / "alpha", "alpha", live_alpha[:8])
    assert result["ok"] is True and result["code"] == "acked"
    assert result.get("recovered") is True
    backups = list(tmp_path.glob("acks.json.corrupt.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"{not valid json"
    # Prior parseable entries plus verbatim junk survive the next merge.
    doc = json.loads(ack_path.read_text(encoding="utf-8"))
    doc["acks"]["odd-shape"] = {"note": "no digest here"}
    doc["acks"]["raw-number"] = 7
    doc["acks"]["gamma"] = {"digest": "ab12cd34" * 8, "custom": [1, 2]}
    ack_path.write_text(json.dumps(doc, sort_keys=True), encoding="utf-8")
    live_beta = fp.content_digest(root / "beta")["digest"]
    again = acks_mod.acknowledge(ack_path, root / "beta", "beta", live_beta[:8])
    assert again["ok"] is True and again.get("recovered") is not True
    merged = acks_mod.load_acks(ack_path)["acks"]
    assert merged["alpha"]["digest"] == live_alpha
    assert merged["beta"]["digest"] == live_beta
    assert merged["odd-shape"] == {"note": "no digest here"}
    assert merged["raw-number"] == 7
    assert merged["gamma"] == {"digest": "ab12cd34" * 8, "custom": [1, 2]}


def test_31_contended_acks_lock_refuses_without_writing(tmp_path, monkeypatch):
    """F6: a lock that cannot be acquired after retry refuses with `locked`."""
    import fcntl

    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    ack_path = tmp_path / "acks.json"
    live = fp.content_digest(root / "alpha")["digest"]
    monkeypatch.setattr(acks_mod, "_ACK_LOCK_TIMEOUT_S", 0.3)
    lock_path = tmp_path / "acks.json.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = acks_mod.acknowledge(ack_path, root / "alpha", "alpha", live[:8])
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    assert result["ok"] is False and result["code"] == "locked"
    assert not ack_path.exists()


def test_32_oversized_file_uses_stable_big_marker(tmp_path):
    """F7/H7: a file past max_file_bytes is marker-hashed, never read.

    The marker consumes a max_files slot but not max_bytes (H7), and the
    digest is truncated.
    """
    import fingerprint as fp

    d = tmp_path / "skills" / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\n---\nbase\n", encoding="utf-8")
    big = d / "weights.bin"
    big.write_bytes(b"x" * 100)
    first = fp.content_digest(d, max_file_bytes=10)
    second = fp.content_digest(d, max_file_bytes=10)
    assert first["digest"] == second["digest"] and first["digest"]
    assert first["truncated"] is True
    assert first["files"] == 2
    # Both files exceed max_file_bytes=10, so both are markers: no hashed
    # bytes at all, and the 100-byte weight never lands in `bytes`.
    assert first["bytes"] == 0
    # Same size, different bytes: the marker covers size, not content.
    big.write_bytes(b"y" * 100)
    assert fp.content_digest(d, max_file_bytes=10)["digest"] == first["digest"]
    # Fully hashed, the digest differs and is complete.
    full = fp.content_digest(d, max_file_bytes=10_000_000)
    assert full["truncated"] is False
    assert full["digest"] != first["digest"]


def test_33_dismissed_finding_dropped_on_incomplete_scan(tmp_path):
    """F10: dismissing a skill drops its finding even when the scan defers."""
    root = tmp_path / "skills"
    for name in ("alpha", "beta"):
        _skill(root, name)
    state = _fresh_state()
    _reconcile(state, root, ["alpha", "beta"], now=1000.0)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nchanged!\n", encoding="utf-8")
    _reconcile(state, root, ["alpha", "beta"], now=2000.0)
    assert state["findings"].get("alpha", {}).get("reason") == "changed"
    state["dismissed"] = ["alpha"]
    _reconcile(state, root, ["alpha", "beta"], now=3000.0, budget=0.0)
    assert state["scan"]["complete"] is False
    assert "alpha" not in state["findings"]
    assert "alpha" not in state["fingerprints"]


def test_34_mixed_case_manifest_name_version_mismatch(tmp_path):
    """F13: a mixed-case manifest row (`Cat/Alpha`) still version-checks."""
    root = tmp_path / "skills"
    _skill(root, "Alpha", version="1.0.0")
    _manifest(root, ["Cat/Alpha"], versions={"Cat/Alpha": "2.0.0"})
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    first = _scan(ctx, cbs)
    assert first["change_baseline_done"] is True
    assert first["findings"] == {}
    second = _scan(ctx, cbs, sid="s2")
    finding = second["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "version-mismatch"
    assert second["pending"] == ["Alpha"]
    _assert_pending_findings_invariant(second)


def test_35_capped_quick_and_digest_agree_on_boundary_edit(tmp_path):
    """F4: one canonical file list — the cap-boundary edit moves both signals."""
    import fingerprint as fp

    d = tmp_path / "skills" / "alpha"
    (d / "a").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\n---\nbase\n", encoding="utf-8")
    (d / "a" / "z.md").write_text("z\n", encoding="utf-8")
    (d / "a-b").write_text("ab\n", encoding="utf-8")
    quick_before = fp.quick_signature(d, max_files=2)
    digest_before = fp.content_digest(d, max_files=2)
    # "a-b" sorts second (SKILL.md, a-b) but walks third (SKILL.md, a/z.md):
    # the old walk-order quick missed it while the digest caught it.
    (d / "a-b").write_text("ab EDITED\n", encoding="utf-8")
    assert fp.quick_signature(d, max_files=2) != quick_before
    assert fp.content_digest(d, max_files=2)["digest"] != digest_before["digest"]
    # Past the cap, an edit moves neither.
    quick_capped = fp.quick_signature(d, max_files=2)
    digest_capped = fp.content_digest(d, max_files=2)["digest"]
    (d / "a" / "z.md").write_text("z EDITED\n", encoding="utf-8")
    assert fp.quick_signature(d, max_files=2) == quick_capped
    assert fp.content_digest(d, max_files=2)["digest"] == digest_capped


def test_36_empty_skill_dir_reports_error_without_clearing(tmp_path, monkeypatch):
    """F8/F9: an unexaminable skill keeps its finding; others still scan."""
    import fingerprint as fp

    root = tmp_path / "skills"
    for name in ("alpha", "beta"):
        _skill(root, name)
    state = _fresh_state()
    _reconcile(state, root, ["alpha", "beta"], now=1000.0)
    for name in ("alpha", "beta"):
        (root / name / "SKILL.md").write_text(
            f"---\n---\nchanged {name}\n", encoding="utf-8"
        )
    _reconcile(state, root, ["alpha", "beta"], now=2000.0)
    assert set(state["findings"]) == {"alpha", "beta"}
    ok_ts = state["scan"]["last_ok_ts"]
    old_digest = state["fingerprints"]["alpha"]["digest"]
    assert fp.content_digest(tmp_path / "empty-dir")["digest"] == ""
    # Touch both trees so the quick short-circuit does not skip the digest.
    for name in ("alpha", "beta"):
        (root / name / "SKILL.md").write_text(
            f"---\n---\nchanged again {name}\n", encoding="utf-8"
        )
    real_digest = hooks.fingerprint.content_digest

    def flaky_digest(skill_dir, **kwargs):
        if Path(skill_dir).name == "alpha":
            return {"digest": "", "files": 0, "bytes": 0, "truncated": False, "skipped": 0}
        return real_digest(skill_dir, **kwargs)

    monkeypatch.setattr(hooks.fingerprint, "content_digest", flaky_digest)
    _reconcile(state, root, ["alpha", "beta"], now=3000.0)
    scan = state["scan"]
    assert scan["complete"] is False
    assert scan["error"] == "skill-read-error"
    assert scan["last_ok_ts"] == ok_ts
    # Alpha: no write, no finding change, no clearing.
    assert state["fingerprints"]["alpha"]["digest"] == old_digest
    assert state["findings"].get("alpha", {}).get("reason") == "changed"
    # Beta was still examined on the same pass.
    assert state["findings"].get("beta", {}).get("reason") == "changed"


def test_38_invalid_sha_shows_unknown_and_omits_ack_line():
    """F11: a non-hex/empty digest renders `sha ????` with no ack instruction."""
    for bad in ("", "not-hex!", "zzzzzzzz"):
        findings = {
            "alpha": {
                "name": "alpha",
                "reason": "changed",
                "digest": bad,
                "files": 1,
                "truncated": False,
                "first_seen_ts": 1.0,
            }
        }
        text = hooks._advisory(["alpha"], "UPSTREAM_MANIFEST.md", 6, {}, findings)
        assert "sha ????" in text, bad
        assert "--expect" not in text, bad
        assert text.splitlines()[-1] == _LINE
    good = {
        "alpha": {
            "name": "alpha",
            "reason": "changed",
            "digest": "ab12cd34" * 8,
            "files": 1,
            "truncated": False,
            "first_seen_ts": 1.0,
        }
    }
    text = hooks._advisory(["alpha"], "UPSTREAM_MANIFEST.md", 6, {}, good)
    assert "sha ab12cd34" in text
    assert "--expect" in text


def test_39_lint_summary_line_shown_with_only_change_findings():
    """F12: the lint sentence appears even when no untracked block exists."""
    findings = {
        "alpha": {
            "name": "alpha",
            "reason": "changed",
            "digest": "ab12cd34" * 8,
            "files": 1,
            "truncated": False,
            "first_seen_ts": 1.0,
        }
    }
    lint = {"errors": 2, "findings": 5, "counts": {}}
    text = hooks._advisory(["alpha"], "UPSTREAM_MANIFEST.md", 6, lint, findings)
    assert "Manifest has 5 lint findings (2 errors)" in text
    assert "run the manifest lint" in text
    assert text.splitlines()[-1] == _LINE
    quiet = hooks._advisory(["alpha"], "UPSTREAM_MANIFEST.md", 6, {}, findings)
    assert "run the manifest lint" not in quiet


def test_40_session_key_is_short_hash_with_legacy_new_once():
    """F14/I7: 16-hex dedupe key; digest moves re-inject; a pure shrink stays silent."""
    digest_one = "ab12cd34" * 8
    digest_two = "99aa0011" * 8

    def _findings(digest):
        return {
            "alpha": {
                "name": "alpha",
                "reason": "changed",
                "digest": digest,
                "files": 1,
                "truncated": False,
                "first_seen_ts": 1.0,
            }
        }

    key_one = hooks._finding_key(["alpha"], _findings(digest_one))
    assert isinstance(key_one, str) and len(key_one) == 16
    int(key_one, 16)
    assert hooks._finding_key(["alpha"], _findings(digest_one)) == key_one
    assert hooks._finding_key(["alpha"], _findings(digest_two)) != key_one
    assert hooks._coerce_key([["alpha", "changed", "ab12cd34"]]) is None
    assert hooks._coerce_key(key_one) == key_one
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "pending": ["alpha"],
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": True,
            "findings": _findings(digest_one),
        },
    )
    hooks.configure(ctx)
    hooks._probed = True
    first = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert first is not None and "ab12cd34" in first["context"]
    stored = ctx.state.get("v1")["advisory_sessions"]["s"]
    assert stored["key"] == key_one
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    state = ctx.state.get("v1")
    state["findings"] = _findings(digest_two)
    ctx.state.set("v1", state)
    third = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert third is not None and "99aa0011" in third["context"]
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    # A pure shrink stays silent (I7): the surviving row did not move.
    both = _findings(digest_two)
    both["beta"] = {
        "name": "beta",
        "reason": "changed",
        "digest": digest_two,
        "files": 1,
        "truncated": False,
        "first_seen_ts": 1.0,
    }
    grown = ctx.state.get("v1")
    grown["pending"] = ["alpha", "beta"]
    grown["findings"] = both
    ctx.state.set("v1", grown)
    grown_out = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert grown_out is not None and "beta" in grown_out["context"]
    shrunk = ctx.state.get("v1")
    shrunk["pending"] = ["alpha"]
    shrunk["findings"] = _findings(digest_two)
    ctx.state.set("v1", shrunk)
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    # A legacy list-shaped key is new exactly once.
    legacy = ctx.state.get("v1")
    legacy["advisory_sessions"]["s"]["key"] = [["alpha", "changed", "99aa0011"]]
    ctx.state.set("v1", legacy)
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is not None
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None


def test_42_broken_symlink_skill_does_not_block_others(tmp_path):
    """H1: one unexaminable skill never blocks change detection for the rest."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    beta = _skill(root, "beta")
    (beta / "SKILL.md").unlink()
    (beta / "SKILL.md").symlink_to(root / "nowhere.md")
    _manifest(root, ["alpha", "beta"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    first = _scan(ctx, cbs)
    assert set(first["fingerprints"]) == {"alpha"}
    assert first["fingerprints"]["alpha"]["ack"] == first["fingerprints"]["alpha"]["digest"]
    assert first["change_baseline_done"] is False
    assert first["scan"]["complete"] is False
    assert first["scan"]["error"] == "skill-read-error"
    assert first["scan"]["skill_errors"] == {"beta": "skill-read-error"}
    assert first["scan"]["checked"] == 2
    assert first["scan"]["deferred"] == 0
    assert first["findings"] == {}
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    second = _scan(ctx, cbs, sid="s2")
    finding = second["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "changed"
    assert second["scan"]["skill_errors"] == {"beta": "skill-read-error"}
    _assert_pending_findings_invariant(second)


def test_43_repaired_skill_resumes_and_reports_pending_edit(tmp_path):
    """H1: repairing the broken skill adopts it silently in the same pass that still reports alpha's edit."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    beta = _skill(root, "beta")
    (beta / "SKILL.md").unlink()
    (beta / "SKILL.md").symlink_to(root / "nowhere.md")
    _manifest(root, ["alpha", "beta"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    first = _scan(ctx, cbs)
    assert first["change_baseline_done"] is False
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    (beta / "SKILL.md").unlink()
    _skill(root, "beta", body="beta v1")
    second = _scan(ctx, cbs, sid="s2")
    assert second["change_baseline_done"] is True
    assert set(second["fingerprints"]) == {"alpha", "beta"}
    assert "beta" not in second["findings"]
    assert second["scan"]["skill_errors"] == {}
    assert second["scan"]["complete"] is True
    finding = second["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "changed"
    _assert_pending_findings_invariant(second)


def test_44_single_malformed_row_still_tracks_healthy_skills(tmp_path):
    """H2: one bad manifest row is a lint error, not a trust gate: healthy rows track."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _skill(root, "beta")
    _manifest(root, ["alpha", "beta"], extra="|  | 9.9.9 |\n")
    doc = manifest_db.load_manifest(root / "UPSTREAM_MANIFEST.md")
    assert doc.errors
    assert set(doc.skills) == {"alpha", "beta"}
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    first = _scan(ctx, cbs)
    assert set(first["fingerprints"]) == {"alpha", "beta"}
    assert first["findings"] == {}
    assert first["scan"]["error"] == ""
    assert first["scan"]["complete"] is True
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    second = _scan(ctx, cbs, sid="s2")
    assert second["findings"].get("alpha", {}).get("reason") == "changed"
    assert "beta" not in second["findings"]
    _assert_pending_findings_invariant(second)


def test_45_rows_removed_clears_to_untracked_without_stall(tmp_path):
    """H2/H10: removing every manifest row is a trusted complete scan.

    The stale change finding clears, the fingerprint survives, and the name
    reconcile raises the skill as untracked.
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    ack_before = changed["fingerprints"]["alpha"]["ack"]
    _manifest(root, [])
    cleared = _scan(ctx, cbs, sid="s3")
    assert cleared["scan"]["complete"] is True
    assert cleared["scan"]["error"] == ""
    finding = cleared["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "untracked"
    assert cleared["fingerprints"]["alpha"]["ack"] == ack_before
    assert cleared["pending"] == ["alpha"]
    assert cleared["baseline"]["alpha"] == "untracked-snapshot"
    _assert_pending_findings_invariant(cleared)


def test_46_walk_error_reports_inventory_partial_and_keeps_state(tmp_path, monkeypatch):
    """H2/I3: an enumeration failure gates `inventory-partial`, but the
    listed skills are still adopted and examined; cleanups are skipped."""
    import fingerprint as fp

    root = tmp_path / "skills"
    for name in ("alpha", "beta"):
        _skill(root, name)
    _manifest(root, ["alpha", "beta"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    digest_before = changed["fingerprints"]["alpha"]["digest"]
    ok_ts = changed["scan"]["last_ok_ts"]
    real_walk = fp.os.walk

    def flaky_walk(top, **kwargs):
        onerror = kwargs.get("onerror")
        for dirpath, dirnames, filenames in real_walk(top, **kwargs):
            if Path(dirpath) == root and "beta" in dirnames and onerror is not None:
                onerror(OSError("denied"))
                dirnames.remove("beta")
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(fp.os, "walk", flaky_walk)
    listed = fp.list_skill_dirs(root)
    assert listed.errors
    assert "beta" not in listed
    _skill(root, "gamma")
    _manifest(root, ["alpha", "beta", "gamma"])
    partial = _scan(ctx, cbs, sid="s3")
    assert partial["scan"]["error"] == "inventory-partial"
    assert partial["scan"]["complete"] is False
    assert partial["scan"]["last_ok_ts"] == ok_ts
    assert partial["findings"].get("alpha", {}).get("reason") == "changed"
    assert partial["fingerprints"]["alpha"]["digest"] == digest_before
    # I3: the listed skills are still adopted/examined on a partial pass.
    # Gamma is a post-baseline adoption, so its ack stays empty.
    assert "gamma" in partial["fingerprints"]
    assert partial["fingerprints"]["gamma"]["ack"] == ""
    assert partial["fingerprints"]["gamma"]["digest"]
    assert "gamma" not in partial["findings"]
    monkeypatch.undo()
    healed = _scan(ctx, cbs, sid="s4")
    assert healed["scan"]["error"] == ""
    assert healed["findings"].get("alpha", {}).get("reason") == "changed"
    assert "gamma" in healed["fingerprints"]
    _assert_pending_findings_invariant(healed)


def test_47_ack_without_lock_refuses_unavailable_and_writes_nothing(tmp_path, monkeypatch):
    """H3: a missing/broken flock refuses with `unavailable`, writing nothing."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    ack_path = tmp_path / "acks.json"
    live = fp.content_digest(root / "alpha")["digest"]
    monkeypatch.setattr(acks_mod, "_acquire_with_retry", lambda *a, **k: "unavailable")
    result = acks_mod.acknowledge(ack_path, root / "alpha", "alpha", live[:8])
    assert result["ok"] is False and result["code"] == "unavailable"
    assert result["message"] == "refused: ack locking unavailable"
    assert not ack_path.exists()


def test_48_ack_read_error_keeps_existing_file(tmp_path, monkeypatch):
    """H4: an unreadable acks file refuses with `read-error`, replacing nothing."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    ack_path = tmp_path / "acks.json"
    before = '{"version": 1, "acks": {"old": {"digest": "%s", "ts": 1.0}}}' % ("ab" * 32)
    ack_path.write_text(before, encoding="utf-8")
    live = fp.content_digest(root / "alpha")["digest"]
    real_read_bytes = Path.read_bytes

    def denied(self):
        if Path(self) == ack_path:
            raise PermissionError("denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", denied)
    result = acks_mod.acknowledge(ack_path, root / "alpha", "alpha", live[:8])
    assert result["ok"] is False and result["code"] == "read-error"
    assert ack_path.read_text(encoding="utf-8") == before


def test_49_ack_backup_failure_refuses_without_writing(tmp_path, monkeypatch):
    """H4: a corrupt acks file whose backup cannot be written refuses with `backup-failed`."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    ack_path = tmp_path / "acks.json"
    corrupt = "{not valid json"
    ack_path.write_text(corrupt, encoding="utf-8")
    live = fp.content_digest(root / "alpha")["digest"]
    real_write_bytes = Path.write_bytes

    def no_backup(self, *args, **kwargs):
        if ".corrupt." in str(self):
            raise OSError("denied")
        return real_write_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", no_backup)
    result = acks_mod.acknowledge(ack_path, root / "alpha", "alpha", live[:8])
    assert result["ok"] is False and result["code"] == "backup-failed"
    assert ack_path.read_text(encoding="utf-8") == corrupt
    assert list(tmp_path.glob("acks.json.corrupt.*")) == []


def test_50_resolve_then_change_reinjects(tmp_path):
    """H5: one finding resolving while another digest moves re-injects (then silent)."""
    digest_old = "ab12cd34" * 8
    digest_new = "99aa0011" * 8

    def _changed(name, digest):
        return {
            "name": name,
            "reason": "changed",
            "digest": digest,
            "files": 1,
            "truncated": False,
            "first_seen_ts": 1.0,
        }

    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "pending": ["alpha", "beta"],
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": True,
            "findings": {
                "alpha": _changed("alpha", digest_old),
                "beta": _changed("beta", digest_old),
            },
        },
    )
    hooks.configure(ctx)
    hooks._probed = True
    first = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert first is not None and "beta" in first["context"]
    state = ctx.state.get("v1")
    state["pending"] = ["alpha"]
    state["findings"] = {"alpha": _changed("alpha", digest_new)}
    ctx.state.set("v1", state)
    second = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert second is not None and digest_new[:8] in second["context"]
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None


def test_51_unreadable_file_aborts_skill_with_read_error(tmp_path, monkeypatch):
    """H6: a file that cannot be hashed aborts the skill; other skills still scan."""
    fp = hooks.fingerprint
    root = tmp_path / "skills"
    d = root / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\n---\nbase\n", encoding="utf-8")
    (d / "extra.md").write_text("more\n", encoding="utf-8")
    real_hash = fp._bounded_file_hash

    def flaky_hash(path, cap):
        if Path(path).name == "extra.md":
            return None, False
        return real_hash(path, cap)

    monkeypatch.setattr(fp, "_bounded_file_hash", flaky_hash)
    dig = fp.content_digest(d)
    assert dig["digest"] == "" and dig["error"] == "read"
    _skill(root, "beta")
    state = _fresh_state()
    _reconcile(state, root, ["alpha", "beta"], now=1000.0)
    assert state["scan"]["error"] == "skill-read-error"
    assert state["scan"]["skill_errors"] == {"alpha": "skill-read-error"}
    assert set(state["fingerprints"]) == {"beta"}
    assert state["scan"]["complete"] is False


def test_52_big_marker_ignores_bytes_and_first_file_never_zero(tmp_path):
    """H7/I5: BIG sizes stay out of `bytes`; a first file over budget emits
    a BIG marker (one slot, truncated) instead of zero files."""
    import fingerprint as fp

    root = tmp_path / "skills"
    d = root / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\n---\nbase\n", encoding="utf-8")
    (d / "weights.bin").write_bytes(b"x" * 100)
    mixed = fp.content_digest(d, max_file_bytes=20, max_bytes=50)
    assert mixed["digest"] and mixed["truncated"] is True
    assert mixed["files"] == 2
    assert mixed["bytes"] == len("---\n---\nbase\n")
    over = fp.content_digest(d, max_bytes=5)
    assert over["digest"] and over["truncated"] is True
    assert over["files"] == 1
    assert over["bytes"] == 0
    assert over["error"] == ""
    (d / "SKILL.md").write_text("x" * 200, encoding="utf-8")
    (d / "weights.bin").unlink()
    _skill(root, "beta")
    state = _fresh_state()
    _reconcile(state, root, ["alpha", "beta"], now=1000.0, max_bytes=100)
    assert set(state["fingerprints"]) == {"alpha", "beta"}
    assert state["fingerprints"]["alpha"]["files"] == 1
    assert state["fingerprints"]["alpha"]["truncated"] is True
    assert state["scan"]["skill_errors"] == {}
    assert state["scan"]["complete"] is True


def test_53_dismissed_finding_dropped_on_trust_error_pass(tmp_path):
    """H8: a trust-error pass still drops dismissed findings (pure state edit)."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    ctx.config["dismissed"] = ["alpha"]
    (root / "UPSTREAM_MANIFEST.md").rename(root / "UPSTREAM_MANIFEST.md.hidden")
    try:
        untrusted = _scan(ctx, cbs, sid="s3")
    finally:
        (root / "UPSTREAM_MANIFEST.md.hidden").rename(root / "UPSTREAM_MANIFEST.md")
    assert untrusted["scan"]["error"] == "manifest-unavailable"
    assert untrusted["scan"]["complete"] is False
    assert "alpha" not in untrusted["findings"]
    assert "alpha" not in untrusted["fingerprints"]
    assert untrusted["pending"] == []


def test_54_unmanifested_change_finding_clears_but_fingerprint_survives(tmp_path):
    """H10: a trusted scan drops change findings for unmanifested keys, keeps fingerprints, resumes on re-add."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    state = _fresh_state()
    _reconcile(state, root, ["alpha"], now=1000.0)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    _reconcile(state, root, ["alpha"], now=2000.0)
    assert state["findings"].get("alpha", {}).get("reason") == "changed"
    ack_before = state["fingerprints"]["alpha"]["ack"]
    _reconcile(state, root, [], now=3000.0)
    assert state["scan"]["complete"] is True
    assert state["scan"]["error"] == ""
    assert "alpha" not in state["findings"]
    assert state["fingerprints"]["alpha"]["ack"] == ack_before
    _reconcile(state, root, ["alpha"], now=4000.0)
    resumed = state["findings"].get("alpha")
    assert resumed is not None and resumed["reason"] == "changed"
    assert resumed["digest"] != ack_before


def test_55_effective_ack_accepts_only_64_hex_from_external_map():
    """H9: a bare string (or short digest) from the external map is ignored."""
    full = "ab12cd34" * 8
    entry = {"ack": "stored-ack"}
    assert hooks._effective_ack({"alpha": {"digest": full}}, "alpha", entry) == full
    assert hooks._effective_ack({"alpha": full}, "alpha", entry) == "stored-ack"
    assert hooks._effective_ack({"alpha": {"digest": "abc123"}}, "alpha", entry) == "stored-ack"
    assert hooks._effective_ack({"alpha": {"digest": ""}}, "alpha", entry) == "stored-ack"
    assert hooks._effective_ack({}, "alpha", entry) == "stored-ack"
    assert hooks._effective_ack({}, "alpha", {}) == ""


def test_56_post_baseline_adoption_leaves_ack_empty_then_reports_edit(tmp_path):
    """I1: a skill adopted after change_baseline_done gets ack="" + first_seen_ts.

    The pass right after adoption reports nothing (stored digest equals the
    live one); the next real edit reports `changed`.
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    first = _scan(ctx, cbs)
    assert first["change_baseline_done"] is True
    assert first["fingerprints"]["alpha"]["ack"] == first["fingerprints"]["alpha"]["digest"]
    _skill(root, "beta")
    _manifest(root, ["alpha", "beta"])
    adopted = _scan(ctx, cbs, sid="s2")
    beta = adopted["fingerprints"]["beta"]
    assert beta["digest"]
    assert beta["ack"] == ""
    assert beta["ack_ts"] == 0.0
    assert beta["first_seen_ts"] > 0
    assert "beta" not in adopted["findings"]
    assert "beta" not in adopted["pending"]
    adopted_digest = beta["digest"]
    (root / "beta" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s3")
    finding = changed["findings"].get("beta")
    assert finding is not None and finding["reason"] == "changed"
    assert finding["digest"] != adopted_digest
    assert "beta" in changed["pending"]
    _assert_pending_findings_invariant(changed)


def test_57_cleanup_runs_with_broken_sibling_skill(tmp_path):
    """I2: a deleted skill's finding+fingerprint drop even while a sibling errors."""
    root = tmp_path / "skills"
    for name in ("alpha", "beta", "gone"):
        _skill(root, name)
    _manifest(root, ["alpha", "beta", "gone"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "gone" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("gone", {}).get("reason") == "changed"
    import shutil

    shutil.rmtree(root / "gone")
    _manifest(root, ["alpha", "beta"])
    (root / "beta" / "SKILL.md").unlink()
    (root / "beta" / "SKILL.md").symlink_to(root / "nowhere.md")
    cleaned = _scan(ctx, cbs, sid="s3")
    assert cleaned["scan"]["skill_errors"] == {"beta": "skill-read-error"}
    assert cleaned["scan"]["complete"] is False
    assert "gone" not in cleaned["findings"]
    assert "gone" not in cleaned["fingerprints"]
    assert "alpha" in cleaned["fingerprints"]
    _assert_pending_findings_invariant(cleaned)


def test_58_partial_inventory_still_examines_listed_skills(tmp_path, monkeypatch):
    """I3: on a partial inventory the listed skills are examined in place.

    R11 Q1/Q2: a partial pass still reports — the fresh `changed` finding
    joins `pending` on the partial pass itself (via the untracked mirror
    plus the pending derivation, with no `reconcile()` call), and the
    advisory body carries it."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    digest_before = fp.content_digest(root / "alpha")["digest"]
    real_walk = fp.os.walk

    def flaky_walk(top, **kwargs):
        onerror = kwargs.get("onerror")
        for dirpath, dirnames, filenames in real_walk(top, **kwargs):
            if Path(dirpath) == root and onerror is not None:
                onerror(OSError("denied"))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(fp.os, "walk", flaky_walk)
    try:
        partial = _scan(ctx, cbs, sid="s2")
    finally:
        monkeypatch.undo()
    assert partial["scan"]["error"] == "inventory-partial"
    assert partial["scan"]["complete"] is False
    assert partial["fingerprints"]["alpha"]["digest"] == digest_before
    assert partial["findings"].get("alpha", {}).get("reason") == "changed"
    assert "alpha" in partial["pending"]
    text = hooks._advisory(
        partial["pending"], "UPSTREAM_MANIFEST.md", 6, {}, partial["findings"]
    )
    assert "alpha" in text
    assert "changed since their last acknowledged review" in text
    _assert_pending_findings_invariant(partial)
    # A following complete pass keeps reporting the change.
    complete = _scan(ctx, cbs, sid="s3")
    assert complete["scan"].get("error") != "inventory-partial"
    assert complete["findings"].get("alpha", {}).get("reason") == "changed"
    assert "alpha" in complete["pending"]
    _assert_pending_findings_invariant(complete)


def test_90_partial_pass_reports_arrival_and_removes_nothing(tmp_path, monkeypatch):
    """R11 Q1/Q3: a partial pass reports arrivals and never drops a name.

    Setup: `alpha` manifested (baselined), `gamma` observer-enqueued so it
    sits in `pending` with an `untracked` finding, then `beta` arrives on
    disk right before a partial pass whose walk still lists every skill
    but reports errors. After the partial pass: `beta` is `untracked` and
    pending, `gamma` is still pending, and no name was removed."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    hooks.on_skill_lifecycle("created", skill_name="gamma")
    with hooks._STATE_LOCK:
        seeded = hooks._load(ctx)
    assert "gamma" in [p.lower() for p in seeded["pending"]]
    _skill(root, "beta")
    pending_before = {p.lower() for p in seeded["pending"]}
    real_walk = fp.os.walk

    def flaky_walk(top, **kwargs):
        onerror = kwargs.get("onerror")
        for dirpath, dirnames, filenames in real_walk(top, **kwargs):
            if Path(dirpath) == root and onerror is not None:
                onerror(OSError("denied"))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(fp.os, "walk", flaky_walk)
    try:
        partial = _scan(ctx, cbs, sid="s2")
    finally:
        monkeypatch.undo()
    assert partial["scan"]["error"] == "inventory-partial"
    assert partial["scan"]["complete"] is False
    assert partial["findings"].get("beta", {}).get("reason") == "untracked"
    assert "beta" in [p.lower() for p in partial["pending"]]
    # Nothing removed: every previously pending name is still pending.
    assert pending_before <= {p.lower() for p in partial["pending"]}
    assert "gamma" in [p.lower() for p in partial["pending"]]
    assert partial["findings"].get("gamma", {}).get("reason") == "untracked"
    text = hooks._advisory(
        partial["pending"], "UPSTREAM_MANIFEST.md", 6, {}, partial["findings"]
    )
    assert "beta" in text
    _assert_pending_findings_invariant(partial)


def test_59_ack_merge_reads_file_once(tmp_path, monkeypatch):
    """I4: acknowledge coerces the bytes already read; no second read happens."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    ack_path = tmp_path / "acks.json"
    old = '{"version": 1, "acks": {"old": {"digest": "%s", "ts": 1.0}}}' % ("ab" * 32)
    ack_path.write_text(old, encoding="utf-8")
    live = fp.content_digest(root / "alpha")["digest"]

    def no_second_read(path):
        raise AssertionError("acknowledge must not re-read the acks file")

    monkeypatch.setattr(acks_mod, "load_acks", no_second_read)
    result = acks_mod.acknowledge(ack_path, root / "alpha", "alpha", live[:8])
    assert result["ok"] is True
    doc = dict(json.loads(ack_path.read_text(encoding="utf-8"))["acks"])
    assert doc["old"]["digest"] == "ab" * 32
    assert doc["alpha"]["digest"] == live


def test_60_wrong_shape_acks_backed_up_and_recovered(tmp_path):
    """I4: valid JSON with the wrong shape is backed up; the merge continues recovered."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    live = fp.content_digest(root / "alpha")["digest"]
    for shape in ('{"acks": ["not", "a", "dict"]}', "[1, 2, 3]"):
        ack_path = tmp_path / "acks.json"
        ack_path.write_text(shape, encoding="utf-8")
        doc, recovered, backup = acks_mod._read_doc_and_recovered(ack_path)
        assert recovered is True
        assert Path(backup).read_bytes() == shape.encode("utf-8")
        assert doc == {"version": 1, "acks": {}}
        result = acks_mod.acknowledge(ack_path, root / "alpha", "alpha", live[:8])
        assert result["ok"] is True and result.get("recovered") is True
        assert list(tmp_path.glob("acks.json.corrupt.*"))
        for leftover in tmp_path.glob("acks.json.corrupt.*"):
            leftover.unlink()


def test_61_scandir_and_stat_failures_abort_skill(tmp_path, monkeypatch):
    """I5: an os.scandir failure aborts the skill; a stat failure does too."""
    import fingerprint as fp

    root = tmp_path / "skills"
    d = _skill(root, "alpha")
    sub = d / "sub"
    sub.mkdir()
    (sub / "notes.md").write_text("notes\n", encoding="utf-8")
    real_scandir = fp.os.scandir

    def flaky_scandir(path, *args, **kwargs):
        if Path(path).name == "sub":
            raise OSError("denied")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(fp.os, "scandir", flaky_scandir)
    try:
        out = fp.content_digest(d)
    finally:
        monkeypatch.undo()
    assert out["digest"] == "" and out["error"] == "read"
    (sub / "notes.md").unlink()
    sub.rmdir()
    # A stat failure on a listed file aborts the skill instead of skipping it.
    (d / "extra.md").write_text("real content\n", encoding="utf-8")
    real_stat = Path.stat

    def flaky_stat(self):
        if Path(self).name == "extra.md":
            raise OSError("denied")
        return real_stat(self)

    monkeypatch.setattr(Path, "stat", flaky_stat)
    try:
        failed = fp.content_digest(d)
    finally:
        monkeypatch.undo()
    assert failed["digest"] == "" and failed["error"] == "read"


def test_62_status_and_advisory_show_coverage():
    """I6: render_status lists up to 5 skill_errors + the scan error code;
    the advisory appends the coverage line only when skills went unreadable."""
    codes = {f"broken{i}": "skill-read-error" for i in range(6)}
    state = _fresh_state(
        scan={
            "last_attempt_ts": 2000.0,
            "last_ok_ts": 1999.5,
            "complete": False,
            "error": "skill-read-error",
            "checked": 1,
            "deferred": 0,
            "skill_errors": codes,
        }
    )
    text = hooks.render_status(state)
    assert "error: skill-read-error" in text
    assert "skill_errors: 6" in text
    for i in range(5):
        assert f"broken{i}" in text
    assert "broken5" not in text
    covered = hooks._advisory(["alpha"], "UPSTREAM_MANIFEST.md", 6, {}, None, state["scan"])
    assert "coverage: 6 skills unreadable, see status" in covered
    assert covered.splitlines()[-1] == _LINE
    one = dict(state["scan"], skill_errors={"solo": "skill-read-error"})
    single = hooks._advisory(["alpha"], "UPSTREAM_MANIFEST.md", 6, {}, None, one)
    assert "coverage: 1 skill unreadable, see status" in single
    clean = hooks._advisory(["alpha"], "UPSTREAM_MANIFEST.md", 6, {}, None, {})
    assert "coverage:" not in clean


def test_63_pure_untracked_shrink_silent_but_digest_move_reinjects():
    """I7: a pure untracked shrink stays silent; a surviving digest move re-injects."""
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "pending": ["a", "b"],
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": False,
        },
    )
    hooks.configure(ctx)
    hooks._probed = True
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is not None
    state = ctx.state.get("v1")
    state["pending"] = ["a"]
    ctx.state.set("v1", state)
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    digest_one = "ab12cd34" * 8
    digest_two = "99aa0011" * 8

    def _changed(name, digest):
        return {
            "name": name,
            "reason": "changed",
            "digest": digest,
            "files": 1,
            "truncated": False,
            "first_seen_ts": 1.0,
        }

    # An untracked->changed flip moves the surviving row: it re-injects.
    moved = ctx.state.get("v1")
    moved["pending"] = ["a"]
    moved["findings"] = {"a": _changed("a", digest_one)}
    ctx.state.set("v1", moved)
    flipped = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert flipped is not None and digest_one[:8] in flipped["context"]
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    # A surviving digest move re-injects, then goes quiet.
    moved_again = ctx.state.get("v1")
    moved_again["findings"] = {"a": _changed("a", digest_two)}
    ctx.state.set("v1", moved_again)
    out = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert out is not None and digest_two[:8] in out["context"]
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None


def test_64_adoption_runs_before_examination_under_tight_budget(tmp_path, monkeypatch):
    """I8: missing keys are adopted first (same budget); unreached keys defer."""
    root = tmp_path / "skills"
    for name in ("aaa", "zzz"):
        _skill(root, name)
    state = _fresh_state()
    _reconcile(state, root, ["aaa"], now=1000.0)
    assert set(state["fingerprints"]) == {"aaa"}
    state["fingerprints"]["aaa"]["quick"] = "stale"
    calls = {"n": 0}

    def one_visit():
        calls["n"] += 1
        return 1000.0 if calls["n"] <= 2 else 9999.0

    monkeypatch.setattr(hooks.time, "monotonic", one_visit)
    try:
        _reconcile(state, root, ["aaa", "zzz"], now=2000.0)
    finally:
        monkeypatch.undo()
    assert "zzz" in state["fingerprints"]
    assert state["scan"]["checked"] == 1
    assert state["scan"]["deferred"] == 1


def test_65_partial_pass_never_closes_baseline(tmp_path):
    """J1: a manifested skill omitted from a partial listing keeps the baseline open."""
    import fingerprint as fp

    root = tmp_path / "skills"
    for name in ("alpha", "beta"):
        _skill(root, name)
    _manifest(root, ["alpha", "beta"])
    full_dirs = fp.list_skill_dirs(root)
    assert set(full_dirs) == {"alpha", "beta"}

    def _options(**kw):
        opts = {
            "max_files": 400,
            "max_bytes": 8_000_000,
            "scan_budget_s": 5.0,
            "now": 1000.0,
            "rows": {},
            "acks": {},
        }
        opts.update(kw)
        return opts

    state = _fresh_state()
    # Beta is hidden by the failed subtree: the partial pass lists alpha only.
    hooks.reconcile_changes(
        state,
        {"alpha": full_dirs["alpha"]},
        {"alpha", "beta"},
        options=_options(partial=True),
    )
    assert set(state["fingerprints"]) == {"alpha"}
    assert state["scan"]["error"] == "inventory-partial"
    assert state["scan"]["complete"] is False
    # Alpha was adopted while the upgrade baseline was still open...
    assert state["fingerprints"]["alpha"]["ack"] == state["fingerprints"]["alpha"]["digest"]
    # ...and the baseline stays open, so the hidden skill gets the baseline ack later.
    assert state["change_baseline_done"] is False
    hooks.reconcile_changes(
        state, dict(full_dirs), {"alpha", "beta"}, options=_options(now=2000.0)
    )
    assert state["change_baseline_done"] is True
    assert state["fingerprints"]["beta"]["ack"] == state["fingerprints"]["beta"]["digest"]
    assert state["fingerprints"]["beta"]["ack"]
    assert state["findings"] == {}
    _assert_pending_findings_invariant(state)


def test_66_failing_adoption_does_not_starve_examination(tmp_path, monkeypatch):
    """J2: a persistently failing adoption retries last; the fingerprinted key is examined."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    beta = _skill(root, "beta")
    (beta / "SKILL.md").unlink()
    (beta / "SKILL.md").symlink_to(root / "nowhere.md")
    _manifest(root, ["alpha", "beta"])
    state = _fresh_state()
    _reconcile(state, root, ["alpha", "beta"], now=1000.0)
    assert set(state["fingerprints"]) == {"alpha"}
    assert state["scan"]["adoption_retry"] == ["beta"]
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    calls = {"n": 0}

    def one_visit():
        calls["n"] += 1
        return 1000.0 if calls["n"] <= 2 else 9999.0

    monkeypatch.setattr(hooks.time, "monotonic", one_visit)
    try:
        _reconcile(state, root, ["alpha", "beta"], now=2000.0)
    finally:
        monkeypatch.undo()
    # The failing adoption no longer takes the only budget slot: alpha is examined.
    finding = state["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "changed"
    assert state["scan"]["checked"] == 1
    assert state["scan"]["deferred"] == 1
    assert state["scan"]["adoption_retry"] == ["beta"]
    # A newly appeared skill pre-empts the failing retry within adoption,
    # and examination still gets its reserved half on the same pass.
    _skill(root, "gamma")
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED AGAIN\n", encoding="utf-8")
    live = fp.content_digest(root / "alpha")["digest"]
    script = iter([1000.0, 1000.0, 1000.0, 9999.0])

    def scripted():
        try:
            return next(script)
        except StopIteration:
            return 9999.0

    monkeypatch.setattr(hooks.time, "monotonic", scripted)
    try:
        _reconcile(state, root, ["alpha", "beta", "gamma"], now=3000.0)
    finally:
        monkeypatch.undo()
    assert "gamma" in state["fingerprints"]
    assert "gamma" not in state["findings"]
    assert state["findings"]["alpha"]["digest"] == live
    assert state["scan"]["adoption_retry"] == ["beta"]
    assert state["scan"]["checked"] == 2
    assert state["scan"]["deferred"] == 1
    # Repairing the failing skill adopts it and clears its retry entry.
    (beta / "SKILL.md").unlink()
    _skill(root, "beta", body="beta v1")
    _reconcile(state, root, ["alpha", "beta", "gamma"], now=4000.0)
    assert state["scan"]["adoption_retry"] == []
    assert "beta" in state["fingerprints"]
    assert "beta" not in state["findings"]
    # The retry list stays bounded (K3/R7-M4: 512) and coerces wrong shapes away.
    assert hooks._clean_adoption_retry(["k%d" % i for i in range(600)]) == [
        "k%d" % i for i in range(512)
    ]
    assert hooks._clean_adoption_retry({"beta": 1}) == []
    assert hooks._clean_adoption_retry(None) == []


def test_67_link_predicate_failures_abort_skill(tmp_path, monkeypatch):
    """J3/R7-M2: islink/realpath/containment/target-stat OSErrors abort the skill."""
    import fingerprint as fp

    root = tmp_path / "skills"
    d = _skill(root, "alpha")
    (d / "extra.md").write_text("data\n", encoding="utf-8")
    link = d / "linked.md"
    link.symlink_to(d / "extra.md")
    assert fp.content_digest(d)["error"] == ""

    def _flaky_path_attr(attr, basename):
        real = getattr(fp.os.path, attr)

        def flaky(path, *args, **kwargs):
            if os.path.basename(path) == basename:
                raise OSError("denied")
            return real(path, *args, **kwargs)

        return flaky

    # os.path.islink failing on a member aborts instead of skipping it.
    monkeypatch.setattr(fp.os.path, "islink", _flaky_path_attr("islink", "extra.md"))
    try:
        failed = fp.content_digest(d)
    finally:
        monkeypatch.undo()
    assert failed["digest"] == "" and failed["error"] == "read"
    # os.path.realpath failing on a link aborts too.
    monkeypatch.setattr(fp.os.path, "realpath", _flaky_path_attr("realpath", "linked.md"))
    try:
        failed = fp.content_digest(d)
    finally:
        monkeypatch.undo()
    assert failed["digest"] == "" and failed["error"] == "read"
    # The inside-tree containment check failing aborts as well.
    parents_prop = fp.Path.parents

    def flaky_parents(self):
        if Path(self).name == "extra.md":
            raise OSError("denied")
        return parents_prop.__get__(self, type(self))

    monkeypatch.setattr(fp.Path, "parents", property(flaky_parents))
    try:
        failed = fp.content_digest(d)
    finally:
        monkeypatch.undo()
    assert failed["digest"] == "" and failed["error"] == "read"
    # A link-target stat failure aborts instead of dropping the link
    # (R7 M2: the target kind comes from os.stat, not is_dir/is_file).
    real_stat = fp.os.stat

    def flaky_stat(path, *args, **kwargs):
        if Path(path).name == "extra.md":
            raise OSError("denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(fp.os, "stat", flaky_stat)
    try:
        failed = fp.content_digest(d)
    finally:
        monkeypatch.undo()
    assert failed["digest"] == "" and failed["error"] == "read"
    # Deliberate non-error skips still succeed: an out-of-tree link is
    # not an error, and a healthy tree digests cleanly.
    (d / "outside.md").symlink_to(tmp_path / "elsewhere.md")
    (tmp_path / "elsewhere.md").write_text("far away\n", encoding="utf-8")
    ok_dig = fp.content_digest(d)
    assert ok_dig["digest"] and ok_dig["error"] == ""
    # At scan level the aborted skill records skill-read-error, not success.
    state = _fresh_state()
    monkeypatch.setattr(fp.os.path, "islink", _flaky_path_attr("islink", "extra.md"))
    try:
        _reconcile(state, root, ["alpha"], now=1000.0)
    finally:
        monkeypatch.undo()
    assert state["fingerprints"] == {}
    assert state["scan"]["skill_errors"] == {"alpha": "skill-read-error"}
    assert state["scan"]["complete"] is False
    assert state["findings"] == {}


def test_68_session_rows_are_hashed_with_key_fallback():
    """J4: rows store as sha256[:8]; a name missing from the map uses the key."""
    import hashlib as _hashlib

    row = "a|untracked|"
    entry = hooks._session_entry(["a"], False, 1.0, "ab" * 8, {"a": row})
    assert entry["rows"] == {"a": _hashlib.sha256(row.encode()).hexdigest()[:8]}
    # Idempotent: stored hashes survive load/copy/save unchanged.
    again = hooks._session_entry(["a"], False, 1.0, "ab" * 8, entry["rows"])
    assert again["rows"] == entry["rows"]
    # Legacy full-row entries hash on load and still compare equal.
    legacy = hooks._coerce_session_entry(
        {"pending": ["a"], "missing": False, "rows": {"a": row}}
    )
    assert legacy is not None and legacy["rows"] == entry["rows"]
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "pending": ["a", "b"],
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": False,
        },
    )
    hooks.configure(ctx)
    hooks._probed = True
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is not None
    stored = ctx.state.get("v1")["advisory_sessions"]["s"]
    assert set(stored["rows"]) == {"a", "b"}
    assert all(len(v) == 8 for v in stored["rows"].values())
    # Evicting b from the stored map (as the 64-cap would) with an
    # unchanged key stays silent: the absent name falls back to the key.
    trimmed = ctx.state.get("v1")
    trimmed["advisory_sessions"]["s"]["rows"] = {"a": stored["rows"]["a"]}
    ctx.state.set("v1", trimmed)
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    # Flipping b untracked->changed moves the key, so the absent name
    # re-injects through the key fallback.
    flipped = ctx.state.get("v1")
    flipped["findings"] = {
        "b": {
            "name": "b",
            "reason": "changed",
            "digest": "ab12cd34" * 8,
            "files": 1,
            "truncated": False,
            "first_seen_ts": 1.0,
        }
    }
    ctx.state.set("v1", flipped)
    out = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert out is not None and "ab12cd34" in out["context"]
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None


def test_69_session_row_map_capped_at_64_names():
    """J4: the stored row map never exceeds 64 names regardless of library size."""
    names = ["skill-%03d" % i for i in range(200)]
    entry = hooks._session_entry(
        names, False, 1.0, "cd" * 8, {n: "%s|untracked|" % n for n in names}
    )
    assert len(entry["rows"]) == 64
    assert set(entry["rows"]) == set(sorted(names)[:64])
    for value in entry["rows"].values():
        assert len(value) == 8
        int(value, 16)


def test_70_unacked_change_survives_row_removal_and_restore(tmp_path):
    """K1: a post-baseline (ack="") skill's edit is reported after its row
    disappears on a trusted pass and comes back (was silently lost)."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    state = _fresh_state()
    _reconcile(state, root, ["alpha"], now=1000.0)
    assert state["change_baseline_done"] is True
    _skill(root, "beta")
    _manifest(root, ["alpha", "beta"])
    _reconcile(state, root, ["alpha", "beta"], now=2000.0)
    beta = state["fingerprints"]["beta"]
    assert beta["ack"] == ""
    adopted_digest = beta["digest"]
    assert adopted_digest
    assert beta.get("origin") == adopted_digest
    (root / "beta" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    _reconcile(state, root, ["alpha", "beta"], now=3000.0)
    assert state["findings"].get("beta", {}).get("reason") == "changed"
    # The row disappears on a trusted pass: the finding drops (accepted
    # row-removed behaviour) while the fingerprint keeps digest and ack.
    _manifest(root, ["alpha"])
    _reconcile(state, root, ["alpha"], now=4000.0)
    assert state["scan"]["complete"] is True
    assert "beta" not in state["findings"]
    assert state["fingerprints"]["beta"]["digest"] != adopted_digest
    assert state["fingerprints"]["beta"]["ack"] == ""
    # The row returns: the edit is reported again.
    _manifest(root, ["alpha", "beta"])
    _reconcile(state, root, ["alpha", "beta"], now=5000.0)
    resumed = state["findings"].get("beta")
    assert resumed is not None and resumed["reason"] == "changed"
    assert resumed["digest"] != adopted_digest


def test_71_unacked_change_survives_track_changes_toggle(tmp_path):
    """K1: dropping change findings (track_changes off) and re-scanning
    (track_changes on) re-reports a post-baseline skill's pending edit."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    state = _fresh_state()
    _reconcile(state, root, ["alpha"], now=1000.0)
    _skill(root, "beta")
    _manifest(root, ["alpha", "beta"])
    _reconcile(state, root, ["alpha", "beta"], now=2000.0)
    adopted_digest = state["fingerprints"]["beta"]["digest"]
    (root / "beta" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    _reconcile(state, root, ["alpha", "beta"], now=3000.0)
    assert state["findings"].get("beta", {}).get("reason") == "changed"
    # track_changes off: every change finding is dropped, fingerprints stay.
    hooks._drop_change_findings(state)
    assert "beta" not in state["findings"]
    assert state["fingerprints"]["beta"]["digest"] != adopted_digest
    # track_changes on again: the edit is reported, not lost.
    _reconcile(state, root, ["alpha", "beta"], now=4000.0)
    resumed = state["findings"].get("beta")
    assert resumed is not None and resumed["reason"] == "changed"
    assert resumed["digest"] != adopted_digest


def test_72_fingerprint_origin_backfills_from_stored_ack():
    """K1: entries predating `origin` backfill from ack; nothing fabricated."""
    digest = "ab12cd34" * 8
    baseline = hooks._clean_fingerprints(
        {"k": {"digest": digest, "ack": digest, "quick": "q"}}
    )
    assert baseline["k"]["origin"] == digest
    unacked = hooks._clean_fingerprints(
        {"k": {"digest": "ff" * 32, "ack": "", "quick": "q"}}
    )
    assert unacked["k"]["origin"] == ""
    kept = hooks._clean_fingerprints(
        {"k": {"digest": "ff" * 32, "ack": "", "origin": digest, "quick": "q"}}
    )
    assert kept["k"]["origin"] == digest


def test_73_entry_type_eacces_aborts_skill(tmp_path, monkeypatch):
    """K2: an EACCES from the entry-type call aborts with error="read"."""
    import errno

    import fingerprint as fp

    root = tmp_path / "skills"
    d = _skill(root, "alpha")
    (d / "extra.md").write_text("data\n", encoding="utf-8")
    assert fp.content_digest(d)["error"] == ""
    real_stat = fp.os.stat
    target = os.path.normpath(os.path.join(str(d), "extra.md"))

    def flaky_stat(path, *args, **kwargs):
        if os.path.normpath(os.fspath(path)) == target:
            raise PermissionError(
                errno.EACCES, "Permission denied", os.fspath(path)
            )
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(fp.os, "stat", flaky_stat)
    try:
        failed = fp.content_digest(d)
    finally:
        monkeypatch.undo()
    assert failed["digest"] == "" and failed["error"] == "read"


def test_74_bounded_adoption_keeps_new_skill_from_starving(tmp_path, monkeypatch):
    """K3/L1/L2: 12 failing keys and one late healthy key. Fresh adoption
    is uncapped (L2); retries rotate 4 per pass from the cursor (L1). The
    healthy key is adopted on its first pass under a one-slot budget."""
    root = tmp_path / "skills"
    failing = ["a%02d" % i for i in range(1, 13)]
    for name in failing:
        d = _skill(root, name)
        (d / "SKILL.md").unlink()
        (d / "SKILL.md").symlink_to(root / "nowhere.md")
    _manifest(root, failing)
    state = _fresh_state()
    # Pass 1 (generous budget): no fresh cap — all 12 attempted, all fail.
    _reconcile(state, root, failing, now=1000.0)
    assert state["scan"]["checked"] == 12
    assert state["scan"]["deferred"] == 0
    assert state["scan"]["adoption_retry"] == failing
    assert state["scan"]["adoption_cursor"] == 0
    # Pass 2 (generous budget): fresh set empty; the retry slice rotates —
    # the first 4 retry-list entries are attempted and the cursor advances.
    _reconcile(state, root, failing, now=2000.0)
    assert state["scan"]["checked"] == 4
    assert state["scan"]["deferred"] == 8
    assert state["scan"]["adoption_retry"] == failing
    assert state["scan"]["adoption_cursor"] == 4
    assert set(state["scan"]["skill_errors"]) == set(failing[:4])
    # The healthy key appears late, sorted after every failing key. Under a
    # one-slot adoption budget it is adopted on its first pass: no failing
    # key poses as fresh ahead of it any more.
    _skill(root, "z9")
    _manifest(root, failing + ["z9"])
    calls = {"n": 0}

    def one_visit():
        calls["n"] += 1
        return 1000.0 if calls["n"] <= 2 else 9999.0

    monkeypatch.setattr(hooks.time, "monotonic", one_visit)
    try:
        _reconcile(state, root, failing + ["z9"], now=3000.0)
    finally:
        monkeypatch.undo()
    assert "z9" in state["fingerprints"]
    assert "z9" not in state["findings"]
    assert state["fingerprints"]["z9"].get("origin") == (
        state["fingerprints"]["z9"]["digest"]
    )


def test_75_healed_retry_key_sorts_last_but_still_rotates_in(tmp_path):
    """L1: a healed retry key sorting after still-failing keys is adopted
    within ceil(len/4) passes (the a12 case)."""
    import math

    root = tmp_path / "skills"
    names = ["a%02d" % i for i in (1, 2, 3, 4, 12)]
    for name in names:
        d = _skill(root, name)
        (d / "SKILL.md").unlink()
        (d / "SKILL.md").symlink_to(root / "nowhere.md")
    _manifest(root, names)
    state = _fresh_state()
    # Pass 1: all 5 fresh-attempted, all fail; the retry list is stored order.
    _reconcile(state, root, names, now=1000.0)
    assert state["scan"]["adoption_retry"] == names
    assert state["scan"]["adoption_cursor"] == 0
    # Pass 2: the slice starts at the cursor — a01..a04 only.
    _reconcile(state, root, names, now=2000.0)
    assert set(state["scan"]["skill_errors"]) == set(names[:4])
    assert state["scan"]["adoption_cursor"] == 4
    assert "a12" not in state["fingerprints"]
    # Pass 3: rotation wraps — a12 is attempted (still broken, still fails).
    _reconcile(state, root, names, now=3000.0)
    assert "a12" in state["scan"]["skill_errors"]
    assert state["scan"]["adoption_cursor"] == 8 % len(names)
    # Heal a12: it is adopted on the very next pass — within ceil(5/4).
    d12 = root / "a12"
    (d12 / "SKILL.md").unlink()
    (d12 / "SKILL.md").write_text("---\n---\nhello\n", encoding="utf-8")
    _reconcile(state, root, names, now=4000.0)
    assert "a12" in state["fingerprints"]
    assert "a12" not in state["findings"]
    assert state["scan"]["adoption_retry"] == names[:4]
    assert math.ceil(len(names) / 4) == 2


def test_76_sixty_skill_library_closes_baseline_in_one_pass(tmp_path):
    """L2: 60 manifested skills with the default budget close the upgrade
    baseline in a single pass with no findings."""
    root = tmp_path / "skills"
    names = ["s%02d" % i for i in range(60)]
    for name in names:
        _skill(root, name)
    _manifest(root, names)
    state = _fresh_state()
    _reconcile(state, root, names, now=1000.0, budget=hooks._DEFAULT_SCAN_BUDGET_S)
    assert len(state["fingerprints"]) == 60
    assert state["change_baseline_done"] is True
    assert state["findings"] == {}
    assert state["scan"]["complete"] is True
    assert state["scan"]["deferred"] == 0


def test_77_subdir_resolve_oserror_aborts_skill(tmp_path, monkeypatch):
    """L3: an OSError from a subdirectory resolve aborts the skill with
    digest="" and error="read" instead of silently dropping the subtree."""
    from pathlib import Path

    import fingerprint as fp

    root = tmp_path / "skills"
    d = _skill(root, "alpha")
    sub = d / "sub"
    sub.mkdir()
    (sub / "notes.md").write_text("notes\n", encoding="utf-8")
    healthy = fp.content_digest(d)
    assert healthy["error"] == "" and healthy["digest"]
    assert healthy["files"] == 2
    real_resolve = Path.resolve
    target = os.path.normpath(str(sub))

    def flaky_resolve(self, *args, **kwargs):
        if os.path.normpath(os.fspath(self)) == target:
            raise OSError(5, "I/O error", os.fspath(self))
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", flaky_resolve)
    try:
        failed = fp.content_digest(d)
    finally:
        monkeypatch.undo()
    assert failed["digest"] == "" and failed["error"] == "read"


def test_78_adoption_cursor_persists_clamps_and_coerces(tmp_path):
    """L1: the retry slice follows stored retry-list order (not sorted
    order); the cursor persists across passes and clamps when the list
    shrinks; wrong shapes coerce to 0 on load."""
    root = tmp_path / "skills"
    names = ["f%02d" % i for i in range(1, 7)]
    for name in names:
        d = _skill(root, name)
        (d / "SKILL.md").unlink()
        (d / "SKILL.md").symlink_to(root / "nowhere.md")
    _manifest(root, names)
    state = _fresh_state()
    _reconcile(state, root, names, now=1000.0)
    assert state["scan"]["adoption_retry"] == names
    # Retry-list order, not sorted order: reverse the stored list and the
    # next slice follows the stored order.
    state["scan"]["adoption_retry"] = list(reversed(names))
    _reconcile(state, root, names, now=2000.0)
    assert set(state["scan"]["skill_errors"]) == set(names[-1:-5:-1])
    assert state["scan"]["adoption_cursor"] == 4
    # Persistence: the following pass continues the rotation (wrapping).
    _reconcile(state, root, names, now=3000.0)
    assert set(state["scan"]["skill_errors"]) == {
        names[1],
        names[0],
        names[5],
        names[4],
    }
    assert state["scan"]["adoption_cursor"] == 8 % len(names)
    # Clamping: only one key still needs adoption — the cursor clamps.
    import shutil

    for name in names[1:]:
        shutil.rmtree(root / name)
    _manifest(root, names[:1])
    _reconcile(state, root, names[:1], now=4000.0)
    assert state["scan"]["adoption_retry"] == names[:1]
    assert state["scan"]["adoption_cursor"] == 0
    # Healing the last key empties the list and zeroes the cursor.
    (root / names[0] / "SKILL.md").unlink()
    _skill(root, names[0], body="healed")
    _reconcile(state, root, names[:1], now=5000.0)
    assert state["scan"]["adoption_retry"] == []
    assert state["scan"]["adoption_cursor"] == 0
    assert names[0] in state["fingerprints"]
    # Wrong shapes coerce on load.
    assert hooks._coerce_cursor("3") == 3
    assert hooks._coerce_cursor(2.9) == 2
    assert hooks._coerce_cursor(True) == 0
    assert hooks._coerce_cursor(-2) == 0
    assert hooks._coerce_cursor(None) == 0
    assert hooks._coerce_cursor("nope") == 0
    dirty = hooks._empty_state()
    dirty["scan"]["adoption_cursor"] = "nope"
    dirty["scan"]["adoption_retry"] = {"f01": 1}
    scan = hooks._ensure_scan(dirty)
    assert scan["adoption_cursor"] == 0
    assert scan["adoption_retry"] == []


def test_79_partial_inventory_without_skill_errors_still_advises_coverage(tmp_path):
    """R7 M1: a partial inventory with no per-skill error still gets an
    advisory coverage line; both parts combine when both are present."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    full_dirs = fp.list_skill_dirs(root)
    state = _fresh_state()
    hooks.reconcile_changes(
        state,
        {"alpha": full_dirs["alpha"]},
        {"alpha", "beta"},
        options={
            "max_files": 400,
            "max_bytes": 8_000_000,
            "scan_budget_s": 5.0,
            "now": 1000.0,
            "rows": {},
            "acks": {},
            "partial": True,
        },
    )
    assert state["scan"]["error"] == "inventory-partial"
    assert state["scan"]["skill_errors"] == {}
    covered = hooks._advisory(
        ["alpha"], "UPSTREAM_MANIFEST.md", 6, {}, None, state["scan"]
    )
    assert "coverage: inventory partial, see status" in covered
    assert len(covered) <= hooks._HARD_CAP
    both = dict(
        state["scan"],
        skill_errors={"a": "skill-read-error", "b": "skill-read-error"},
    )
    combined = hooks._advisory(
        ["alpha"], "UPSTREAM_MANIFEST.md", 6, {}, None, both
    )
    assert "coverage: 2 skills unreadable, inventory partial, see status" in combined
    assert len(combined) <= hooks._HARD_CAP


def test_80_symlink_target_stat_eacces_aborts_skill(tmp_path, monkeypatch):
    """R7 M2: a symlink whose target stat raises EACCES aborts with
    digest="" and error="read" — even when is_dir/is_file swallow OSError
    into False (the predicate behaviour on some interpreters)."""
    import errno

    import fingerprint as fp

    root = tmp_path / "skills"
    d = _skill(root, "alpha")
    target_file = d / "data.md"
    target_file.write_text("data\n", encoding="utf-8")
    (d / "lnk").symlink_to(target_file)
    healthy = fp.content_digest(d)
    assert healthy["error"] == "" and healthy["digest"]
    real_stat = fp.os.stat
    wanted = {
        os.path.normpath(str(target_file)),
        os.path.normpath(os.path.realpath(target_file)),
    }

    def flaky_stat(path, *args, **kwargs):
        # Only the bare follow-stat (the link-target typing call) fails:
        # lstat-style calls and pathlib's explicit-follow stats stay
        # healthy, so only the M2 path can abort the skill.
        if "follow_symlinks" not in kwargs and os.path.normpath(
            os.fspath(path)
        ) in wanted:
            raise PermissionError(
                errno.EACCES, "Permission denied", os.fspath(path)
            )
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(fp.os, "stat", flaky_stat)
    # Simulate a swallowing interpreter, narrowly: only the link target's
    # kind predicates report False instead of raising; the target os.stat
    # must still abort the skill.
    real_is_dir = Path.is_dir
    real_is_file = Path.is_file

    def swallowing_is_dir(self):
        if os.path.normpath(os.fspath(self)) in wanted:
            return False
        return real_is_dir(self)

    def swallowing_is_file(self):
        if os.path.normpath(os.fspath(self)) in wanted:
            return False
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_dir", swallowing_is_dir)
    monkeypatch.setattr(Path, "is_file", swallowing_is_file)
    try:
        failed = fp.content_digest(d)
    finally:
        monkeypatch.undo()
    assert failed["digest"] == "" and failed["error"] == "read"


def test_81_unacked_originless_change_survives_row_round_trip(tmp_path):
    """R7 M3: an unacked fingerprint with empty origin (state written before
    K1) keeps its change finding across row removal and restore; an ack
    for the live digest still clears it."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    state = _fresh_state()
    _reconcile(state, root, ["alpha"], now=1000.0)
    assert state["change_baseline_done"] is True
    # Simulate state written before K1: unacked with no adoption reference.
    entry = state["fingerprints"]["alpha"]
    entry["ack"] = ""
    entry["origin"] = ""
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    _reconcile(state, root, ["alpha"], now=2000.0)
    assert state["findings"].get("alpha", {}).get("reason") == "changed"
    # The row disappears on a trusted pass: the unreviewable finding stays.
    _manifest(root, [])
    _reconcile(state, root, [], now=3000.0)
    assert state["scan"]["complete"] is True
    assert state["findings"].get("alpha", {}).get("reason") == "changed"
    # The row returns: the finding is still there (never reconstructed).
    _manifest(root, ["alpha"])
    _reconcile(state, root, ["alpha"], now=4000.0)
    resumed = state["findings"].get("alpha")
    assert resumed is not None and resumed["reason"] == "changed"
    # An ack for the live digest still clears it.
    live = state["fingerprints"]["alpha"]["digest"]
    _reconcile(
        state, root, ["alpha"], now=5000.0, acks_dict={"alpha": {"digest": live}}
    )
    assert "alpha" not in state["findings"]
    _assert_pending_findings_invariant(state)


def test_82_adoption_retry_cap_512_enforced_and_coerced():
    """R7 M4: the retry list keeps 512 names; wrong shapes coerce away."""
    assert hooks._ADOPTION_RETRY_CAP == 512
    assert hooks._clean_adoption_retry(["k%03d" % i for i in range(600)]) == [
        "k%03d" % i for i in range(512)
    ]
    assert hooks._clean_adoption_retry({"k001": 1}) == []
    assert hooks._clean_adoption_retry(None) == []


def test_83_full_examination_slice_still_leaves_retry_time(tmp_path, monkeypatch):
    """R7 M5: a pass whose examination fills its whole 75% slice still makes
    retry attempts in the final quarter and advances the cursor."""
    root = tmp_path / "skills"
    good = ["g%d" % i for i in range(1, 6)]
    bad = ["b%d" % i for i in range(1, 7)]
    for name in good:
        _skill(root, name)
    for name in bad:
        d = _skill(root, name)
        (d / "SKILL.md").unlink()
        (d / "SKILL.md").symlink_to(root / "nowhere.md")
    _manifest(root, good + bad)
    state = _fresh_state()
    _reconcile(state, root, good + bad, now=1000.0)
    assert state["scan"]["adoption_retry"] == bad
    assert state["scan"]["adoption_cursor"] == 0
    # Each examination visibly consumes budget; the scripted clock fills the
    # 75% examination slice (3 x 30s on a 100s budget) while the final
    # quarter stays open for retries.
    clock = {"now": 1000.0}
    real_examine = hooks._examine_skill

    def timed_examine(state_arg, key, *args, **kwargs):
        try:
            return real_examine(state_arg, key, *args, **kwargs)
        finally:
            clock["now"] += 30.0

    monkeypatch.setattr(hooks.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(hooks, "_examine_skill", timed_examine)
    try:
        _reconcile(state, root, good + bad, now=2000.0, budget=100.0)
    finally:
        monkeypatch.undo()
    # Examinations stopped at the 75% slice (g4, g5 deferred) while the
    # reserved final quarter still ran the 4-attempt retry slice.
    assert state["scan"]["checked"] == 7
    assert state["scan"]["deferred"] == 4
    assert set(state["scan"]["skill_errors"]) == set(bad[:4])
    assert state["scan"]["adoption_retry"] == bad
    assert state["scan"]["adoption_cursor"] == 4


def test_84_ack_keeps_unmanifested_skill_reported_as_untracked(tmp_path):
    """R9 O2: acking a `changed` finding for a key with no manifest row
    replaces it with an `untracked` finding (kept name and first_seen_ts,
    empty digest, files 0) and re-derives pending — the skill stays
    reported, and a follow-up probe still reports it. Other keys untouched."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    _skill(root, "beta")
    _manifest(root, ["alpha", "beta"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    _wire(ctx)
    hooks.on_session_start(session_id="s")
    state = ctx.state.get("v1")
    assert state["change_baseline_done"] is True
    # Pre-K1 shape for alpha: unacked with no adoption reference.
    state["fingerprints"]["alpha"]["ack"] = ""
    state["fingerprints"]["alpha"]["origin"] = ""
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED A\n", encoding="utf-8")
    (root / "beta" / "SKILL.md").write_text("---\n---\nEDITED B\n", encoding="utf-8")
    hooks.on_session_start(session_id="s2")
    state = ctx.state.get("v1")
    assert state["findings"].get("alpha", {}).get("reason") == "changed"
    assert state["findings"].get("beta", {}).get("reason") == "changed"
    # Alpha's row disappears on a trusted pass: the unreviewable finding
    # is retained (R7 M3) and can never clear by re-examination.
    _manifest(root, ["beta"])
    hooks.on_session_start(session_id="s3")
    state = ctx.state.get("v1")
    assert state["findings"].get("alpha", {}).get("reason") == "changed"
    assert state["findings"].get("beta", {}).get("reason") == "changed"
    assert "alpha" in [p.lower() for p in state["pending"]]
    first_seen = state["findings"]["alpha"]["first_seen_ts"]
    # A successful ack for the live digest converts alpha's finding to
    # `untracked` at once instead of deleting it.
    live = fp.content_digest(root / "alpha")["digest"]
    text, rc = hooks._do_ack("alpha", live[:8])
    assert rc == 0, text
    assert text == f"acked alpha {live[:8]}"
    # Untracked on the next read; pending still names alpha; beta untouched.
    with hooks._STATE_LOCK:
        reread = hooks._load(ctx)
    kept = reread["findings"].get("alpha")
    assert kept is not None and kept["reason"] == "untracked"
    assert kept["digest"] == "" and kept["files"] == 0
    assert kept["first_seen_ts"] == first_seen
    assert "alpha" in [p.lower() for p in reread["pending"]]
    assert reread["findings"].get("beta", {}).get("reason") == "changed"
    assert "beta" in [p.lower() for p in reread["pending"]]
    _assert_pending_findings_invariant(reread)
    # A follow-up probe still reports the unmanifested skill.
    hooks.on_session_start(session_id="s4")
    with hooks._STATE_LOCK:
        probed = hooks._load(ctx)
    assert probed["findings"].get("alpha", {}).get("reason") == "untracked"
    assert "alpha" in [p.lower() for p in probed["pending"]]
    _assert_pending_findings_invariant(probed)


def test_87_ack_keeps_version_mismatch_on_manifested_skill(tmp_path):
    """R9 O1: acking a manifested skill whose finding is
    `version-mismatch` returns `acked` and leaves the finding in place —
    the digest matches but the declared version still disagrees with the
    manifest row. A later pass clears it through the normal equality path
    once versions agree."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha", version="1.0.0")
    _manifest(root, ["alpha"], versions={"alpha": "1.0.0"})
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    _wire(ctx)
    hooks.on_session_start(session_id="s")
    state = ctx.state.get("v1")
    assert state["change_baseline_done"] is True
    # Local ver bump with identical content -> version-mismatch.
    _manifest(root, ["alpha"], versions={"alpha": "2.0.0"})
    hooks.on_session_start(session_id="s2")
    state = ctx.state.get("v1")
    assert state["findings"].get("alpha", {}).get("reason") == "version-mismatch"
    assert "alpha" in [p.lower() for p in state["pending"]]
    live = fp.content_digest(root / "alpha")["digest"]
    text, rc = hooks._do_ack("alpha", live[:8])
    assert rc == 0, text
    assert text == f"acked alpha {live[:8]}"
    # Still present on the next read.
    with hooks._STATE_LOCK:
        reread = hooks._load(ctx)
    kept = reread["findings"].get("alpha")
    assert kept is not None and kept["reason"] == "version-mismatch"
    assert kept["digest"] == live
    assert "alpha" in [p.lower() for p in reread["pending"]]
    _assert_pending_findings_invariant(reread)
    # Once versions agree, a later pass clears it through the normal path.
    _manifest(root, ["alpha"], versions={"alpha": "1.0.0"})
    hooks.on_session_start(session_id="s3")
    with hooks._STATE_LOCK:
        cleared = hooks._load(ctx)
    assert "alpha" not in cleared["findings"]
    assert "alpha" not in [p.lower() for p in cleared["pending"]]
    _assert_pending_findings_invariant(cleared)


def test_88_ack_clears_changed_on_manifested_skill(tmp_path):
    """R9 O1 manifested path: acking a `changed` finding for a manifested
    skill deletes the finding, and the next probe raises nothing (the
    digest still equals the recorded ack and versions agree)."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    _wire(ctx)
    hooks.on_session_start(session_id="s")
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    hooks.on_session_start(session_id="s2")
    state = ctx.state.get("v1")
    assert state["findings"].get("alpha", {}).get("reason") == "changed"
    live = fp.content_digest(root / "alpha")["digest"]
    text, rc = hooks._do_ack("alpha", live[:8])
    assert rc == 0, text
    assert text == f"acked alpha {live[:8]}"
    with hooks._STATE_LOCK:
        reread = hooks._load(ctx)
    assert "alpha" not in reread["findings"]
    assert "alpha" not in [p.lower() for p in reread["pending"]]
    _assert_pending_findings_invariant(reread)
    hooks.on_session_start(session_id="s3")
    with hooks._STATE_LOCK:
        probed = hooks._load(ctx)
    assert "alpha" not in probed["findings"]
    assert "alpha" not in [p.lower() for p in probed["pending"]]
    _assert_pending_findings_invariant(probed)


def test_85_content_digest_types_regular_files_from_stat(tmp_path, monkeypatch):
    """R8 N2: the file type comes from the `stat` result already obtained;
    no second `is_file()` call can silently drop a file from a digest
    reported as success. Non-regular entries are deliberate counted
    skips; a stat failure still aborts."""
    import fingerprint as fp

    d = tmp_path / "skills" / "alpha"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\n---\nbase\n", encoding="utf-8")
    (d / "second.md").write_text("more\n", encoding="utf-8")
    full = fp.content_digest(d)
    assert full["error"] == "" and full["digest"] and full["files"] == 2
    # A permission flip / race between stat and a second type check must
    # not drop the file from a success digest.
    real_is_file = Path.is_file

    def flipping_is_file(self):
        if self.name == "SKILL.md":
            return False
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", flipping_is_file)
    assert fp.content_digest(d) == full
    # The second check is gone entirely: even a raising is_file is ignored.
    def raising_is_file(self):
        raise AssertionError("is_file must not be consulted")

    monkeypatch.setattr(Path, "is_file", raising_is_file)
    assert fp.content_digest(d) == full
    monkeypatch.undo()
    # A non-regular entry (here a FIFO surfacing after enumeration, as in
    # a replacement race) is skipped deliberately and counted.
    fifo = d / "pipe"
    os.mkfifo(fifo)
    real_iter = fp._iter_sorted_skill_files

    def with_fifo(skill_dir, **kw):
        yield from real_iter(skill_dir, **kw)
        yield ("pipe", fifo, False)

    monkeypatch.setattr(fp, "_iter_sorted_skill_files", with_fifo)
    out = fp.content_digest(d)
    assert out["error"] == "" and out["digest"] == full["digest"]
    assert out["files"] == 2 and out["skipped"] == 1
    # A stat failure still aborts instead of reporting a partial digest.
    real_stat = Path.stat

    def boom_stat(self):
        if self.name == "second.md":
            raise OSError("EACCES")
        return real_stat(self)

    monkeypatch.setattr(Path, "stat", boom_stat)
    failed = fp.content_digest(d)
    assert failed["digest"] == "" and failed["error"] == "read"


def test_86_empty_retry_list_keeps_full_examination_slice(tmp_path, monkeypatch):
    """R8 N4: with nothing queued for retry, the examination phase runs to
    the full budget (`exam_deadline = retry_deadline`)."""
    root = tmp_path / "skills"
    names = ["g%d" % i for i in range(1, 6)]
    for name in names:
        _skill(root, name)
    _manifest(root, names)
    state = _fresh_state()
    _reconcile(state, root, names, now=1000.0)
    assert state["scan"]["adoption_retry"] == []
    assert state["change_baseline_done"] is True
    # Each examination visibly consumes budget: 5 x 20s fits the full 100s
    # budget but not the old 75% slice (which would stop after 4 and
    # defer g5).
    clock = {"now": 1000.0}
    real_examine = hooks._examine_skill

    def timed_examine(state_arg, key, *args, **kwargs):
        try:
            return real_examine(state_arg, key, *args, **kwargs)
        finally:
            clock["now"] += 20.0

    monkeypatch.setattr(hooks.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(hooks, "_examine_skill", timed_examine)
    try:
        _reconcile(state, root, names, now=2000.0, budget=100.0)
    finally:
        monkeypatch.undo()
    assert state["scan"]["checked"] == 5
    assert state["scan"]["deferred"] == 0
    assert state["scan"]["complete"] is True
    _assert_pending_findings_invariant(state)


def test_41_cron_prompt_keeps_change_findings_for_humans():
    """F15: the drift prompt never clears changed/version-mismatch findings."""
    assert (
        "Never acknowledge changed or version-mismatch findings; "
        "report them for human review." in hooks.PROMPT_AUTO_RESOLVE
    )
    assert (
        "Never dismiss, acknowledge, or add to the dismissed config list a changed "
        "or version-mismatch finding; report it for human review."
        in hooks.PROMPT_AUTO_RESOLVE
    )


def test_89_partial_name_pass_preserves_pending_untracked_state(tmp_path, monkeypatch):
    """R10 P1: a partial name pass must not delete untracked findings.

    Setup: beta arrives after the first sweep, so a complete pass pends it
    with an `untracked` finding. A later pass whose walk omits beta WITH
    errors must leave pending, findings, baseline, and dismissed untouched
    and record `scan["error"] == "inventory-partial"`. A following complete
    pass behaves normally (beta still reported, error cleared)."""
    import copy

    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    _wire(ctx)
    hooks.on_session_start(session_id="s")
    # Beta arrives after the first sweep, so the next complete pass pends it.
    _skill(root, "beta")
    hooks.on_session_start(session_id="s1")
    with hooks._STATE_LOCK:
        before = copy.deepcopy(hooks._load(ctx))
    assert "beta" in [p.lower() for p in before["pending"]]
    assert before["findings"].get("beta", {}).get("reason") == "untracked"
    assert before["scan"].get("error") != "inventory-partial"
    first_seen = before["findings"]["beta"]["first_seen_ts"]
    # The partial walk omits beta and reports errors, at both _probe call
    # sites (the name listing and the change-tracking listing).
    full = hooks.fingerprint.list_skill_dirs(root)
    assert "beta" in full
    partial = hooks.fingerprint.SkillDirs(
        {k: v for k, v in full.items() if k != "beta"},
        errors=["walk-error"],
    )
    targets = [hooks.fingerprint]
    fm = getattr(hooks.probe, "fingerprint_mod", None)
    if fm is not None and fm is not hooks.fingerprint:
        targets.append(fm)
    for target in targets:
        monkeypatch.setattr(
            target, "list_skill_dirs", lambda _root: partial
        )
    hooks.on_session_start(session_id="s2")
    with hooks._STATE_LOCK:
        during = hooks._load(ctx)
    assert during["pending"] == before["pending"]
    assert during["findings"] == before["findings"]
    assert during["baseline"] == before["baseline"]
    assert during["dismissed"] == before["dismissed"]
    assert during["findings"].get("beta", {}).get("reason") == "untracked"
    assert during["findings"]["beta"]["first_seen_ts"] == first_seen
    assert "beta" in [p.lower() for p in during["pending"]]
    assert during["scan"].get("error") == "inventory-partial"
    _assert_pending_findings_invariant(during)
    # A following complete pass behaves normally: beta is still reported.
    monkeypatch.undo()
    hooks.on_session_start(session_id="s3")
    with hooks._STATE_LOCK:
        after = hooks._load(ctx)
    assert after["findings"].get("beta", {}).get("reason") == "untracked"
    assert "beta" in [p.lower() for p in after["pending"]]
    assert after["scan"].get("error") != "inventory-partial"
    _assert_pending_findings_invariant(after)


def _flaky_walk_partial(monkeypatch, root):
    """Make every walk report errors while still listing every skill."""
    import fingerprint as fp

    real_walk = fp.os.walk

    def flaky_walk(top, **kwargs):
        onerror = kwargs.get("onerror")
        for dirpath, dirnames, filenames in real_walk(top, **kwargs):
            if Path(dirpath) == root and onerror is not None:
                onerror(OSError("denied"))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(fp.os, "walk", flaky_walk)


def test_91_partial_row_removal_reports_untracked(tmp_path, monkeypatch):
    """R12: a manifest row removed while the inventory stays partial.

    Setup: `alpha` manifested, baselined, edited (so it holds a `changed`
    finding), then its manifest row is removed while it stays on disk.
    After a partial pass whose walk still lists every skill: the finding
    is `untracked` (`digest=""`, `files=0`, kept `first_seen_ts`) and the
    name is pending. A following complete pass keeps reporting it."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    first_seen = changed["findings"]["alpha"]["first_seen_ts"]
    _manifest(root, [])
    _flaky_walk_partial(monkeypatch, root)
    try:
        partial = _scan(ctx, cbs, sid="s3")
    finally:
        monkeypatch.undo()
    assert partial["scan"]["error"] == "inventory-partial"
    assert partial["scan"]["complete"] is False
    finding = partial["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "untracked"
    assert finding["digest"] == ""
    assert finding["files"] == 0
    assert finding["first_seen_ts"] == first_seen
    assert "alpha" in [p.lower() for p in partial["pending"]]
    _assert_pending_findings_invariant(partial)
    # A following complete pass keeps reporting the skill as untracked.
    complete = _scan(ctx, cbs, sid="s4")
    assert complete["scan"].get("error") != "inventory-partial"
    assert complete["findings"].get("alpha", {}).get("reason") == "untracked"
    assert "alpha" in [p.lower() for p in complete["pending"]]
    _assert_pending_findings_invariant(complete)


def test_92_partial_pass_drops_dismissed_without_regain(tmp_path, monkeypatch):
    """R12: dismissing on a partial pass removes findings of any reason.

    Setup: `alpha` manifested and edited (`changed` finding), `beta` an
    unmanifested arrival (`untracked` finding); both pending. After both
    are dismissed, a partial pass leaves no finding and no pending name
    for either — the dropped `changed` finding must not come back as an
    `untracked` finding through the mirror. A second partial pass regains
    nothing either."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    _skill(root, "beta")
    arrived = _scan(ctx, cbs, sid="s3")
    assert arrived["findings"].get("beta", {}).get("reason") == "untracked"
    assert {"alpha", "beta"} <= {p.lower() for p in arrived["pending"]}
    ctx.config["dismissed"] = ["alpha", "beta"]
    _flaky_walk_partial(monkeypatch, root)
    try:
        partial = _scan(ctx, cbs, sid="s4")
    finally:
        monkeypatch.undo()
    assert partial["scan"]["error"] == "inventory-partial"
    assert partial["scan"]["complete"] is False
    assert "alpha" not in partial["findings"]
    assert "beta" not in partial["findings"]
    assert "alpha" not in [p.lower() for p in partial["pending"]]
    assert "beta" not in [p.lower() for p in partial["pending"]]
    _assert_pending_findings_invariant(partial)
    # A second partial pass regains nothing through the untracked mirror.
    _flaky_walk_partial(monkeypatch, root)
    try:
        again = _scan(ctx, cbs, sid="s5")
    finally:
        monkeypatch.undo()
    assert again["scan"]["error"] == "inventory-partial"
    assert "alpha" not in again["findings"]
    assert "beta" not in again["findings"]
    assert "alpha" not in [p.lower() for p in again["pending"]]
    assert "beta" not in [p.lower() for p in again["pending"]]
    _assert_pending_findings_invariant(again)


def test_93_partial_pass_clears_stale_manifested_pending(tmp_path, monkeypatch):
    """R12: a manifested key pending with no finding leaves `pending`.

    Setup: `alpha` manifested and clean, then `Alpha` is injected into
    `pending` with no finding behind it. A partial pass removes the stale
    name instead of filing a bogus `untracked` finding for a manifested
    skill through the mirror."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    with hooks._STATE_LOCK:
        seeded = hooks._load(ctx)
        seeded["pending"] = ["Alpha"]
        hooks._save(ctx, seeded)
    _flaky_walk_partial(monkeypatch, root)
    try:
        partial = _scan(ctx, cbs, sid="s2")
    finally:
        monkeypatch.undo()
    assert partial["scan"]["error"] == "inventory-partial"
    assert partial["scan"]["complete"] is False
    assert "alpha" not in [p.lower() for p in partial["pending"]]
    assert "alpha" not in partial["findings"]
    _assert_pending_findings_invariant(partial)


def test_94_partial_row_removal_without_finding_reports_untracked(
    tmp_path, monkeypatch
):
    """R13: a manifested clean skill loses its manifest row on a partial pass.

    Setup: `alpha` manifested, baselined, never edited (so it holds NO
    finding), then its manifest row is removed while it stays on disk.
    After a partial pass whose walk still lists every skill: an
    `untracked` finding is created (`digest=""`, `files=0`) and the name
    is pending on that same pass. A following complete pass keeps
    reporting it."""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    adopted = _scan(ctx, cbs)
    assert adopted["findings"] == {}
    assert adopted["pending"] == []
    _manifest(root, [])
    _flaky_walk_partial(monkeypatch, root)
    try:
        partial = _scan(ctx, cbs, sid="s2")
    finally:
        monkeypatch.undo()
    assert partial["scan"]["error"] == "inventory-partial"
    assert partial["scan"]["complete"] is False
    finding = partial["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "untracked"
    assert finding["digest"] == ""
    assert finding["files"] == 0
    assert isinstance(finding["first_seen_ts"], float)
    assert finding["first_seen_ts"] > 0.0
    assert "alpha" in [p.lower() for p in partial["pending"]]
    _assert_pending_findings_invariant(partial)
    # A following complete pass keeps reporting the skill as untracked.
    complete = _scan(ctx, cbs, sid="s3")
    assert complete["scan"].get("error") != "inventory-partial"
    assert complete["findings"].get("alpha", {}).get("reason") == "untracked"
    assert "alpha" in [p.lower() for p in complete["pending"]]
    _assert_pending_findings_invariant(complete)


def test_95_partial_pass_keeps_originless_changed_finding(tmp_path, monkeypatch):
    """R14 T1: a partial pass must not convert an unreviewable change.

    Setup: `alpha` manifested, baselined, edited (so it holds a `changed`
    finding); its fingerprint is then reduced to the intermediate-build
    shape (`ack == ""`, `origin == ""`), which can never reconstruct a
    dropped finding. Its manifest row is removed while it stays on disk.
    After a partial pass whose walk still lists every skill: the finding
    is still `changed` (not converted to `untracked`). After the row
    returns, a complete pass still reports `changed` (the stored digest
    equals the live one, so the change could never be recreated)."""
    import copy

    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    first_seen = changed["findings"]["alpha"]["first_seen_ts"]
    with hooks._STATE_LOCK:
        seeded = hooks._load(ctx)
        seeded["fingerprints"]["alpha"]["ack"] = ""
        seeded["fingerprints"]["alpha"]["origin"] = ""
        hooks._save(ctx, seeded)
    _manifest(root, [])
    _flaky_walk_partial(monkeypatch, root)
    try:
        partial = _scan(ctx, cbs, sid="s3")
    finally:
        monkeypatch.undo()
    assert partial["scan"]["error"] == "inventory-partial"
    assert partial["scan"]["complete"] is False
    finding = partial["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "changed"
    assert finding["first_seen_ts"] == first_seen
    _assert_pending_findings_invariant(partial)
    # The row returns: the unreviewed change is still reported, because
    # the stored digest already equals the live one.
    _manifest(root, ["alpha"])
    complete = _scan(ctx, cbs, sid="s4")
    assert complete["scan"].get("error") != "inventory-partial"
    assert complete["findings"].get("alpha", {}).get("reason") == "changed"
    _assert_pending_findings_invariant(complete)


def test_96_partial_pass_with_unavailable_manifest_changes_nothing(
    tmp_path, monkeypatch
):
    """R14 T2: a partial pass on an untrusted manifest releases nothing.

    Setup: `alpha` manifested, baselined, edited (so it holds a `changed`
    finding and is pending). The manifest then becomes unavailable
    (deleted here: the same trust gate as an unreadable file) while the
    walk reports errors. The partial pass must change no finding and no
    pending name, and record `scan["error"] == "manifest-unavailable"`."""
    import copy

    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    with hooks._STATE_LOCK:
        before = copy.deepcopy(hooks._load(ctx))
    (root / "UPSTREAM_MANIFEST.md").unlink()
    _flaky_walk_partial(monkeypatch, root)
    try:
        partial = _scan(ctx, cbs, sid="s3")
    finally:
        monkeypatch.undo()
    assert partial["scan"]["error"] == "manifest-unavailable"
    assert partial["scan"]["complete"] is False
    assert partial["findings"] == before["findings"]
    assert partial["pending"] == before["pending"]
    _assert_pending_findings_invariant(partial)


def test_97_quick_signature_stat_failure_changes_signature(tmp_path, monkeypatch):
    """R14 T3 (R16 V1): a stat failure inside `quick_signature` empties it.

    Setup: a skill with one file (signature recorded), then a new file is
    added whose `os.stat` fails. The signature must differ from the stored
    one — it is `""` ("could not be fully stat'd; always re-hash") — so
    the skill is re-examined instead of short-circuiting on a stale equal
    signature."""
    import fingerprint as fp

    d = tmp_path / "skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\n---\nhello\n", encoding="utf-8")
    sig_before = fp.quick_signature(d)
    assert sig_before
    extra = d / "extra.md"
    extra.write_text("new file\n", encoding="utf-8")
    real_stat = fp.os.stat

    def flaky_stat(path, *args, **kwargs):
        # Only the quick-signature stat call fails: the enumeration call
        # passes follow_symlinks=False and must keep working, otherwise
        # the skill would abort before any signature is produced.
        if kwargs.get("follow_symlinks") is False:
            return real_stat(path, *args, **kwargs)
        try:
            same = os.fspath(path) == os.fspath(extra)
        except Exception:
            same = False
        if same:
            raise OSError("denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(fp.os, "stat", flaky_stat)
    sig_after = fp.quick_signature(d)
    assert sig_after == ""
    assert sig_after != sig_before


def _flaky_quick_stat(monkeypatch, target):
    """Fail only the quick signature's stat of `target`.

    `quick_signature` calls bare `os.stat(path)` (no kwargs); the file
    enumeration passes `follow_symlinks=False` and `content_digest` goes
    through `Path.stat()` (a captured reference, unaffected by patching
    `os.stat`). Failing exactly the bare call models the transient
    failure from the brief: the quick signature comes back empty while
    the digest succeeds on the same pass.
    """
    import fingerprint as fp

    real_stat = fp.os.stat

    def flaky_stat(path, *args, **kwargs):
        if "follow_symlinks" not in kwargs:
            try:
                same = os.fspath(path) == os.fspath(target)
            except Exception:
                same = False
            if same:
                raise OSError("denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(fp.os, "stat", flaky_stat)


def test_98_fail_marked_quick_is_never_stored_or_trusted(tmp_path, monkeypatch):
    """R15 U1: a FAIL-marked quick signature is never stored or trusted.

    Setup: `alpha` manifested and baselined. A new file is added whose
    quick-signature `stat` fails while the digest succeeds on the same
    pass: the stored `quick` must be `""`, not the marker. The file is
    then edited with the quick `stat` still failing: a fresh digest is
    computed and the edit is reported (on unfixed code the identical
    marker short-circuits and the edit is missed)."""
    import fingerprint as fp

    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    extra = root / "alpha" / "extra.md"
    extra.write_text("v1\n", encoding="utf-8")
    _flaky_quick_stat(monkeypatch, extra)
    try:
        added = _scan(ctx, cbs, sid="s2")
    finally:
        monkeypatch.undo()
    assert added["findings"].get("alpha", {}).get("reason") == "changed"
    with hooks._STATE_LOCK:
        stored = hooks._load(ctx)
    assert stored["fingerprints"]["alpha"]["quick"] == ""
    first_digest = stored["findings"]["alpha"]["digest"]
    assert first_digest
    _assert_pending_findings_invariant(stored)
    # Edit with the quick stat still failing: the change must be seen.
    extra.write_text("v2\n", encoding="utf-8")
    _flaky_quick_stat(monkeypatch, extra)
    try:
        edited = _scan(ctx, cbs, sid="s3")
    finally:
        monkeypatch.undo()
    finding = edited["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "changed"
    assert finding["digest"] != first_digest
    live = fp.content_digest(root / "alpha")
    assert live["digest"] and live["digest"] == finding["digest"]
    _assert_pending_findings_invariant(edited)


def test_99_partial_pass_track_changes_off_missing_manifest(tmp_path, monkeypatch):
    """R15 U2(a): with `track_changes=false` releases still need trust.

    Setup: `alpha` manifested, `beta` an unmanifested arrival
    (`untracked` finding, pending), all scanned with
    `track_changes=false`. Then a new arrival `gamma` appears on disk,
    the manifest is deleted, and the walk reports errors. The partial
    pass must release nothing, arrive nothing, change no finding, and
    record `scan["error"] == "manifest-unavailable"`. (On unfixed code
    the stale empty `scan.error` lets the arrival land in pending and
    findings.)"""
    import copy

    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    ctx.config["track_changes"] = False
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    _skill(root, "beta")
    arrived = _scan(ctx, cbs, sid="s1b")
    assert arrived["findings"].get("beta", {}).get("reason") == "untracked"
    assert "beta" in [p.lower() for p in arrived["pending"]]
    with hooks._STATE_LOCK:
        before = copy.deepcopy(hooks._load(ctx))
    _skill(root, "gamma")
    (root / "UPSTREAM_MANIFEST.md").unlink()
    _flaky_walk_partial(monkeypatch, root)
    try:
        partial = _scan(ctx, cbs, sid="s2")
    finally:
        monkeypatch.undo()
    assert partial["scan"]["error"] == "manifest-unavailable"
    assert partial["scan"]["complete"] is False
    assert partial["findings"] == before["findings"]
    assert partial["pending"] == before["pending"]
    assert "gamma" not in [p.lower() for p in partial["pending"]]
    assert "gamma" not in partial["findings"]
    _assert_pending_findings_invariant(partial)


def test_100_partial_pass_fingerprint_unavailable_changes_nothing(
    tmp_path, monkeypatch
):
    """R15 U2(b): `fingerprint-unavailable` skips the release block too.

    Setup: `alpha` manifested, `beta` an unmanifested arrival
    (`untracked` finding, pending). Then a new arrival `gamma` appears,
    the walk reports errors, and `fingerprint` is simulated unavailable.
    The partial pass must release nothing, arrive nothing, change no
    finding, and record `scan["error"] == "fingerprint-unavailable"`.
    (On unfixed code that error is not in the R14 gate list, so the
    arrival lands in pending and findings.)"""
    import copy

    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    _skill(root, "beta")
    arrived = _scan(ctx, cbs, sid="s1b")
    assert arrived["findings"].get("beta", {}).get("reason") == "untracked"
    assert "beta" in [p.lower() for p in arrived["pending"]]
    with hooks._STATE_LOCK:
        before = copy.deepcopy(hooks._load(ctx))
    _skill(root, "gamma")
    _flaky_walk_partial(monkeypatch, root)
    monkeypatch.setattr(hooks, "fingerprint", None)
    try:
        partial = _scan(ctx, cbs, sid="s2")
    finally:
        monkeypatch.undo()
    assert partial["scan"]["error"] == "fingerprint-unavailable"
    assert partial["scan"]["complete"] is False
    assert partial["findings"] == before["findings"]
    assert partial["pending"] == before["pending"]
    assert "gamma" not in [p.lower() for p in partial["pending"]]
    assert "gamma" not in partial["findings"]
    _assert_pending_findings_invariant(partial)


def test_101_partial_pass_untrusted_manifest_still_unpends_dismissed(
    tmp_path, monkeypatch
):
    """R15 U2(c): a dropped dismissed finding still leaves `pending`.

    Setup: `alpha` manifested, baselined, edited (so it holds a
    `changed` finding and is pending). Then `alpha` is dismissed and
    the manifest becomes unavailable while the walk reports errors. The
    trust gate blocks the release block, but `_derive_pending` still
    runs: `alpha` has no finding and is not pending on that same pass.
    (On unfixed code the derive never runs, so the stale name stays
    pending with no finding behind it.)"""
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    assert "alpha" in [p.lower() for p in changed["pending"]]
    ctx.config["dismissed"] = ["alpha"]
    (root / "UPSTREAM_MANIFEST.md").unlink()
    _flaky_walk_partial(monkeypatch, root)
    try:
        partial = _scan(ctx, cbs, sid="s3")
    finally:
        monkeypatch.undo()
    assert partial["scan"]["error"] == "manifest-unavailable"
    assert partial["scan"]["complete"] is False
    assert "alpha" not in partial["findings"]
    assert "alpha" not in [p.lower() for p in partial["pending"]]
    _assert_pending_findings_invariant(partial)


def test_102_quick_signature_single_traversal_empty_on_stat_failure(
    tmp_path, monkeypatch
):
    """R16 V1: one traversal decides both facts.

    Setup: a skill with two files. A stat failure on either file makes
    `quick_signature` return `""` (never the hex hash of a FAIL row),
    while the healthy tree keeps a stable non-empty signature (T3 still
    holds: empty never equals a stored non-empty signature, so the
    digest is recomputed). No second walk exists: the helper that ran
    one (`quick_signature_has_fail`) is gone.
    """
    import fingerprint as fp

    assert not hasattr(fp, "quick_signature_has_fail")
    d = tmp_path / "skill"
    d.mkdir()
    (d / "SKILL.md").write_text("---\n---\nhello\n", encoding="utf-8")
    (d / "extra.md").write_text("more\n", encoding="utf-8")
    healthy = fp.quick_signature(d)
    assert healthy
    assert fp.quick_signature(d) == healthy
    real_stat = fp.os.stat

    def flaky_stat(path, *args, **kwargs):
        if kwargs.get("follow_symlinks") is False:
            return real_stat(path, *args, **kwargs)
        try:
            same = os.fspath(path) == os.fspath(d / "extra.md")
        except Exception:
            same = False
        if same:
            raise OSError("denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(fp.os, "stat", flaky_stat)
    assert fp.quick_signature(d) == ""
    # The failure is transient: a later healthy pass hashes again.
    monkeypatch.undo()
    assert fp.quick_signature(d) == healthy


def test_103_healthy_skill_short_circuits_without_digest(tmp_path):
    """R16 V1: a healthy skill still short-circuits (no digest recompute).

    Setup: `alpha` manifested and baselined via full scans. The second
    scan performs zero content digests and reports nothing: an empty
    current signature never occurs here, so the matching non-empty
    signature takes the short circuit.
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    first = _scan(ctx, cbs)
    assert first["findings"] == {}
    assert first["fingerprints"]["alpha"]["quick"] != ""
    calls = {"n": 0}
    real = hooks.fingerprint.content_digest

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    hooks.fingerprint.content_digest = counting
    try:
        second = _scan(ctx, cbs, sid="s2")
    finally:
        hooks.fingerprint.content_digest = real
    assert calls["n"] == 0
    assert second["findings"] == {}
    _assert_pending_findings_invariant(second)


def test_104_partial_gate_trusts_loaded_doc_without_reread(
    tmp_path, monkeypatch
):
    """R16 V2: the partial gate uses the already-loaded doc, no reload.

    Setup: `alpha` manifested and baselined, `gamma` a new unmanifested
    arrival on disk, `track_changes=false` (so the partial-branch gate
    is the only trust decision). The manifest file vanishes between the
    `_probe` doc load and the gate. The doc says the manifest is fine,
    so that is the decision for this pass: the arrival is pended (on
    unfixed code the gate re-read the missing file, recorded
    `manifest-unavailable`, and blocked the arrival).
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    ctx.config["track_changes"] = False
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    _skill(root, "gamma")
    manifest_path = root / "UPSTREAM_MANIFEST.md"
    real_load = manifest_db.load_manifest

    def load_then_vanish(path):
        doc = real_load(path)
        if Path(path) == manifest_path:
            manifest_path.unlink()
        return doc

    monkeypatch.setattr(manifest_db, "load_manifest", load_then_vanish)
    _flaky_walk_partial(monkeypatch, root)
    partial = _scan(ctx, cbs, sid="s2")
    assert "gamma" in [p.lower() for p in partial["pending"]]
    assert partial["findings"].get("gamma", {}).get("reason") == "untracked"
    _assert_pending_findings_invariant(partial)


def test_105_prose_manifest_is_parse_error_and_preserves_state(tmp_path):
    """R17 W1: manifest overwritten with prose (no Skill table) + complete inventory.

    Setup: `alpha`+`beta` manifested and baselined, `alpha` edited (so it
    holds a `changed` finding). The manifest is then replaced with ordinary
    prose. The complete pass must report `manifest-parse-error` (not trust
    the error-free rowless document), keep the `changed` finding and every
    fingerprint, and leave `last_ok_ts` unchanged (on unfixed code the pass
    was trusted: the finding was dropped, the library filed `untracked`,
    and `last_ok_ts` advanced).
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _skill(root, "beta")
    _manifest(root, ["alpha", "beta"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    ok_ts = changed["scan"]["last_ok_ts"]
    assert ok_ts > 0
    digest_before = changed["fingerprints"]["alpha"]["digest"]
    (root / "UPSTREAM_MANIFEST.md").write_text(
        "# Notes\nNothing here.\n", encoding="utf-8"
    )
    doc = manifest_db.load_manifest(root / "UPSTREAM_MANIFEST.md")
    assert doc.skills == set()
    assert doc.errors == []
    assert doc.has_table is False
    after = _scan(ctx, cbs, sid="s3")
    assert after["scan"]["error"] == "manifest-parse-error"
    assert after["scan"]["complete"] is False
    assert after["findings"].get("alpha", {}).get("reason") == "changed"
    assert after["fingerprints"]["alpha"]["digest"] == digest_before
    assert set(after["fingerprints"]) == {"alpha", "beta"}
    assert after["scan"]["last_ok_ts"] == ok_ts
    _assert_pending_findings_invariant(after)


def test_106_header_only_skill_table_is_trusted_removal(tmp_path):
    """R17 W1: manifest reduced to a header-only Skill table stays trusted.

    The header row was seen (`has_table`), so the rowless document is a
    legitimate removal: no error, and the skills report `untracked` while
    their fingerprints survive (same shape as `test_45`, now via the
    table-presence distinction).
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    ack_before = changed["fingerprints"]["alpha"]["ack"]
    (root / "UPSTREAM_MANIFEST.md").write_text(
        "| Skill | Source |\n| --- | --- |\n", encoding="utf-8"
    )
    doc = manifest_db.load_manifest(root / "UPSTREAM_MANIFEST.md")
    assert doc.skills == set()
    assert doc.errors == []
    assert doc.has_table is True
    cleared = _scan(ctx, cbs, sid="s3")
    assert cleared["scan"]["error"] == ""
    assert cleared["scan"]["complete"] is True
    finding = cleared["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "untracked"
    assert cleared["fingerprints"]["alpha"]["ack"] == ack_before
    assert cleared["pending"] == ["alpha"]
    _assert_pending_findings_invariant(cleared)


def test_107_zero_byte_manifest_is_trusted_empty(tmp_path):
    """R17 W1: manifest truncated to 0 bytes stays trusted (legitimate empty).

    No table header was seen, but the loaded file itself is empty, so the
    rowless document is trusted: no error, and the skill reports
    `untracked` while its fingerprint survives.
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    digest_before = changed["fingerprints"]["alpha"]["digest"]
    (root / "UPSTREAM_MANIFEST.md").write_bytes(b"")
    doc = manifest_db.load_manifest(root / "UPSTREAM_MANIFEST.md")
    assert doc.skills == set()
    assert doc.errors == []
    assert doc.has_table is False
    emptied = _scan(ctx, cbs, sid="s3")
    assert emptied["scan"]["error"] == ""
    assert emptied["scan"]["complete"] is True
    finding = emptied["findings"].get("alpha")
    assert finding is not None and finding["reason"] == "untracked"
    assert emptied["fingerprints"]["alpha"]["digest"] == digest_before
    _assert_pending_findings_invariant(emptied)


def test_108_partial_gate_trusts_header_only_doc_without_reread(
    tmp_path, monkeypatch
):
    """R17 W1 (R16 V2 still holds for the new branch): the partial gate uses
    the already-loaded document, never the file.

    Setup: `alpha` manifested and baselined, `gamma` a new unmanifested
    arrival on disk, `track_changes=false` (so the partial-branch gate is
    the only trust decision). The manifest holds a header-only Skill table
    (trusted via `has_table`) and vanishes between the `_probe` doc load
    and the gate. The loaded document decides: arrivals still pend and the
    pass records `inventory-partial` — a gate that re-read the file would
    fail closed with `manifest-unavailable`/`manifest-parse-error`.
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _manifest(root, ["alpha"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    ctx.config["track_changes"] = False
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    _skill(root, "gamma")
    (root / "UPSTREAM_MANIFEST.md").write_text(
        "| Skill | Source |\n| --- | --- |\n", encoding="utf-8"
    )
    manifest_path = root / "UPSTREAM_MANIFEST.md"
    real_load = manifest_db.load_manifest

    def load_then_vanish(path):
        doc = real_load(path)
        if Path(path) == manifest_path:
            assert doc.has_table is True
            manifest_path.unlink()
        return doc

    monkeypatch.setattr(manifest_db, "load_manifest", load_then_vanish)
    _flaky_walk_partial(monkeypatch, root)
    partial = _scan(ctx, cbs, sid="s2")
    assert partial["scan"]["error"] == "inventory-partial"
    assert "gamma" in [p.lower() for p in partial["pending"]]
    assert partial["findings"].get("gamma", {}).get("reason") == "untracked"
    assert "alpha" in [p.lower() for p in partial["pending"]]
    assert partial["findings"].get("alpha", {}).get("reason") == "untracked"
    _assert_pending_findings_invariant(partial)


def test_109_prose_truncated_after_parse_stays_parse_error(tmp_path, monkeypatch):
    """R18 X2: prose parsed, file truncated to zero before the size stat.

    The manifest holds prose (no Skill table); the file is truncated to
    zero bytes after its bytes are read but `load_manifest` still returns
    first. The document must carry the parsed byte length (not the later
    0-byte stat), so the gate still reports `manifest-parse-error` and the
    complete pass preserves the `changed` finding, every fingerprint, and
    `last_ok_ts` (on unfixed code the document reports `size == 0`, the
    pass is trusted, the finding is dropped, and `last_ok_ts` advances).
    """
    root = tmp_path / "skills"
    _skill(root, "alpha")
    _skill(root, "beta")
    _manifest(root, ["alpha", "beta"])
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(root)
    cbs = _wire(ctx)
    _scan(ctx, cbs)
    (root / "alpha" / "SKILL.md").write_text("---\n---\nEDITED\n", encoding="utf-8")
    changed = _scan(ctx, cbs, sid="s2")
    assert changed["findings"].get("alpha", {}).get("reason") == "changed"
    ok_ts = changed["scan"]["last_ok_ts"]
    assert ok_ts > 0
    digest_before = changed["fingerprints"]["alpha"]["digest"]
    prose = b"# Notes\nNothing here.\n"
    manifest_path = root / "UPSTREAM_MANIFEST.md"
    manifest_path.write_bytes(prose)
    real_read_bytes = Path.read_bytes
    real_read_text = Path.read_text

    def read_then_truncate_bytes(self, *a, **k):
        if Path(self) == manifest_path:
            # Every parse sees the prose bytes while the file itself is
            # zero: the truncation lands after the read, before any later
            # stat, for every load in the pass.
            manifest_path.write_bytes(b"")
            return prose
        return real_read_bytes(self, *a, **k)

    def read_then_truncate_text(self, *a, **k):
        if Path(self) == manifest_path:
            manifest_path.write_bytes(b"")
            return prose.decode("utf-8")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(Path, "read_bytes", read_then_truncate_bytes)
    monkeypatch.setattr(Path, "read_text", read_then_truncate_text)
    doc = manifest_db.load_manifest(manifest_path)
    assert doc.skills == set()
    assert doc.errors == []
    assert doc.has_table is False
    assert doc.size == len(prose)
    after = _scan(ctx, cbs, sid="s3")
    assert after["scan"]["error"] == "manifest-parse-error"
    assert after["scan"]["complete"] is False
    assert after["findings"].get("alpha", {}).get("reason") == "changed"
    assert after["fingerprints"]["alpha"]["digest"] == digest_before
    assert set(after["fingerprints"]) == {"alpha", "beta"}
    assert after["scan"]["last_ok_ts"] == ok_ts
    _assert_pending_findings_invariant(after)
