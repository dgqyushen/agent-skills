"""Option 2 ensure-cron and once-per-session advisory. No real cron."""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import sys
import threading
import time
from pathlib import Path

import pytest

import hooks

_LINE = "Advisory only. Do not act on this unless the user asks."


class FakeState:
    def __init__(self):
        self._d = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class FakeCtx:
    def __init__(self):
        self.config = {}
        self.state = FakeState()

    def get_config(self, key, default=None):
        return self.config.get(key, default)


class _FakeCron:
    """Stand-in for cron.load_jobs + cronjob. Host-adapter tests never touch the real store."""

    def __init__(self, jobs=None):
        self.jobs = [dict(job) for job in (jobs or [])]
        self.creates = []
        self.loads = 0

    def load_jobs(self):
        self.loads += 1
        return [dict(job) for job in self.jobs]

    def cronjob(self, **kwargs):
        self.creates.append(kwargs)
        job_id = f"job-{len(self.creates)}"
        self.jobs.append({"id": job_id, "name": kwargs.get("name")})
        return json.dumps({"success": True, "job_id": job_id})


@pytest.fixture(autouse=True)
def _reset_hooks():
    hooks._reset_runtime()
    hooks._session_signatures.clear()
    hooks._sessions_loaded = False
    yield
    hooks._reset_runtime()
    hooks._session_signatures.clear()
    hooks._sessions_loaded = False


def _patch_cron(monkeypatch, cron: _FakeCron) -> None:
    monkeypatch.setattr(hooks, "_import_cron", lambda: (cron.load_jobs, cron.cronjob))


def _arm_pending(ctx: FakeCtx, pending: list[str]) -> None:
    ctx.state.set(
        "v1",
        {
            "baseline": {},
            "pending": list(pending),
            "dismissed": [],
            "first_sweep_done": True,
            "manifest_missing_prompted": True,
            "lint_summary": {},
        },
    )
    hooks.configure(ctx)
    hooks._probed = True


def test_ensure_cron_creates_once(monkeypatch):
    """First ensure() creates the unpinned job. The second sees it and does not create."""
    cron = _FakeCron()
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    hooks.ensure_cron(ctx)
    assert len(cron.creates) == 1
    created = cron.creates[0]
    assert created["action"] == "create"
    assert created["name"] == "Skill-manifest drift clearance"
    assert created["schedule"] == "0 9 * * *"
    assert created["deliver"] == "telegram"
    assert created["prompt"] == hooks.PROMPT_AUTO_RESOLVE
    assert "pinned" not in created
    assert "assigned task" in created["prompt"]
    assert "CLEAN" in created["prompt"]
    assert "at most 6" in created["prompt"]
    assert "Never install, delete, or publish" in created["prompt"]
    assert ctx.state.get("v1")["auto_resolve_job_id"] == "job-1"
    assert ctx.state.get("v1")["auto_resolve_user_removed"] is False


def test_ensure_cron_auto_resolve_false(monkeypatch):
    """auto_resolve false never attempts create."""
    cron = _FakeCron()
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    ctx.config["auto_resolve"] = False
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    assert cron.creates == []
    assert cron.loads == 0


def test_ensure_cron_user_removed(monkeypatch):
    """A stored id and no same-name job, with a non-empty list, is a user removal.

    An EMPTY listing is unknown (fresh store ambiguity) and must not flag —
    covered by test_unknown_empty_list_does_not_flag_or_create.""",
    fake = _FakeCron(jobs=[{"id": "unrelated", "name": "Other job"}])
    _patch_cron(monkeypatch, fake)
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "baseline": {},
            "pending": ["keep"],
            "dismissed": [],
            "first_sweep_done": True,
            "manifest_missing_prompted": False,
            "lint_summary": {},
            "auto_resolve_job_id": "job-removed",
            "auto_resolve_user_removed": False,
        },
    )
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    hooks.ensure_cron(ctx)
    assert fake.creates == []
    saved = ctx.state.get("v1")
    assert saved["auto_resolve_user_removed"] is True
    assert saved["pending"] == ["keep"]
    # The retry lists again so a hand-recreated same-name job can be adopted.
    assert fake.loads == 2


def test_ensure_cron_disabled(monkeypatch):
    """disabled short-circuits ensure before any cron call."""
    cron = _FakeCron()
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    ctx.config["disabled"] = True
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    assert cron.creates == []
    assert cron.loads == 0
    assert ctx.state.get("v1") is None


def test_ensure_cron_empty_deliver_omits_field(monkeypatch):
    """An empty auto_resolve_deliver omits deliver. An existing name is not duplicated."""
    cron = _FakeCron([{"id": "already", "name": "Skill-manifest drift clearance"}])
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    ctx.config["auto_resolve_deliver"] = ""
    ctx.config["auto_resolve_schedule"] = "15 8 * * 1"
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    assert cron.creates == []
    assert ctx.state.get("v1")["auto_resolve_job_id"] == "already"

    cron.jobs.clear()
    ctx.state.set("v1", {})
    hooks.ensure_cron(ctx)
    assert len(cron.creates) == 1
    assert "deliver" not in cron.creates[0]
    assert cron.creates[0]["schedule"] == "15 8 * * 1"
    assert "pinned" not in cron.creates[0]


def test_per_session_injection():
    """Same session and signature injects once. A new name or a new session injects."""
    ctx = FakeCtx()
    _arm_pending(ctx, ["alpha"])
    first = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    second = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert first is not None
    assert "alpha" in first["context"]
    assert second is None
    _arm_pending(ctx, ["alpha", "beta"])
    changed = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert changed is not None
    assert "beta" in changed["context"]
    other = hooks.on_pre_llm_call(session_id="s2", user_message="hi", is_first_turn=False)
    assert other is not None
    assert "beta" in other["context"]
    assert hooks.on_pre_llm_call(session_id="s2", user_message="hi", is_first_turn=False) is None


def test_session_memory_evicts_oldest():
    """The session map keeps _SESSION_CAP entries and drops the oldest insertion."""
    ctx = FakeCtx()
    _arm_pending(ctx, ["alpha"])
    for i in range(hooks._SESSION_CAP):
        out = hooks.on_pre_llm_call(
            session_id=f"s{i}", user_message="hi", is_first_turn=False
        )
        assert out is not None
    assert len(hooks._session_signatures) == hooks._SESSION_CAP
    assert hooks.on_pre_llm_call(session_id="s0", user_message="hi", is_first_turn=False) is None
    fresh = hooks.on_pre_llm_call(session_id="s-new", user_message="hi", is_first_turn=False)
    assert fresh is not None
    assert "s0" not in hooks._session_signatures
    assert len(hooks._session_signatures) == hooks._SESSION_CAP
    assert hooks.on_pre_llm_call(session_id="s0", user_message="hi", is_first_turn=False) is not None


def test_advisory_wording():
    """Both advisory builders end with the do-not-act line."""
    advisory = hooks._advisory(["alpha"], "UPSTREAM_MANIFEST.md", 6, {})
    missing = hooks._missing_text("UPSTREAM_MANIFEST.md")
    assert advisory.splitlines()[-1] == _LINE
    assert missing.splitlines()[-1] == _LINE


def test_other_session_start_does_not_reinject(tmp_path):
    """Session A stays quiet after session B starts."""
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(tmp_path / "no-skills")
    _arm_pending(ctx, ["alpha"])
    first = hooks.on_pre_llm_call(session_id="sA", user_message="hi", is_first_turn=False)
    assert first is not None
    assert hooks.on_pre_llm_call(session_id="sA", user_message="hi", is_first_turn=False) is None
    hooks.on_session_start(session_id="sB")
    assert hooks.on_pre_llm_call(session_id="sA", user_message="hi", is_first_turn=False) is None


def test_ensure_cron_flock_serializes_create(tmp_path, monkeypatch):
    """Two overlapping ensure calls create one job. The lock file is real."""
    cron = _FakeCron()
    real = cron.cronjob

    def slow(**kwargs):
        time.sleep(0.3)
        return real(**kwargs)

    cron.cronjob = slow
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(tmp_path / "skills")
    hooks.configure(ctx)
    errors = []

    def run():
        try:
            hooks.ensure_cron(ctx)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert errors == []
    assert len(cron.creates) == 1
    lock = tmp_path / ".skills-locks" / "skill-manifest-ensure-cron.lock"
    assert lock.is_file()
    assert lock.stat().st_mode & 0o777 == 0o644


def test_adopt_same_name_over_stale_id(monkeypatch):
    """A live same-name job replaces a stale stored id and does not set the removed flag."""
    cron = _FakeCron([{"id": "live-id", "name": "Skill-manifest drift clearance"}])
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "baseline": {},
            "pending": [],
            "dismissed": [],
            "first_sweep_done": True,
            "manifest_missing_prompted": False,
            "lint_summary": {},
            "auto_resolve_job_id": "stale-id",
            "auto_resolve_user_removed": False,
        },
    )
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    saved = ctx.state.get("v1")
    assert saved["auto_resolve_job_id"] == "live-id"
    assert saved["auto_resolve_user_removed"] is False
    assert cron.creates == []


def test_unknown_empty_list_does_not_flag_or_create(monkeypatch):
    """A stored id plus an empty listing is unknown. Skip, with no flag and no create."""
    cron = _FakeCron()
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "baseline": {},
            "pending": [],
            "dismissed": [],
            "first_sweep_done": True,
            "manifest_missing_prompted": False,
            "lint_summary": {},
            "auto_resolve_job_id": "job-stored",
            "auto_resolve_user_removed": False,
        },
    )
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    saved = ctx.state.get("v1")
    assert cron.creates == []
    assert saved["auto_resolve_user_removed"] is False
    assert saved["auto_resolve_job_id"] == "job-stored"


def test_user_removed_when_other_jobs_remain(monkeypatch):
    """A non-empty list with no id match and no name match is a user removal."""
    cron = _FakeCron([{"id": "other", "name": "backup"}])
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "baseline": {},
            "pending": [],
            "dismissed": [],
            "first_sweep_done": True,
            "manifest_missing_prompted": False,
            "lint_summary": {},
            "auto_resolve_job_id": "job-gone",
            "auto_resolve_user_removed": False,
        },
    )
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    saved = ctx.state.get("v1")
    assert cron.creates == []
    assert saved["auto_resolve_user_removed"] is True


def test_cron_and_subagent_platforms_skip_advisory():
    """Cron and subagent platforms get no advisory, and the session is still unadvised."""
    ctx = FakeCtx()
    _arm_pending(ctx, ["alpha"])
    assert (
        hooks.on_pre_llm_call(
            session_id="s", user_message="hi", is_first_turn=False, platform="cron"
        )
        is None
    )
    assert (
        hooks.on_pre_llm_call(
            session_id="s", user_message="hi", is_first_turn=False, platform="subagent"
        )
        is None
    )
    out = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert out is not None
    assert "alpha" in out["context"]


def test_combined_advisory_has_one_suffix():
    """TX-3. Pending plus missing text ends with the advisory line, once."""
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "baseline": {},
            "pending": ["alpha"],
            "dismissed": [],
            "first_sweep_done": True,
            "manifest_missing_prompted": False,
            "lint_summary": {"manifest_missing": True, "errors": 0, "findings": 0},
        },
    )
    hooks.configure(ctx)
    hooks._probed = True
    out = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert out is not None
    assert out["context"].count("Advisory only.") == 1
    assert out["context"].splitlines()[-1] == _LINE
    assert "alpha" in out["context"]
    assert "is missing" in out["context"]


def test_shrink_does_not_reinject():
    """Dropping a pending name updates memory and does not inject (I7).

    A pure untracked shrink leaves every surviving row unchanged, so it
    stays silent; the session entry still advances to the shrunk pending
    set (with its per-name rows).
    """
    ctx = FakeCtx()
    _arm_pending(ctx, ["a", "b"])
    first = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert first is not None
    _arm_pending(ctx, ["a"])
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    entry = ctx.state.get("v1")["advisory_sessions"]["s"]
    assert set(entry) == {"pending", "missing", "ts", "key", "rows"}
    assert entry["pending"] == ["a"]
    assert entry["missing"] is False
    assert isinstance(entry["ts"], float)
    # J4: session rows store sha256 hex digests, not full row strings.
    assert entry["rows"] == {
        "a": hashlib.sha256(b"a|untracked|").hexdigest()[:8]
    }


def test_session_memory_survives_reset_and_reload():
    """Persisted pending sets survive _reset_runtime and a cleared in-process cache."""
    ctx = FakeCtx()
    _arm_pending(ctx, ["alpha"])
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is not None
    hooks._reset_runtime()
    hooks._probed = True
    assert "s" in hooks._session_signatures
    hooks._session_signatures.clear()
    hooks._sessions_loaded = False
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    entry = ctx.state.get("v1")["advisory_sessions"]["s"]
    assert set(entry) == {"pending", "missing", "ts", "key", "rows"}
    assert entry["pending"] == ["alpha"]
    assert entry["missing"] is False
    assert isinstance(entry["ts"], float)


def test_autouse_stub_blocks_real_cron_create(tmp_path, monkeypatch):
    """Session start with auto_resolve on does not call the host cronjob."""
    calls = []
    try:
        import tools.cronjob_tools as cron_tools
    except ImportError:
        cron_tools = None
    if cron_tools is not None:
        def spy(**kwargs):
            calls.append(kwargs)
            return json.dumps({"success": False})

        monkeypatch.setattr(cron_tools, "cronjob", spy)
    ctx = FakeCtx()
    ctx.config["auto_resolve"] = True
    ctx.config["skills_root"] = str(tmp_path / "no-skills")
    hooks.configure(ctx)
    assert hooks.on_session_start(session_id="s") is None
    assert calls == []
    assert hooks._import_cron() is None


@pytest.mark.parametrize("value,creates", [(0, 0), (1, 1), (3, 1)])
def test_auto_resolve_int_coercion(monkeypatch, value, creates):
    """Int 0 disables ensure-cron. Any other int enables it."""
    cron = _FakeCron()
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    ctx.config["auto_resolve"] = value
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    assert len(cron.creates) == creates


def test_tx1_merge_on_write_keeps_external_session():
    """An external session written after cache load survives the next save."""
    ctx = FakeCtx()
    _arm_pending(ctx, ["alpha"])
    assert hooks.on_pre_llm_call(session_id="A", user_message="hi", is_first_turn=False) is not None
    saved = ctx.state.get("v1")
    ts_a = saved["advisory_sessions"]["A"]["ts"]
    saved["advisory_sessions"]["B"] = {"pending": ["beta"], "missing": False}
    ctx.state.set("v1", saved)
    assert hooks.on_pre_llm_call(session_id="C", user_message="hi", is_first_turn=False) is not None
    sessions = ctx.state.get("v1")["advisory_sessions"]
    assert set(sessions) == {"A", "B", "C"}
    assert sessions["A"]["pending"] == ["alpha"]
    assert sessions["A"]["ts"] == ts_a
    assert sessions["B"]["pending"] == ["beta"]
    assert sessions["B"]["missing"] is False
    assert sessions["B"]["ts"] == 0.0
    assert sessions["C"]["pending"] == ["alpha"]
    assert set(hooks._session_signatures) == {"A", "B", "C"}


def test_tx2_missing_flag_flip():
    """false→true injects. true→false is silent. Missing plus additions inject once."""
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "baseline": {},
            "pending": ["alpha"],
            "dismissed": [],
            "first_sweep_done": True,
            "manifest_missing_prompted": False,
            "lint_summary": {},
        },
    )
    hooks.configure(ctx)
    hooks._probed = True
    first = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert first is not None
    assert "is missing" not in first["context"]
    assert ctx.state.get("v1")["manifest_missing_prompted"] is False

    state = ctx.state.get("v1")
    state["lint_summary"] = {"manifest_missing": True}
    risen = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert risen is not None
    assert "is missing" in risen["context"]
    assert ctx.state.get("v1")["manifest_missing_prompted"] is True
    assert ctx.state.get("v1")["advisory_sessions"]["s"]["missing"] is True
    assert ctx.state.get("v1")["advisory_sessions"]["s"]["pending"] == ["alpha"]

    state = ctx.state.get("v1")
    state["lint_summary"] = {}
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    assert ctx.state.get("v1")["advisory_sessions"]["s"]["missing"] is False
    assert ctx.state.get("v1")["advisory_sessions"]["s"]["pending"] == ["alpha"]

    hooks._session_signatures.clear()
    hooks._sessions_loaded = False
    ctx.state.set(
        "v1",
        {
            "baseline": {},
            "pending": ["alpha"],
            "dismissed": [],
            "first_sweep_done": True,
            "manifest_missing_prompted": False,
            "lint_summary": {},
        },
    )
    hooks.configure(ctx)
    hooks._probed = True
    assert hooks.on_pre_llm_call(session_id="s2", user_message="hi", is_first_turn=False) is not None
    state = ctx.state.get("v1")
    state["pending"] = ["alpha", "beta"]
    state["lint_summary"] = {"manifest_missing": True}
    both = hooks.on_pre_llm_call(session_id="s2", user_message="hi", is_first_turn=False)
    assert both is not None
    assert "beta" in both["context"]
    assert "is missing" in both["context"]
    assert both["context"].count("Advisory only.") == 1
    assert hooks.on_pre_llm_call(session_id="s2", user_message="hi", is_first_turn=False) is None


def test_tx4_lock_path_follows_skills_root(tmp_path, monkeypatch):
    """The lock sits next to the skills root and does not touch HERMES_HOME."""
    hermes = tmp_path / "hermes-home"
    hermes.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    skills = tmp_path / "skills-root"
    skills.mkdir()
    cron = _FakeCron()
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(skills)
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    lock = tmp_path / ".skills-locks" / "skill-manifest-ensure-cron.lock"
    assert lock.is_file()
    assert lock.stat().st_mode & 0o777 == 0o644
    assert list(hermes.rglob("*")) == []


def test_tx5_lock_import_error_still_creates(tmp_path, monkeypatch, caplog):
    """Lock setup ImportError warns and still creates the job."""

    def broken(_fd):
        raise ImportError("fcntl")

    monkeypatch.setattr(hooks, "_acquire_flock", broken)
    cron = _FakeCron()
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(tmp_path / "skills")
    hooks.configure(ctx)
    with caplog.at_level(logging.WARNING, logger="hooks"):
        hooks.ensure_cron(ctx)
    assert len(cron.creates) == 1
    assert "cross-process locking unavailable" in caplog.text


def test_tx6_adopt_same_name_after_user_removed(tmp_path, monkeypatch):
    """A hand-recreated same-name job clears the removed flag and does not create."""
    cron = _FakeCron([{"id": "hand-made", "name": "Skill-manifest drift clearance"}])
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    ctx.config["skills_root"] = str(tmp_path / "skills")
    ctx.state.set(
        "v1",
        {
            "baseline": {},
            "pending": ["keep"],
            "dismissed": [],
            "first_sweep_done": True,
            "manifest_missing_prompted": False,
            "lint_summary": {},
            "auto_resolve_job_id": "old-id",
            "auto_resolve_user_removed": True,
        },
    )
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    saved = ctx.state.get("v1")
    assert cron.creates == []
    assert saved["auto_resolve_job_id"] == "hand-made"
    assert saved["auto_resolve_user_removed"] is False
    assert saved["pending"] == ["keep"]


def test_newer_timestamp_beats_larger_pending():
    """A stale larger cache must not replace a newer smaller persisted set."""
    ctx = FakeCtx()
    _arm_pending(ctx, ["alpha", "beta"])
    assert hooks.on_pre_llm_call(session_id="B", user_message="hi", is_first_turn=False) is not None
    saved = ctx.state.get("v1")
    saved["advisory_sessions"]["B"] = {"pending": ["alpha"], "missing": False, "ts": 200.0}
    ctx.state.set("v1", saved)
    hooks._session_signatures["B"] = {
        "pending": ["alpha", "beta"],
        "missing": False,
        "ts": 100.0,
    }
    assert hooks.on_pre_llm_call(session_id="C", user_message="hi", is_first_turn=False) is not None
    entry = ctx.state.get("v1")["advisory_sessions"]["B"]
    assert entry["pending"] == ["alpha"]
    assert entry["missing"] is False
    assert entry["ts"] == 200.0


def test_timestamp_tie_keeps_persisted():
    """Equal timestamps keep the persisted entry, even when the cache set is larger."""
    ctx = FakeCtx()
    _arm_pending(ctx, ["alpha", "beta"])
    assert hooks.on_pre_llm_call(session_id="B", user_message="hi", is_first_turn=False) is not None
    saved = ctx.state.get("v1")
    saved["advisory_sessions"]["B"] = {"pending": ["alpha"], "missing": False, "ts": 50.0}
    ctx.state.set("v1", saved)
    hooks._session_signatures["B"] = {
        "pending": ["alpha", "beta"],
        "missing": False,
        "ts": 50.0,
    }
    assert hooks.on_pre_llm_call(session_id="C", user_message="hi", is_first_turn=False) is not None
    entry = ctx.state.get("v1")["advisory_sessions"]["B"]
    assert entry["pending"] == ["alpha"]
    assert entry["ts"] == 50.0


def test_legacy_list_ts_zero_loses_to_newer():
    """A legacy name list coerces to ts=0 and loses to a newer cached entry."""
    ctx = FakeCtx()
    _arm_pending(ctx, ["alpha"])
    assert hooks.on_pre_llm_call(session_id="A", user_message="hi", is_first_turn=False) is not None
    saved = ctx.state.get("v1")
    saved["advisory_sessions"]["B"] = ["alpha", "beta"]
    ctx.state.set("v1", saved)
    hooks._session_signatures["B"] = {"pending": ["alpha"], "missing": False, "ts": 10.0}
    assert hooks.on_pre_llm_call(session_id="C", user_message="hi", is_first_turn=False) is not None
    entry = ctx.state.get("v1")["advisory_sessions"]["B"]
    assert entry["pending"] == ["alpha"]
    assert entry["missing"] is False
    assert entry["ts"] == 10.0


def test_eviction_drops_oldest_timestamp(monkeypatch):
    """The cap drops the oldest ts, including a later insertion with an older stamp."""
    monkeypatch.setattr(hooks, "_SESSION_CAP", 2)
    ctx = FakeCtx()
    _arm_pending(ctx, ["alpha"])
    assert hooks.on_pre_llm_call(session_id="early", user_message="hi", is_first_turn=False) is not None
    assert hooks.on_pre_llm_call(session_id="late", user_message="hi", is_first_turn=False) is not None
    saved = ctx.state.get("v1")
    saved["advisory_sessions"]["early"]["ts"] = 50.0
    saved["advisory_sessions"]["late"]["ts"] = 1.0
    ctx.state.set("v1", saved)
    hooks._session_signatures["early"]["ts"] = 50.0
    hooks._session_signatures["late"]["ts"] = 1.0
    fresh = hooks.on_pre_llm_call(session_id="new", user_message="hi", is_first_turn=False)
    assert fresh is not None
    sessions = ctx.state.get("v1")["advisory_sessions"]
    assert set(sessions) == {"early", "new"}
    assert "late" not in hooks._session_signatures


def test_timestamp_updates_only_when_content_changes(monkeypatch):
    """Saving another session keeps an unchanged stamp. A pure pending shrink stays silent (I7) but refreshes it."""
    clock = {"now": 100.0}
    monkeypatch.setattr(hooks.time, "time", lambda: clock["now"])
    ctx = FakeCtx()
    _arm_pending(ctx, ["a", "b"])
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is not None
    assert ctx.state.get("v1")["advisory_sessions"]["s"]["ts"] == 100.0
    clock["now"] = 400.0
    assert hooks.on_pre_llm_call(session_id="other", user_message="hi", is_first_turn=False) is not None
    sessions = ctx.state.get("v1")["advisory_sessions"]
    assert sessions["s"]["ts"] == 100.0
    assert sessions["other"]["ts"] == 400.0
    clock["now"] = 700.0
    state = ctx.state.get("v1")
    state["pending"] = ["a"]
    ctx.state.set("v1", state)
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    assert hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False) is None
    entry = ctx.state.get("v1")["advisory_sessions"]["s"]
    assert entry["pending"] == ["a"]
    assert entry["missing"] is False
    assert entry["ts"] == 700.0
    assert ctx.state.get("v1")["advisory_sessions"]["other"]["ts"] == 400.0


def test_truncated_missing_sentence_stays_due():
    """A 240-character filename cuts the missing sentence, so the next turn tries again."""
    ctx = FakeCtx()
    ctx.config["manifest_filename"] = "M" * 240
    ctx.state.set(
        "v1",
        {
            "baseline": {},
            "pending": ["alpha"],
            "dismissed": [],
            "first_sweep_done": True,
            "manifest_missing_prompted": False,
            "lint_summary": {"manifest_missing": True, "errors": 0, "findings": 0},
        },
    )
    hooks.configure(ctx)
    hooks._probed = True
    first = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert first is not None
    assert "alpha" in first["context"]
    assert "is missing" not in first["context"]
    saved = ctx.state.get("v1")
    assert saved["manifest_missing_prompted"] is False
    entry = saved["advisory_sessions"]["s"]
    assert entry["pending"] == ["alpha"]
    assert entry["missing"] is False
    second = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert second is not None
    assert "alpha" in second["context"]
    assert "is missing" not in second["context"]
    assert ctx.state.get("v1")["advisory_sessions"]["s"]["missing"] is False
    assert ctx.state.get("v1")["manifest_missing_prompted"] is False


def test_default_config_ensure_cron_leaves_hermes_home_empty(tmp_path, monkeypatch):
    """Default config writes the lock beside the tmp skills root, not under HERMES_HOME."""
    hermes = tmp_path / "hermes-home"
    hermes.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    cron = _FakeCron()
    _patch_cron(monkeypatch, cron)
    ctx = FakeCtx()
    hooks.configure(ctx)
    hooks.ensure_cron(ctx)
    assert list(hermes.rglob("*")) == []
    lock = tmp_path / ".skills-locks" / "skill-manifest-ensure-cron.lock"
    assert lock.is_file()
    assert len(cron.creates) == 1


def _load_peer_hooks():
    """A second hooks module. Its process cache is not the test module's cache."""
    import importlib.util

    path = Path(hooks.__file__).resolve()
    name = "skill_manifest_hooks_tz_peer"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _reset_instance(module) -> None:
    module._reset_runtime()
    module._session_signatures.clear()
    module._sessions_loaded = False
    module._probed = True


def _shared_contexts():
    state = FakeState()
    first = FakeCtx()
    second = FakeCtx()
    first.state = state
    second.state = state
    return first, second


def _run_shared_session(peer, clock, final_pending):
    """Instance A records session B, peer B shrinks it, then A runs once more."""
    _reset_instance(hooks)
    _reset_instance(peer)
    ctx_a, ctx_b = _shared_contexts()
    _arm_pending(ctx_a, ["alpha", "beta"])
    peer.configure(ctx_b)
    clock["now"] = 100.0
    assert hooks.on_pre_llm_call(session_id="B", user_message="hi", is_first_turn=False) is not None
    saved = ctx_a.state.get("v1")
    saved["pending"] = ["alpha"]
    ctx_a.state.set("v1", saved)
    clock["now"] = 200.0
    # I7: a pure shrink stays silent (surviving rows unchanged) while the
    # entry still advances to the shrunk set with a refreshed stamp.
    assert peer.on_pre_llm_call(session_id="B", user_message="hi", is_first_turn=False) is None
    assert peer.on_pre_llm_call(session_id="B", user_message="hi", is_first_turn=False) is None
    shrunk = ctx_a.state.get("v1")["advisory_sessions"]["B"]
    assert shrunk["pending"] == ["alpha"]
    assert shrunk["ts"] == 200.0
    saved = ctx_a.state.get("v1")
    saved["pending"] = list(final_pending)
    ctx_a.state.set("v1", saved)
    return hooks.on_pre_llm_call(session_id="B", user_message="hi", is_first_turn=False), ctx_a


def test_tz1_multi_instance_decision(monkeypatch):
    """Two hook instances share state. A must follow B's newer persisted entry."""
    peer = _load_peer_hooks()
    assert peer is not hooks
    assert peer._session_signatures is not hooks._session_signatures
    clock = {"now": 100.0}
    monkeypatch.setattr(hooks.time, "time", lambda: clock["now"])
    try:
        quiet, ctx = _run_shared_session(peer, clock, ["alpha"])
        assert quiet is None
        assert ctx.state.get("v1")["advisory_sessions"]["B"]["pending"] == ["alpha"]
        # Continuous-sequence variant (Codex r6 test-gap note): the shrink and
        # the later growth run against the SAME persisted session B entry
        # without reset, so a reappearing name is a true new-advisory case.
        grown, ctx = _run_shared_session(peer, clock, ["alpha", "beta", "gamma"])
        assert grown is not None
        assert "gamma" in grown["context"]
        pruned, ctx = _run_shared_session(peer, clock, ["alpha"])
        assert pruned is None
        back, ctx = _run_shared_session(peer, clock, ["gamma", "alpha"])
        assert back is not None
        assert "gamma" in back["context"]
    finally:
        sys.modules.pop("skill_manifest_hooks_tz_peer", None)


def test_tz2_tie_keeps_persisted_entry_in_the_decision():
    """Equal timestamps keep the persisted entry, so a cache-only name still injects."""
    ctx = FakeCtx()
    _arm_pending(ctx, ["alpha", "beta"])
    hooks._sessions_loaded = True
    saved = ctx.state.get("v1")
    saved["advisory_sessions"] = {
        "s": {"pending": ["alpha"], "missing": False, "ts": 50.0},
    }
    ctx.state.set("v1", saved)
    hooks._session_signatures["s"] = {
        "pending": ["alpha", "beta"],
        "missing": False,
        "ts": 50.0,
    }
    out = hooks.on_pre_llm_call(session_id="s", user_message="hi", is_first_turn=False)
    assert out is not None
    assert "beta" in out["context"]
    assert ctx.state.get("v1")["advisory_sessions"]["s"]["pending"] == ["alpha", "beta"]


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _compares_two_entry_timestamps(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    calls = [_call_name(operand) for operand in (node.left, *node.comparators)]
    return calls.count("_entry_ts") >= 2


def test_tz3_one_ts_comparison_path():
    """The newer-wins timestamp check lives in one function, called by decision and merge."""
    source = Path(hooks.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    comparers = [
        fn.name
        for fn in functions
        if any(_compares_two_entry_timestamps(node) for node in ast.walk(fn))
    ]
    assert len(comparers) == 1
    winner = comparers[0]
    assert winner not in {"on_pre_llm_call", "_merge_advisory_sessions"}
    by_name = {fn.name: fn for fn in functions}
    assert any(_call_name(node) == winner for node in ast.walk(by_name["on_pre_llm_call"]))
    assert any(
        _call_name(node) == winner for node in ast.walk(by_name["_merge_advisory_sessions"])
    )
