"""Phase 2 hook behavior. Each test names the hazard it locks."""
from __future__ import annotations

from helpers import _writable_tmp_base

import hashlib
import importlib.util
import logging
import os
import pickle
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import types
from pathlib import Path

import pytest


import hooks
import manifest_db
import manifest_lint
import probe

PLUGIN = Path(__file__).resolve().parent.parent
STATE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
HOOKS = {"on_skill_lifecycle", "on_session_start", "pre_llm_call"}


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
        self.state = FakeState()

    def get_config(self, key, default=None):
        return self.config.get(key, default)

    def set_config(self, key, value):
        self.config[key] = value

    def register_hook(self, hook_name, callback):
        self.hooks.append((hook_name, callback))
        return callback


def _temp():
    class _T:
        def __enter__(self):
            self.t = tempfile.TemporaryDirectory(dir=_writable_tmp_base())
            self.p = Path(self.t.name)
            return self.p

        def __exit__(self, *a):
            self.t.cleanup()
            return False

    return _T()


def _skill(root: Path, name: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\n", encoding="utf-8")


def _manifest(root: Path, names: list[str], extra: str = "") -> None:
    rows = "\n".join(f"| {n} | src |" for n in names)
    text = f"| Skill | Source |\n| --- | --- |\n{rows}\n{extra}"
    (root / "UPSTREAM_MANIFEST.md").write_text(text, encoding="utf-8")


def _plain_init():
    sys.path.insert(0, str(PLUGIN))
    name = "skill_manifest_plain_init_tests"
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
    return {event: cb for event, cb in ctx.hooks}


def _pending(ctx: FakeCtx) -> list[str]:
    raw = ctx.state.get("v1")
    if not isinstance(raw, dict):
        return []
    return list(raw.get("pending") or [])


def _low(names) -> set[str]:
    return {n.lower() for n in names}


@pytest.fixture(autouse=True)
def _reset_hooks():
    hooks._reset_runtime()
    hooks._session_signatures.clear()
    hooks._sessions_loaded = False
    yield
    hooks._reset_runtime()
    hooks._session_signatures.clear()
    hooks._sessions_loaded = False


def test_package_exec_register_without_plugin_on_sys_path():
    """Gateway loader execs __init__ as a package with the plugin dir off sys.path."""
    saved_path = sys.path[:]
    saved_modules = {
        key: sys.modules.pop(key, None)
        for key in ("hooks", "manifest_db", "manifest_lint", "probe")
    }
    parent = sys.modules.get("hermes_plugins")
    name = "hermes_plugins.skill_manifest_lf"
    plugin = str(PLUGIN.resolve())
    cwd = str(Path.cwd().resolve())
    try:
        kept = []
        for entry in saved_path:
            if entry in ("", "."):
                if cwd == plugin or cwd.startswith(plugin + os.sep):
                    continue
            else:
                try:
                    resolved = str(Path(entry).resolve())
                except OSError:
                    kept.append(entry)
                    continue
                if resolved == plugin or resolved.startswith(plugin + os.sep):
                    continue
            kept.append(entry)
        sys.path[:] = kept
        pkg = types.ModuleType("hermes_plugins")
        pkg.__path__ = []
        pkg.__package__ = "hermes_plugins"
        sys.modules["hermes_plugins"] = pkg
        spec = importlib.util.spec_from_file_location(
            name,
            PLUGIN / "__init__.py",
            submodule_search_locations=[str(PLUGIN)],
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__path__ = [str(PLUGIN)]
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        ctx = FakeCtx()
        path_before = list(sys.path)
        mdb_before = {
            key
            for key in sys.modules
            if key == "manifest_db" or key.endswith(".manifest_db")
        }
        mod.register(ctx)
        assert list(sys.path) == path_before
        mdb_after = {
            key
            for key in sys.modules
            if key == "manifest_db" or key.endswith(".manifest_db")
        }
        assert mdb_after - mdb_before == {f"{name}.manifest_db"}
        assert "manifest_db" not in sys.modules or "manifest_db" in mdb_before
        pkg_mdb = sys.modules[f"{name}.manifest_db"]
        pkg_hooks = sys.modules[f"{name}.hooks"]
        assert pkg_hooks.manifest_db is pkg_mdb
        assert sys.modules[f"{name}.shared.scripts.manifest_lint"].manifest_db is pkg_mdb
        assert sys.modules[f"{name}.shared.scripts.probe"].manifest_db is pkg_mdb
        events = {event for event, _cb in ctx.hooks}
        assert events == HOOKS
        for _event, cb in ctx.hooks:
            assert cb.__module__ == f"{name}.hooks"
    finally:
        sys.path[:] = saved_path
        for key, mod in saved_modules.items():
            sys.modules.pop(key, None)
            if mod is not None:
                sys.modules[key] = mod
        if parent is None:
            sys.modules.pop("hermes_plugins", None)
        else:
            sys.modules["hermes_plugins"] = parent
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key, None)


def test_plain_module_register_uses_sys_path_fallback():
    """HERMES's own suite loads __init__ as a plain module; register must still wire hooks."""
    mod = _plain_init()
    ctx = FakeCtx()
    mod.register(ctx)
    assert {event for event, _cb in ctx.hooks} == HOOKS
    cb = next(cb for event, cb in ctx.hooks if event == "on_skill_lifecycle")
    assert cb.__module__ == "hooks"


def test_created_manifested_does_not_enqueue():
    """A created skill that already has a manifest row must not enter pending."""
    with _temp() as root:
        _skill(root, "alpha")
        _manifest(root, ["alpha"])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        cbs["on_skill_lifecycle"](action="created", skill_name="alpha")
        assert _low(_pending(ctx)) == set()


def test_installed_unmanifested_enqueues():
    """An installed skill with no manifest row is the arrival the advisory exists for."""
    with _temp() as root:
        _skill(root, "beta")
        _manifest(root, ["other"])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        cbs["on_skill_lifecycle"](action="installed", skill_name="beta")
        assert _low(_pending(ctx)) == {"beta"}


def test_observer_dedups_pending():
    """A second created event for the same skill must not double the nag list."""
    with _temp() as root:
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        root.mkdir(exist_ok=True)
        cbs = _wire(ctx)
        cbs["on_skill_lifecycle"](action="created", skill_name="beta")
        cbs["on_skill_lifecycle"](action="created", skill_name="Beta")
        assert _pending(ctx) == ["beta"]


def test_unhandled_actions_do_not_resolve():
    """patched/loaded/archived must return before any skill-dir resolution."""
    with _temp() as root:
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        ctx.state.set("v1", {"pending": ["keep"], "dismissed": [], "lint_summary": {}})

        def explode(*_a, **_k):
            ctx.state.set("v1", {"pending": ["should-not"], "dismissed": [], "lint_summary": {}})
            raise RuntimeError("resolved")

        orig = hooks._resolve_skill_status
        hooks._resolve_skill_status = explode
        try:
            for action in ("", "patched", "edited", "loaded", "archived", "stale", "restored"):
                cbs["on_skill_lifecycle"](action=action, skill_name="beta")
        finally:
            hooks._resolve_skill_status = orig
        assert _pending(ctx) == ["keep"]


def test_plugin_namespace_names_ignored():
    """plugin:skill names are catalog-tracked, not manifest-tracked."""
    with _temp() as root:
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)

        def explode(*_a, **_k):
            raise RuntimeError("resolved")

        orig = hooks._resolve_skill_status
        hooks._resolve_skill_status = explode
        try:
            cbs["on_skill_lifecycle"](action="created", skill_name="vendor:demo")
        finally:
            hooks._resolve_skill_status = orig
        assert _pending(ctx) == []


def test_observer_exception_does_not_propagate(caplog):
    """The observer runs inside skill_manage's mutation path and must not raise."""
    ctx = FakeCtx()
    cbs = _wire(ctx)
    orig = hooks._resolve_skill_status
    hooks._resolve_skill_status = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        with caplog.at_level(logging.WARNING):
            cbs["on_skill_lifecycle"](action="created", skill_name="whatever")
            cbs["on_skill_lifecycle"](action="created", skill_name="again")
    finally:
        hooks._resolve_skill_status = orig
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1


def test_session_probe_detects_manual_arrival():
    """The first sweep seeds the corpus. A later copy is the arrival that pends."""
    with _temp() as root:
        _skill(root, "tracked")
        _manifest(root, ["tracked"])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert state["pending"] == []
        assert state["first_sweep_done"] is True
        assert "last_turn_injected" not in state
        assert set(state) >= {
            "baseline",
            "pending",
            "dismissed",
            "first_sweep_done",
            "manifest_missing_prompted",
            "lint_summary",
        }
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["tracked"] == "manifest"
        _skill(root, "copied-in")
        cbs["on_session_start"](session_id="s2")
        assert _low(_pending(ctx)) == {"copied-in"}


def test_session_probe_dedups_and_sees_later_copy():
    """Seeding does not pend the corpus. A later copy pends once."""
    with _temp() as root:
        _skill(root, "copied-in")
        _manifest(root, [])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        cbs["on_session_start"](session_id="s")
        assert _pending(ctx) == []
        base = {k.lower(): v for k, v in ctx.state.get("v1")["baseline"].items()}
        assert base["copied-in"] == "untracked-snapshot"
        _skill(root, "later")
        cbs["on_session_start"](session_id="s")
        assert _low(_pending(ctx)) == {"later"}
        cbs["on_session_start"](session_id="s")
        assert _pending(ctx).count("later") == 1


def test_session_probe_missing_root_preserves_pending():
    """An empty sweep of a missing skills root must not clear pending."""
    with _temp() as root:
        missing = root / "no-such-skills"
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(missing)
        ctx.state.set(
            "v1",
            {
                "baseline": {"old": "manifest"},
                "pending": ["keep-me"],
                "dismissed": [],
                "manifest_missing_prompted": False,
                "first_sweep_done": True,
                "lint_summary": {},
            },
        )
        cbs = _wire(ctx)
        first = cbs["pre_llm_call"](
            session_id="s", user_message="hi", is_first_turn=False, turn_id="turn-9"
        )
        assert first["context"].startswith("⚠️ skill-manifest")
        assert "last_turn_injected" not in ctx.state.get("v1")
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert state["pending"] == ["keep-me"]
        assert state["baseline"] == {"old": "manifest"}
        assert "last_turn_injected" not in state
        again = cbs["pre_llm_call"](
            session_id="s", user_message="hi", is_first_turn=False, turn_id="turn-9"
        )
        # F1: session start no longer clears the signature map, so the
        # unchanged signature does not re-inject (B1 fix, not a regression).
        assert again is None


def test_session_probe_removes_vanished_names():
    """Names that left the disk leave baseline and pending."""
    with _temp() as root:
        _skill(root, "still-here")
        _manifest(root, ["still-here"])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.state.set(
            "v1",
            {
                "baseline": {"gone-base": "manifest", "still-here": "manifest"},
                "pending": ["gone-pending", "still-here"],
                "dismissed": [],
                "manifest_missing_prompted": False,
                "first_sweep_done": True,
                "lint_summary": {},
            },
        )
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert _low(state["pending"]) == set()
        assert "gone-base" not in {k.lower() for k in state["baseline"]}
        assert "still-here" in {k.lower() for k in state["baseline"]}


def test_session_probe_resolves_manifested_and_skips_dismissed():
    """A manifest row clears pending; a dismissed original skill stays quiet."""
    with _temp() as root:
        _skill(root, "now-row")
        _skill(root, "original")
        _skill(root, "fresh")
        _manifest(root, ["now-row"])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.state.set(
            "v1",
            {
                "baseline": {},
                "pending": ["now-row"],
                "dismissed": ["original"],
                "manifest_missing_prompted": False,
                "first_sweep_done": True,
                "lint_summary": {},
            },
        )
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert _low(state["pending"]) == {"fresh"}
        assert state["dismissed"] == ["original"]
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["now-row"] == "manifest"
        assert base["original"] == "dismissed"


def test_session_start_exception_preserves_pending(monkeypatch):
    """A probe crash must not propagate and must not wipe pending."""
    with _temp() as root:
        _skill(root, "present")
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.state.set(
            "v1",
            {
                "baseline": {},
                "pending": ["keep"],
                "dismissed": [],
                "manifest_missing_prompted": False,
                "last_turn_injected": None,
                "lint_summary": {},
            },
        )
        cbs = _wire(ctx)

        def boom(_root):
            raise RuntimeError("disk")

        monkeypatch.setattr(probe, "list_skill_names_report", boom)
        cbs["on_session_start"](session_id="s")
        assert _pending(ctx) == ["keep"]


def test_session_start_stashes_lint_counts_by_check_id():
    """The advisor reads lint counts the session probe stored, including errors."""
    with _temp() as root:
        _skill(root, "loose")
        _manifest(root, ["kept"], extra="| --- | --- |\n")
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.state.set(
            "v1",
            {
                "baseline": {},
                "pending": [],
                "dismissed": [],
                "first_sweep_done": True,
                "manifest_missing_prompted": True,
                "lint_summary": {},
            },
        )
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        summary = ctx.state.get("v1")["lint_summary"]
        assert summary["counts"]["L1-structure"] >= 1
        assert summary["errors"] >= 1
        assert summary["findings"] >= summary["errors"]
        out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
        assert "loose" in out["context"]
        assert re.search(
            r"Manifest has \d+ lint findings \(\d+ errors\) — run the manifest lint\.",
            out["context"],
        )


def test_pre_llm_injects_exact_advisory_and_none_when_idle():
    """Pending names produce the bounded advisory; an empty list produces None."""
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "pending": ["alpha", "beta"],
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": True,
        },
    )
    cbs = _wire(ctx)
    hooks._probed = True
    assert cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)[
        "context"
    ] == (
        "⚠️ skill-manifest: 2 skills missing from UPSTREAM_MANIFEST.md: "
        "alpha, beta. Add a manifest row to UPSTREAM_MANIFEST.md, or add "
        "the skill to the skill-manifest plugin's dismissed config list.\n"
        "Advisory only. Do not act on this unless the user asks."
    )
    ctx.state.set(
        "v1",
        {
            "pending": [],
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": True,
        },
    )
    assert cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False) is None


def test_pre_llm_skips_first_turn():
    """Turn 1 stays clean even when skills are already pending."""
    ctx = FakeCtx()
    ctx.state.set("v1", {"pending": ["alpha"], "dismissed": [], "lint_summary": {}})
    cbs = _wire(ctx)
    hooks._probed = True
    assert cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=True) is None
    out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
    assert out["context"].startswith("⚠️ skill-manifest")
    assert "alpha" in out["context"]
    assert len(out["context"]) <= 600


def test_pre_llm_truncates_at_max_reported():
    """Names past max_reported collapse to a +N more suffix."""
    ctx = FakeCtx()
    ctx.config["max_reported"] = 2
    names = ["name-one", "name-two", "name-three", "name-four"]
    ctx.state.set("v1", {"pending": names, "dismissed": [], "lint_summary": {}})
    cbs = _wire(ctx)
    hooks._probed = True
    out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
    assert out["context"] == (
        "⚠️ skill-manifest: 4 skills missing from UPSTREAM_MANIFEST.md: "
        "name-one, name-two, +2 more. Add a manifest row to UPSTREAM_MANIFEST.md, "
        "or add the skill to the skill-manifest plugin's dismissed config list.\n"
        "Advisory only. Do not act on this unless the user asks."
    )
    assert "name-three" not in out["context"]
    assert "name-four" not in out["context"]


def test_pre_llm_hard_caps_context_length():
    """A long pending name cannot push the advisory to 900 characters."""
    ctx = FakeCtx()
    ctx.config["max_reported"] = 6
    ctx.state.set(
        "v1",
        {
            "pending": ["n" * 400 for _ in range(6)],
            "dismissed": [],
            "lint_summary": {"findings": 3, "errors": 2, "counts": {"L1-structure": 2}},
        },
    )
    cbs = _wire(ctx)
    hooks._probed = True
    out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
    assert out["context"].startswith("⚠️ skill-manifest")
    assert len(out["context"]) < 900


def test_pre_llm_injects_once_per_session():
    """The same session signature must not inject twice; change re-injects."""
    ctx = FakeCtx()
    ctx.state.set("v1", {"pending": ["alpha"], "dismissed": [], "lint_summary": {}})
    cbs = _wire(ctx)
    hooks._probed = True
    first = cbs["pre_llm_call"](
        session_id="s", user_message="hi", is_first_turn=False, turn_id="t2"
    )
    second = cbs["pre_llm_call"](
        session_id="s", user_message="hi", is_first_turn=False, turn_id="t3"
    )
    ctx.state.set(
        "v1", {"pending": ["alpha", "beta"], "dismissed": [], "lint_summary": {}}
    )
    third = cbs["pre_llm_call"](
        session_id="s", user_message="hi", is_first_turn=False, turn_id="t4"
    )
    other = cbs["pre_llm_call"](
        session_id="s2", user_message="hi", is_first_turn=False, turn_id="t5"
    )
    assert first["context"].startswith("⚠️ skill-manifest")
    assert second is None
    assert third["context"].startswith("⚠️ skill-manifest")
    assert other["context"].startswith("⚠️ skill-manifest")
    assert _pending(ctx) == ["alpha", "beta"]


def test_disabled_performs_no_io(monkeypatch):
    """disabled short-circuits every hook before inventory, manifest, or lint I/O."""
    hits = []
    monkeypatch.setattr(
        probe, "list_skill_names", lambda root: hits.append(("list", root)) or set()
    )
    monkeypatch.setattr(
        manifest_db,
        "load_manifest",
        lambda path: hits.append(("load", path)) or manifest_db.ManifestDoc(),
    )
    monkeypatch.setattr(
        manifest_lint,
        "lint_manifest",
        lambda *a, **k: hits.append("lint") or manifest_lint.LintReport(),
    )
    real_walk = os.walk

    def _walk(*a, **k):
        hits.append("walk")
        return real_walk(*a, **k)

    monkeypatch.setattr(os, "walk", _walk)
    ctx = FakeCtx()
    ctx.config["disabled"] = True
    ctx.state.set("v1", {"pending": ["x"], "dismissed": [], "lint_summary": {}})
    cbs = _wire(ctx)
    assert cbs["on_skill_lifecycle"](action="created", skill_name="y") is None
    assert cbs["on_session_start"](session_id="s") is None
    assert cbs["pre_llm_call"](session_id="s", user_message="m", is_first_turn=False) is None
    assert hits == []


def test_manifest_missing_prompts_once():
    """A missing manifest file nags once, including after turn 1, then stops."""
    with _temp() as root:
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        assert ctx.state.get("v1")["lint_summary"]["manifest_missing"] is True
        assert (
            cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=True) is None
        )
        out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
        assert out["context"] == (
            "⚠️ skill-manifest: UPSTREAM_MANIFEST.md is missing. "
            "Create it from the skill-maintainer upstream-manifest template.\n"
            "Advisory only. Do not act on this unless the user asks."
        )
        assert ctx.state.get("v1")["manifest_missing_prompted"] is True
        assert (
            cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False) is None
        )


def test_lazy_probe_on_pre_llm_when_state_uninitialized():
    """Skipping session start still sweeps once, and the existing corpus does not pend."""
    with _temp() as root:
        _skill(root, "dropped")
        _manifest(root, ["other"])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
        assert out is None
        state = ctx.state.get("v1")
        assert state["pending"] == []
        assert state["first_sweep_done"] is True
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["dropped"] == "untracked-snapshot"


def test_state_key_v1_is_pluginstate_safe(monkeypatch):
    """Writes use the regex-safe key v1 and round-trip through PluginState."""
    pytest.importorskip("hermes_cli")  # host-adapter test: needs the installed Hermes host
    with _temp() as root:
        skills = root / "skills"
        skills.mkdir()
        _skill(skills, "new-one")
        monkeypatch.setenv("HERMES_HOME", str(root))
        monkeypatch.syspath_prepend("/usr/local/lib/hermes-agent")
        from hermes_cli.plugins_state import PluginState

        st = PluginState("skill-manifest-p2-test")
        ctx = FakeCtx()
        ctx.state = st
        ctx.config["skills_root"] = str(skills)
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        saved = st.get("v1")
        assert saved["pending"] == []
        assert saved["first_sweep_done"] is True
        assert "last_turn_injected" not in saved
        base = {k.lower(): v for k, v in saved["baseline"].items()}
        assert base["new-one"] == "untracked-snapshot"
        import json

        disk = json.loads(st.path.read_text(encoding="utf-8"))
        assert set(disk) == {"v1"}
        assert "last_turn_injected" not in disk["v1"]
        assert STATE_RE.fullmatch("v1")


def test_skills_root_fallback_chain(monkeypatch):
    """Empty skills_root uses hermes home, then HERMES_HOME, then ~/.hermes."""
    pytest.importorskip("hermes_constants")  # host-adapter test: needs the installed Hermes host
    import builtins

    original_import = builtins.__import__

    def blocked(name, *a, **k):
        if name == "hermes_constants":
            raise ImportError("blocked")
        return original_import(name, *a, **k)

    with _temp() as root:
        via_const = root / "via-const" / "skills"
        via_const.mkdir(parents=True)
        _skill(via_const, "from-const")
        _manifest(via_const, [])
        monkeypatch.setattr(
            "hermes_constants.get_hermes_home", lambda: root / "via-const"
        )
        ctx = FakeCtx()
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert state["pending"] == []
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["from-const"] == "untracked-snapshot"

    with _temp() as root:
        via_env = root / "skills"
        via_env.mkdir()
        _skill(via_env, "from-env")
        _manifest(via_env, [])
        monkeypatch.setattr(builtins, "__import__", blocked)
        monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(root))
        ctx = FakeCtx()
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert state["pending"] == []
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["from-env"] == "untracked-snapshot"

    with _temp() as home:
        skills = home / ".hermes" / "skills"
        skills.mkdir(parents=True)
        _skill(skills, "from-home")
        _manifest(skills, [])
        monkeypatch.setattr(builtins, "__import__", blocked)
        monkeypatch.delitem(sys.modules, "hermes_constants", raising=False)
        monkeypatch.delenv("HERMES_HOME", raising=False)
        monkeypatch.setenv("HOME", str(home))
        ctx = FakeCtx()
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert state["pending"] == []
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["from-home"] == "untracked-snapshot"


def test_plugin_yaml_declares_hooks_and_config():
    """plugin.yaml mirrors the hook list and declares the config keys."""
    import yaml

    raw = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
    y = yaml.safe_load(raw)
    assert y["name"] == "skill-manifest"
    assert y["version"] == "0.3.0"
    assert set(y["provides_hooks"]) == HOOKS
    assert not y.get("provides_tools")
    assert not y.get("requires_env")
    assert not y.get("python_dependencies")
    schema = y["config_schema"]
    assert schema["manifest_filename"]["default"] == "UPSTREAM_MANIFEST.md"
    assert schema["skills_root"]["default"] == ""
    assert schema["disabled"]["default"] is False
    assert schema["max_reported"]["default"] == 6
    assert schema["dismissed"]["type"] == "list"
    assert schema["dismissed"]["default"] == []
    assert schema["dismissed"]["description"] == (
        "Skill names treated as originals (no manifest row needed)."
    )


_ACTION = (
    "Add a manifest row to UPSTREAM_MANIFEST.md, or add "
    "the skill to the skill-manifest plugin's dismissed config list."
)


def test_plain_scripts_import_without_conftest_injection():
    """Plain `import manifest_lint` works when the caller puts the paths on sys.path."""
    code = r"""
import sys
from pathlib import Path
plugin = Path(sys.argv[1]).resolve()
scripts = plugin / "shared" / "scripts"
sys.path = [p for p in sys.path if plugin.as_posix() not in p and str(plugin) not in p]
sys.path.insert(0, str(scripts))
sys.path.insert(0, str(plugin))
before = list(sys.path)
import manifest_db
import probe
import manifest_lint
assert list(sys.path) == before, (before, sys.path)
text = "| Skill | Source |\n| --- | --- |\n| alpha | src |\n"
report = manifest_lint.lint_manifest(text, on_disk={"alpha"})
assert report.ok
assert manifest_lint.manifest_db is manifest_db
assert probe.manifest_db is manifest_db
"""
    subprocess.check_call([sys.executable, "-c", code, str(PLUGIN)], cwd="/tmp")


def test_resumed_session_probes_once_without_session_start(monkeypatch):
    """Continuation never fires on_session_start, but the first turn still sweeps once."""
    with _temp() as root:
        _skill(root, "seeded")
        _skill(root, "arrived")
        _manifest(root, [])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.state.set(
            "v1",
            {
                "baseline": {"seeded": "untracked-snapshot"},
                "pending": [],
                "dismissed": [],
                "first_sweep_done": True,
                "manifest_missing_prompted": True,
                "lint_summary": {},
            },
        )
        calls = {"n": 0}
        real = probe.list_skill_names_report

        def counting(path):
            calls["n"] += 1
            return real(path)

        monkeypatch.setattr(probe, "list_skill_names_report", counting)
        cbs = _wire(ctx)
        out = cbs["pre_llm_call"](
            session_id="s", user_message="hi", is_first_turn=False, turn_id="t2"
        )
        assert "arrived" in out["context"]
        assert "seeded" not in out["context"]
        assert calls["n"] == 1
        cbs["pre_llm_call"](
            session_id="s", user_message="hi", is_first_turn=False, turn_id="t3"
        )
        assert calls["n"] == 1


def test_failed_state_save_does_not_reprobe_every_turn(monkeypatch):
    """A quota failure on state.set must not walk the tree again on the next turn."""
    calls = {"n": 0}

    def counting(_root):
        calls["n"] += 1
        return ({"alpha"}, [])

    monkeypatch.setattr(probe, "list_skill_names_report", counting)

    class Boom(FakeState):
        def set(self, key, value):
            raise OSError("quota")

    with _temp() as root:
        ctx = FakeCtx()
        ctx.state = Boom()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        assert (
            cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
            is None
        )
        assert (
            cbs["pre_llm_call"](
                session_id="s", user_message="hi", is_first_turn=False, turn_id="t2"
            )
            is None
        )
        assert calls["n"] == 1


def test_first_sweep_of_three_untracked_skills_pends_nothing():
    """The pre-existing corpus is snapshotted. Nothing is pending."""
    with _temp() as root:
        for name in ("one", "two", "three"):
            _skill(root, name)
        _manifest(root, ["one"])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.state.set(
            "v1",
            {
                "baseline": {},
                "pending": [],
                "dismissed": ["two"],
                "manifest_missing_prompted": False,
                "lint_summary": {},
            },
        )
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert state["pending"] == []
        assert state["first_sweep_done"] is True
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base == {
            "one": "manifest",
            "two": "dismissed",
            "three": "untracked-snapshot",
        }


def test_arrival_after_seed_pends_only_the_new_skill():
    """A directory that appears after the seed is the only pending name."""
    with _temp() as root:
        _skill(root, "one")
        _skill(root, "two")
        _skill(root, "three")
        _manifest(root, [])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        assert ctx.state.get("v1")["pending"] == []
        _skill(root, "four")
        cbs["on_session_start"](session_id="s2")
        assert _low(_pending(ctx)) == {"four"}


def test_config_dismissed_observer_skips_and_reconcile_seeds_dismissed():
    """Config dismissal is case-insensitive for the observer and for the sweep.

    The baseline mark is dismissed-config, and the name is not written into
    state.dismissed — that list is only the state path.
    """
    with _temp() as root:
        _skill(root, "original")
        _skill(root, "fresh")
        _manifest(root, [])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.config["dismissed"] = ["Original"]
        cbs = _wire(ctx)
        cbs["on_skill_lifecycle"](action="created", skill_name="original")
        assert _pending(ctx) == []
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert state["pending"] == []
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["original"] == "dismissed-config"
        assert base["fresh"] == "untracked-snapshot"
        assert "original" not in {d.lower() for d in state["dismissed"]}
        ctx.config["dismissed"] = ["Original", "Later"]
        _skill(root, "later")
        cbs["on_session_start"](session_id="s2")
        assert _pending(ctx) == []
        base = {k.lower(): v for k, v in ctx.state.get("v1")["baseline"].items()}
        assert base["later"] == "dismissed-config"
        assert "later" not in {d.lower() for d in ctx.state.get("v1")["dismissed"]}


def test_config_dismissed_unions_without_case_duplicates():
    """State 'Foo' plus config 'foo' stays one entry, and a new config name is added."""
    ctx = FakeCtx()
    ctx.config["dismissed"] = ["foo", "Bar"]
    ctx.state.set(
        "v1",
        {"baseline": {}, "pending": [], "dismissed": ["Foo"], "lint_summary": {}},
    )
    _wire(ctx)
    state = hooks._load(ctx)
    lowered = [d.lower() for d in state["dismissed"]]
    assert lowered.count("foo") == 1
    assert "bar" in lowered


def test_config_dismiss_is_read_time_and_repends_when_cleared():
    """Config dismissal must not stick after the name leaves the config list.

    A sweep while the name is config-dismissed records baseline "dismissed-config"
    and does not write that name into state.dismissed. Emptying the list on the
    next arrival sweep drops the lock: the baseline becomes "untracked-snapshot"
    and the skill pends. A name that was already in state.dismissed is kept.
    """
    with _temp() as root:
        _skill(root, "original")
        _skill(root, "keep-me")
        _manifest(root, [])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.config["dismissed"] = ["Original", "keep-me"]
        ctx.state.set(
            "v1",
            {
                "baseline": {},
                "pending": [],
                "dismissed": ["keep-me"],
                "lint_summary": {},
            },
        )
        cbs = _wire(ctx)
        cbs["on_skill_lifecycle"](action="created", skill_name="original")
        assert _pending(ctx) == []
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert state["pending"] == []
        persisted = {d.lower() for d in state["dismissed"]}
        assert "original" not in persisted
        assert "keep-me" in persisted
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["original"] == "dismissed-config"
        assert base["keep-me"] == "dismissed"

        ctx.config["dismissed"] = []
        cbs["on_session_start"](session_id="s2")
        state = ctx.state.get("v1")
        assert "original" in _low(state["pending"])
        assert "keep-me" not in _low(state["pending"])
        persisted = {d.lower() for d in state["dismissed"]}
        assert persisted == {"keep-me"}
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["original"] == "untracked-snapshot"
        assert base["keep-me"] == "dismissed"
        hooks._probed = True
        out = cbs["pre_llm_call"](session_id="s2", user_message="hi", is_first_turn=False)
        assert "original" in out["context"]
        persisted = {d.lower() for d in ctx.state.get("v1")["dismissed"]}
        assert "original" not in persisted
        assert "keep-me" in persisted


def test_config_change_between_load_and_save_does_not_persist_config_only_name(monkeypatch):
    """A config-only name captured at load must not stick if config drops it before save.

    `_config_dismissed` returns ["x"] for the load and [] for a later save.
    "x" is not a state-path dismissal, so the persisted list must not gain it.
    """
    answers = iter([["x"], []])

    def sequenced(_ctx):
        return next(answers)

    monkeypatch.setattr(hooks, "_config_dismissed", sequenced)
    ctx = FakeCtx()
    state = hooks._load(ctx)
    assert [item.lower() for item in state["dismissed"]] == ["x"]
    hooks._save(ctx, state)
    persisted = ctx.state.get("v1")
    assert "x" not in {item.lower() for item in persisted["dismissed"]}
    assert "_cfg_dismissed" not in persisted
    assert "_state_dismissed" not in persisted


def test_observer_skips_already_dismissed_name():
    """A name already in state dismissed is not appended to pending."""
    with _temp() as root:
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.state.set(
            "v1",
            {
                "baseline": {},
                "pending": [],
                "dismissed": ["Beta"],
                "lint_summary": {},
                "manifest_missing_prompted": True,
            },
        )
        cbs = _wire(ctx)
        before = list(ctx.state.keys)
        cbs["on_skill_lifecycle"](action="created", skill_name="beta")
        assert _pending(ctx) == []
        assert ctx.state.keys == before


def test_missing_manifest_flag_only_when_line_is_in_the_advisory():
    """A truncated advisory must not record the missing-manifest nag as delivered."""
    ctx = FakeCtx()
    ctx.config["manifest_filename"] = "M" * 500
    ctx.state.set(
        "v1",
        {
            "pending": ["alpha"],
            "dismissed": [],
            "lint_summary": {"manifest_missing": True, "errors": 0, "findings": 0},
            "manifest_missing_prompted": False,
            "first_sweep_done": True,
        },
    )
    cbs = _wire(ctx)
    hooks._probed = True
    out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
    assert "is missing" not in out["context"]
    assert len(out["context"]) < 900
    assert ctx.state.get("v1")["manifest_missing_prompted"] is False


def test_missing_manifest_flag_sets_when_the_line_is_appended():
    """When the missing-manifest sentence fits, the flag sticks and the next turn is quiet."""
    with _temp() as root:
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.state.set(
            "v1",
            {
                "pending": [],
                "dismissed": [],
                "baseline": {},
                "first_sweep_done": True,
                "lint_summary": {"manifest_missing": True, "errors": 0, "findings": 0},
                "manifest_missing_prompted": False,
            },
        )
        cbs = _wire(ctx)
        hooks._probed = True
        out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
        assert "is missing" in out["context"]
        assert ctx.state.get("v1")["manifest_missing_prompted"] is True
        assert (
            cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
            is None
        )


def test_bom_manifest_sweep_reports_no_lint_errors():
    """A BOM-prefixed manifest lints as cleanly as the parser reads it."""
    with _temp() as root:
        _skill(root, "alpha")
        body = "| Skill | Source |\n| --- | --- |\n| alpha | src |\n"
        (root / "UPSTREAM_MANIFEST.md").write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert state["lint_summary"]["errors"] == 0
        assert state["lint_summary"].get("manifest_missing") is not True
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["alpha"] == "manifest"
        assert state["pending"] == []


def test_lint_failure_does_not_abort_reconcile(monkeypatch):
    """Permission or a lint crash still seeds the sweep."""
    with _temp() as root:
        _skill(root, "alpha")
        _skill(root, "beta")
        _manifest(root, ["alpha"])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)

        def boom(*_a, **_k):
            raise PermissionError("denied")

        monkeypatch.setattr(hooks.manifest_lint, "lint_manifest", boom)
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert state["first_sweep_done"] is True
        assert state["pending"] == []
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["alpha"] == "manifest"
        assert base["beta"] == "untracked-snapshot"


def test_manifest_directory_does_not_abort_reconcile():
    """A directory sitting where the manifest file should be still completes the sweep."""
    with _temp() as root:
        _skill(root, "alpha")
        (root / "UPSTREAM_MANIFEST.md").mkdir()
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        assert state["first_sweep_done"] is True
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert base["alpha"] == "untracked-snapshot"
        assert state["pending"] == []


def test_zero_error_lint_summary_adds_no_lint_line():
    """Findings without errors do not append the lint sentence."""
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "pending": ["alpha"],
            "dismissed": [],
            "lint_summary": {"errors": 0, "findings": 4, "counts": {"L4-untracked": 4}},
            "manifest_missing_prompted": True,
            "first_sweep_done": True,
        },
    )
    cbs = _wire(ctx)
    hooks._probed = True
    out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
    assert "lint findings" not in out["context"]
    assert "run the manifest lint" not in out["context"]
    assert "alpha" in out["context"]


def test_advisory_singular_when_one_skill_is_pending():
    """One pending skill uses the singular noun."""
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "pending": ["alpha"],
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": True,
        },
    )
    cbs = _wire(ctx)
    hooks._probed = True
    out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
    assert "1 skill missing" in out["context"]
    assert "1 skills missing" not in out["context"]
    assert _ACTION in out["context"]


def test_advisory_hides_unusual_names_and_keeps_the_count():
    """Attacker-controlled folder names are not echoed. The count still includes them."""
    weird = "../etc/passwd"
    long_name = "a" * 65
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "pending": ["ok-name", weird, long_name, "also_ok"],
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": True,
        },
    )
    cbs = _wire(ctx)
    hooks._probed = True
    out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
    text = out["context"]
    assert "ok-name" in text
    assert "also_ok" in text
    assert weird not in text
    assert long_name not in text
    assert text.count("[unusual-name hidden]") == 2
    assert "4 skills missing" in text


def test_sixty_four_char_name_is_shown():
    """A name at the sanitizer's length limit is still shown verbatim."""
    name = "a" * 64
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "pending": [name],
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": True,
        },
    )
    cbs = _wire(ctx)
    hooks._probed = True
    out = cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
    assert name in out["context"]
    assert "[unusual-name hidden]" not in out["context"]


@pytest.mark.parametrize("bad", [0, "x"])
def test_invalid_max_reported_falls_back_to_six(bad):
    """0 and a non-numeric max_reported both show the default six names."""
    ctx = FakeCtx()
    ctx.config["max_reported"] = bad
    names = [f"skill-{i}" for i in range(8)]
    ctx.state.set(
        "v1",
        {
            "pending": names,
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": True,
        },
    )
    cbs = _wire(ctx)
    hooks._probed = True
    out = cbs["pre_llm_call"](
        session_id="s", user_message="hi", is_first_turn=False, turn_id=f"t-{bad}"
    )
    assert "skill-0" in out["context"]
    assert "skill-5" in out["context"]
    assert "skill-6" not in out["context"]
    assert "+2 more" in out["context"]


@pytest.mark.parametrize("flag", ["true", "1", "TRUE"])
def test_disabled_string_performs_no_io(monkeypatch, flag):
    """Truthy disabled strings are cached at register and skip every hook."""
    hits = []
    monkeypatch.setattr(
        probe, "list_skill_names", lambda root: hits.append("list") or set()
    )
    ctx = FakeCtx()
    ctx.config["disabled"] = flag
    calls = []
    orig = ctx.get_config

    def wrapped(key, default=None):
        calls.append(key)
        return orig(key, default)

    ctx.get_config = wrapped
    ctx.state.set("v1", {"pending": ["x"], "dismissed": [], "lint_summary": {}})
    cbs = _wire(ctx)
    calls.clear()
    assert cbs["on_skill_lifecycle"](action="created", skill_name="y") is None
    assert cbs["on_session_start"](session_id="s") is None
    assert cbs["pre_llm_call"](session_id="s", user_message="m", is_first_turn=False) is None
    assert hits == []
    assert calls == []


def test_disabled_string_false_does_not_disable():
    """The string 'false' is not a truthy disabled flag."""
    with _temp() as root:
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.config["disabled"] = "false"
        cbs = _wire(ctx)
        cbs["on_skill_lifecycle"](action="created", skill_name="beta")
        assert _low(_pending(ctx)) == {"beta"}


def test_idle_pre_llm_does_not_write_state():
    """An already-swept tree with nothing pending does not call state.set."""
    with _temp() as root:
        _skill(root, "alpha")
        _manifest(root, ["alpha"])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        assert ctx.state.get("v1")["pending"] == []
        ctx.state.keys.clear()
        assert (
            cbs["pre_llm_call"](session_id="s", user_message="hi", is_first_turn=False)
            is None
        )
        assert ctx.state.keys == []


def test_turn_id_is_coerced_and_not_persisted():
    """turn_id 2 and '2' are one turn, and the id never lands in the state document."""
    ctx = FakeCtx()
    ctx.state.set(
        "v1",
        {
            "pending": ["alpha"],
            "dismissed": [],
            "lint_summary": {},
            "manifest_missing_prompted": True,
        },
    )
    cbs = _wire(ctx)
    hooks._probed = True
    first = cbs["pre_llm_call"](
        session_id="s", user_message="hi", is_first_turn=False, turn_id=2
    )
    assert first["context"].startswith("⚠️ skill-manifest")
    assert "last_turn_injected" not in ctx.state.get("v1")
    second = cbs["pre_llm_call"](
        session_id="s", user_message="hi", is_first_turn=False, turn_id="2"
    )
    assert second is None


def test_dismissed_name_removed_when_it_leaves_disk():
    """A dismissed skill that vanished is dropped, the same way pending and baseline are."""
    with _temp() as root:
        _skill(root, "keep")
        _skill(root, "keep-dismissed")
        _manifest(root, ["keep"])
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        ctx.state.set(
            "v1",
            {
                "baseline": {"keep": "manifest", "old-orig": "dismissed"},
                "pending": [],
                "dismissed": ["old-orig", "keep-dismissed"],
                "first_sweep_done": True,
                "manifest_missing_prompted": False,
                "lint_summary": {},
            },
        )
        cbs = _wire(ctx)
        cbs["on_session_start"](session_id="s")
        state = ctx.state.get("v1")
        dismissed = {d.lower() for d in state["dismissed"]}
        assert "old-orig" not in dismissed
        assert "keep-dismissed" in dismissed
        base = {k.lower(): v for k, v in state["baseline"].items()}
        assert "old-orig" not in base
        assert base["keep-dismissed"] == "dismissed"


def test_concurrent_enqueues_keep_both_names():
    """Two hook threads appending pending must not drop one of the names."""
    import threading

    with _temp() as root:
        ctx = FakeCtx()
        ctx.config["skills_root"] = str(root)
        cbs = _wire(ctx)
        barrier = threading.Barrier(2)

        def add(name):
            barrier.wait()
            cbs["on_skill_lifecycle"](action="created", skill_name=name)

        threads = [threading.Thread(target=add, args=(name,)) for name in ("alpha", "beta")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert _low(_pending(ctx)) == {"alpha", "beta"}


def _isolated_python(code: str, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code), *args],
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
    )


def _assert_isolated_ok(proc: subprocess.CompletedProcess) -> None:
    assert proc.returncode == 0, proc.stdout + proc.stderr


_PLAIN_ONLY_PLUGIN = """\
    import importlib.util
    import os
    import sys
    import tempfile
    from pathlib import Path

    plugin = Path(sys.argv[1]).resolve()
    scripts = (plugin / "shared" / "scripts").resolve()

    def _same(entry, target):
        if not entry:
            return False
        try:
            return Path(entry).resolve() == target
        except OSError:
            return False

    def _under_plugin(entry):
        if not entry:
            return False
        try:
            resolved = Path(entry).resolve()
        except OSError:
            return False
        return resolved == plugin or str(resolved).startswith(str(plugin) + os.sep)

    sys.path = [entry for entry in sys.path if not _under_plugin(entry)]
    sys.path.insert(0, str(plugin))
    assert sum(_same(entry, scripts) for entry in sys.path) == 0

    spec = importlib.util.spec_from_file_location(
        "skill_manifest_only_plugin",
        plugin / "__init__.py",
        submodule_search_locations=None,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    class _State:
        def __init__(self):
            self._d = {}

        def get(self, key, default=None):
            return self._d.get(key, default)

        def set(self, key, value):
            self._d[key] = value

    class _Ctx:
        def __init__(self):
            self.config = {}
            self.hooks = []
            self.state = _State()

        def get_config(self, key, default=None):
            return self.config.get(key, default)

        def register_hook(self, hook_name, callback):
            self.hooks.append((hook_name, callback))

    root = tempfile.mkdtemp(dir="/tmp")
    ctx = _Ctx()
    ctx.config["skills_root"] = root
    mod.register(ctx)
    mod.register(ctx)
    import hooks

    assert hooks.manifest_db is not None
    assert hooks.probe is not None
    assert hooks.manifest_lint is not None
    assert sum(_same(entry, plugin) for entry in sys.path) == 1
    assert sum(_same(entry, scripts) for entry in sys.path) == 1
    cbs = {event: cb for event, cb in ctx.hooks}
    cbs["on_skill_lifecycle"](action="created", skill_name="beta")
    assert ctx.state.get("v1")["pending"] == ["beta"]
    """


_BROKEN_BIND = """\
    import builtins
    import importlib.util
    import os
    import sys
    import tempfile
    import types
    from pathlib import Path

    plugin = Path(sys.argv[1]).resolve()
    mode = sys.argv[2]

    def _under_plugin(entry):
        if not entry:
            return False
        try:
            resolved = Path(entry).resolve()
        except OSError:
            return False
        return resolved == plugin or str(resolved).startswith(str(plugin) + os.sep)

    sys.path = [entry for entry in sys.path if not _under_plugin(entry)]
    real_import = builtins.__import__
    broken = {"on": True}

    def _guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if broken["on"] and (
            name == "manifest_lint"
            or str(name).endswith(".manifest_lint")
            or (fromlist and "manifest_lint" in fromlist)
        ):
            raise TypeError("manifest_lint import boom")
        return real_import(name, globals, locals, fromlist, level)

    builtins.__import__ = _guarded
    parent = types.ModuleType("hermes_plugins")
    parent.__path__ = []
    parent.__package__ = "hermes_plugins"
    sys.modules["hermes_plugins"] = parent
    qual = "hermes_plugins.skill_manifest_r7a"
    spec = importlib.util.spec_from_file_location(
        qual,
        plugin / "__init__.py",
        submodule_search_locations=[str(plugin)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__path__ = [str(plugin)]
    sys.modules[qual] = mod
    spec.loader.exec_module(mod)

    class _State:
        def __init__(self):
            self._d = {}

        def get(self, key, default=None):
            return self._d.get(key, default)

        def set(self, key, value):
            self._d[key] = value

    class _Ctx:
        def __init__(self, root):
            self.config = {"skills_root": root}
            self.hooks = []
            self.state = _State()

        def get_config(self, key, default=None):
            return self.config.get(key, default)

        def register_hook(self, hook_name, callback):
            self.hooks.append((hook_name, callback))

    ctx = _Ctx(tempfile.mkdtemp(dir="/tmp"))
    mod.register(ctx)
    hooks = sys.modules[qual + ".hooks"]
    assert hooks.manifest_db is None
    assert hooks.probe is None
    assert hooks.manifest_lint is None
    assert {event for event, _cb in ctx.hooks} == {
        "on_skill_lifecycle",
        "on_session_start",
        "pre_llm_call",
    }
    assert all(not _under_plugin(entry) for entry in sys.path)
    cbs = {event: cb for event, cb in ctx.hooks}

    if mode == "rebind":
        broken["on"] = False
        cbs["on_skill_lifecycle"](action="created", skill_name="beta")
        assert hooks.manifest_db is not None
        assert hooks.probe is not None
        assert hooks.manifest_lint is not None
        assert ctx.state.get("v1")["pending"] == ["beta"]
    elif mode == "repeat":
        calls = {"resolve": 0, "probe": 0}
        orig_resolve = hooks._resolve_skill_status
        orig_probe = hooks._probe

        def _spy_resolve(*_a, **_k):
            calls["resolve"] += 1
            return "untracked"

        def _spy_probe():
            calls["probe"] += 1
            return orig_probe()

        hooks._resolve_skill_status = _spy_resolve
        hooks._probe = _spy_probe
        for _ in range(3):
            assert cbs["on_skill_lifecycle"](action="created", skill_name="beta") is None
            assert cbs["on_session_start"](session_id="s") is None
            assert (
                cbs["pre_llm_call"](
                    session_id="s", user_message="hi", is_first_turn=False
                )
                is None
            )
        assert calls == {"resolve": 0, "probe": 0}
        assert ctx.state.get("v1") is None
        assert hooks._probed is False
        assert hooks.manifest_db is None
        broken["on"] = False
        hooks._resolve_skill_status = orig_resolve
        hooks._probe = orig_probe
        cbs["on_skill_lifecycle"](action="created", skill_name="beta")
        assert hooks.manifest_db is not None
        assert hooks.probe is not None
        assert hooks.manifest_lint is not None
        assert ctx.state.get("v1")["pending"] == ["beta"]
    else:
        raise SystemExit("unknown mode " + mode)
    """


_DECOY_PLAIN_LOAD = """\
    import importlib.util
    import sys
    import tempfile
    from pathlib import Path

    plugin = Path(sys.argv[1]).resolve()
    decoy = Path(sys.argv[2]).resolve()
    flag_dir = Path(sys.argv[3]).resolve()
    hooks_flag = flag_dir / "hooks-loaded"
    probe_flag = flag_dir / "probe-loaded"
    lint_flag = flag_dir / "lint-loaded"

    sys.path.insert(0, str(decoy))

    spec = importlib.util.spec_from_file_location(
        "skill_manifest_plain_decoy",
        plugin / "__init__.py",
        submodule_search_locations=None,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)

    class _State:
        def __init__(self):
            self._d = {}

        def get(self, key, default=None):
            return self._d.get(key, default)

        def set(self, key, value):
            self._d[key] = value

    class _Ctx:
        def __init__(self):
            self.config = {}
            self.hooks = []
            self.state = _State()

        def get_config(self, key, default=None):
            return self.config.get(key, default)

        def register_hook(self, hook_name, callback):
            self.hooks.append((hook_name, callback))

    root = Path(tempfile.mkdtemp(dir="/tmp"))
    skill = root / "beta"
    skill.mkdir()
    (skill / "SKILL.md").write_text("---\\n", encoding="utf-8")
    ctx = _Ctx()
    ctx.config["skills_root"] = str(root)
    mod.register(ctx)
    assert not hooks_flag.exists(), "plain-load imported the decoy hooks.py"
    assert not probe_flag.exists(), "plain-load imported the decoy probe.py"
    assert not lint_flag.exists(), "plain-load imported the decoy manifest_lint.py"
    cbs = {event: cb for event, cb in ctx.hooks}
    assert cbs["on_skill_lifecycle"].__module__ == "skill_manifest_hooks"
    bound = sys.modules[cbs["on_skill_lifecycle"].__module__]
    assert getattr(bound, "DECOY", False) is False
    assert getattr(bound.probe, "DECOY", False) is False
    assert getattr(bound.manifest_lint, "DECOY", False) is False
    cbs["on_skill_lifecycle"](action="created", skill_name="beta")
    assert ctx.state.get("v1")["pending"] == ["beta"]
    """


_DECOY_MANIFEST_DB = """\
    import importlib.util
    import os
    import sys
    from pathlib import Path

    plugin = Path(sys.argv[1]).resolve()
    decoy_dir = Path(sys.argv[2]).resolve()
    real_mdb = (plugin / "manifest_db.py").resolve()

    def _under_plugin(entry):
        if not entry:
            return False
        try:
            resolved = Path(entry).resolve()
        except OSError:
            return False
        return resolved == plugin or str(resolved).startswith(str(plugin) + os.sep)

    sys.path = [entry for entry in sys.path if not _under_plugin(entry)]
    sys.path.insert(0, str(decoy_dir))
    import manifest_db as decoy

    assert decoy.DECOY is True
    assert Path(decoy.__file__).resolve() == (decoy_dir / "manifest_db.py").resolve()
    assert Path(decoy.__file__).resolve() != real_mdb

    spec = importlib.util.spec_from_file_location(
        "skill_manifest_hooks_r9a",
        plugin / "hooks.py",
    )
    hooks = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = hooks
    spec.loader.exec_module(hooks)

    probe_mod = hooks.probe
    lint_mod = hooks.manifest_lint
    assert probe_mod is not None and lint_mod is not None
    assert sys.modules["manifest_db"] is decoy
    assert hooks.manifest_db is probe_mod.manifest_db is lint_mod.manifest_db
    assert Path(probe_mod.manifest_db.__file__).resolve() == real_mdb
    assert Path(lint_mod.manifest_db.__file__).resolve() == real_mdb
    assert getattr(probe_mod.manifest_db, "DECOY", False) is False
    assert getattr(lint_mod.manifest_db, "DECOY", False) is False
    """


def test_path_loaded_probe_and_lint_ignore_decoy_manifest_db():
    """A decoy manifest_db earlier on sys.path must not bind inside path-loaded probe/lint."""
    with _temp() as flag_dir:
        decoy = flag_dir / "decoy"
        decoy.mkdir()
        (decoy / "manifest_db.py").write_text(
            "DECOY = True\n"
            "def scan_tables(*_a, **_k):\n"
            "    raise RuntimeError('decoy manifest_db')\n",
            encoding="utf-8",
        )
        proc = _isolated_python(_DECOY_MANIFEST_DB, str(PLUGIN), str(decoy))
        _assert_isolated_ok(proc)


def test_plain_load_ignores_decoy_hooks_and_probe():
    """A hooks.py and probe.py earlier on sys.path must not be the plain-load modules."""
    with _temp() as flag_dir:
        decoy = flag_dir / "decoy"
        decoy.mkdir()
        (decoy / "hooks.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(flag_dir / 'hooks-loaded')!r}).write_text('yes')\n"
            "DECOY = True\n"
            "def configure(ctx):\n    return None\n"
            "def on_skill_lifecycle(*_a, **_k):\n    return None\n"
            "def on_session_start(*_a, **_k):\n    return None\n"
            "def on_pre_llm_call(*_a, **_k):\n    return None\n",
            encoding="utf-8",
        )
        (decoy / "probe.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(flag_dir / 'probe-loaded')!r}).write_text('yes')\n"
            "DECOY = True\n"
            "def list_skill_names(_root):\n    return set()\n"
            "def compute_pending(*_a, **_k):\n    return set(), set()\n",
            encoding="utf-8",
        )
        (decoy / "manifest_lint.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(flag_dir / 'lint-loaded')!r}).write_text('yes')\n"
            "DECOY = True\n"
            "def lint_manifest(*_a, **_k):\n    raise RuntimeError('decoy lint')\n",
            encoding="utf-8",
        )
        proc = _isolated_python(_DECOY_PLAIN_LOAD, str(PLUGIN), str(decoy), str(flag_dir))
        _assert_isolated_ok(proc)


def test_plain_module_only_plugin_dir_binds_deps():
    """Plain-module register with only the plugin dir on sys.path binds deps immediately."""
    proc = _isolated_python(_PLAIN_ONLY_PLUGIN, str(PLUGIN))
    _assert_isolated_ok(proc)


def test_typeerror_bind_survives_and_first_call_rebinds():
    """A TypeError from the manifest_lint import must not kill register; the first call rebinds."""
    proc = _isolated_python(_BROKEN_BIND, str(PLUGIN), "rebind")
    _assert_isolated_ok(proc)


def test_repeated_bind_failure_noops_until_importable():
    """While deps stay unbound, every hook returns without running; the next call works once they import."""
    proc = _isolated_python(_BROKEN_BIND, str(PLUGIN), "repeat")
    _assert_isolated_ok(proc)


def test_warn_once_threads_emit_one_formatted_warning():
    """N threads warning one key emit one formatted warning, and a second key still warns once."""
    key = "r7-warn-once-race"
    other = "r7-warn-once-other"
    message = "skill-manifest warn-once race"
    original = hooks._warned
    release = threading.Event()
    arrived: list[int] = []
    arrived_lock = threading.Lock()

    class _RacingSet(set):
        def __contains__(self, item):
            present = set.__contains__(self, item)
            if item == key and not present:
                with arrived_lock:
                    arrived.append(threading.get_ident())
                    count = len(arrived)
                if count == 1:
                    release.wait(timeout=0.5)
                else:
                    release.set()
            return present

    class _Capture(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.WARNING)
            self.formatted: list[str] = []

        def emit(self, record):
            self.formatted.append(self.format(record))

    handler = _Capture()
    handler.setFormatter(logging.Formatter("%(message)s"))
    hooks.logger.addHandler(handler)
    hooks._warned = _RacingSet(original)
    threads = []
    try:
        n = 8
        barrier = threading.Barrier(n)

        def _run():
            barrier.wait()
            hooks._warn_once(key, message)

        threads = [threading.Thread(target=_run) for _ in range(n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert all(not thread.is_alive() for thread in threads)
        race_hits = [text for text in handler.formatted if message in text]
        assert len(race_hits) == 1
        assert race_hits[0].count(message) == 1
        hooks._warn_once(key, message)
        assert len([text for text in handler.formatted if message in text]) == 1
        hooks._warn_once(other, "skill-manifest warn-once other")
        assert len([text for text in handler.formatted if message in text]) == 1
        others = [text for text in handler.formatted if "skill-manifest warn-once other" in text]
        assert len(others) == 1
    finally:
        hooks.logger.removeHandler(handler)
        hooks._warned = original
        for thread in threads:
            if thread.is_alive():
                thread.join(timeout=1)


_MANIFEST_DB_NAME = "skill_manifest_manifest_db"


def _temp_root() -> str:
    return os.environ.get("TMPDIR") or "/tmp"


def _park_bare_manifest_db(monkeypatch, directory: Path) -> None:
    """Point the bare name at a different file.

    Copy identity has to survive on the canonical slot. The bare name is
    allowed to be a decoy, and that is when a second copy used to replace
    the first copy's module.
    """
    decoy_path = directory / "decoy_manifest_db.py"
    decoy_path.write_text("DECOY = True\n", encoding="utf-8")
    decoy = types.ModuleType("manifest_db")
    decoy.__file__ = str(decoy_path.resolve())
    decoy.DECOY = True
    monkeypatch.setitem(sys.modules, "manifest_db", decoy)


def test_second_copy_does_not_clobber_first_copy_identity(monkeypatch):
    """A second copy under the same canonical name must not replace the first copy."""
    first = hooks.manifest_db
    assert first is not None
    original = os.path.realpath(first.__file__)
    monkeypatch.setitem(
        sys.modules, _MANIFEST_DB_NAME, sys.modules.get(_MANIFEST_DB_NAME)
    )
    before = set(sys.modules)
    try:
        with tempfile.TemporaryDirectory(dir=_temp_root()) as tmp:
            tmp_path = Path(tmp)
            _park_bare_manifest_db(monkeypatch, tmp_path)
            copy_path = tmp_path / "manifest_db.py"
            copy_path.write_bytes(Path(original).read_bytes())
            hooks._load_by_path(_MANIFEST_DB_NAME, str(copy_path))
            assert hooks.manifest_db is first
            assert os.path.realpath(hooks.manifest_db.__file__) == original
            again = hooks._load_by_path(_MANIFEST_DB_NAME, original)
            assert again is first, (
                "repeat load of the first copy returned a new module "
                f"{again!r} from {getattr(again, '__file__', None)}"
            )
            assert probe._load_by_path(_MANIFEST_DB_NAME, original) is first
            assert manifest_lint._load_by_path(_MANIFEST_DB_NAME, original) is first
    finally:
        for key in list(sys.modules):
            if key not in before:
                sys.modules.pop(key, None)


def test_two_copies_are_independently_consistent(monkeypatch):
    """The two copies are different modules, and each copy reloads as itself."""
    first = hooks.manifest_db
    assert first is not None
    original = os.path.realpath(first.__file__)
    monkeypatch.setitem(
        sys.modules, _MANIFEST_DB_NAME, sys.modules.get(_MANIFEST_DB_NAME)
    )
    before = set(sys.modules)
    try:
        with tempfile.TemporaryDirectory(dir=_temp_root()) as tmp:
            tmp_path = Path(tmp)
            _park_bare_manifest_db(monkeypatch, tmp_path)
            copy_path = tmp_path / "manifest_db.py"
            copy_path.write_bytes(Path(original).read_bytes())
            second = hooks._load_by_path(_MANIFEST_DB_NAME, str(copy_path))
            assert second is not first
            assert probe._load_by_path(_MANIFEST_DB_NAME, str(copy_path)) is second
            # A later load of the first copy must not evict the second copy.
            hooks._load_by_path(_MANIFEST_DB_NAME, original)
            second_again = probe._load_by_path(_MANIFEST_DB_NAME, str(copy_path))
            assert second_again is second, (
                "repeat load of the second copy returned a new module "
                f"{second_again!r} from {getattr(second_again, '__file__', None)}"
            )
            assert hooks._load_by_path(_MANIFEST_DB_NAME, str(copy_path)) is second
            assert manifest_lint._load_by_path(_MANIFEST_DB_NAME, str(copy_path)) is second
    finally:
        for key in list(sys.modules):
            if key not in before:
                sys.modules.pop(key, None)


def _manifest_db_sys_key(real: str) -> str:
    digest = hashlib.sha1(os.fsencode(real)).hexdigest()[:12]
    return f"{_MANIFEST_DB_NAME}_{digest}"


def test_surrogate_path_hashes_without_error():
    """A path with undecodable filename bytes loads; repeat loads share one module."""
    before = set(sys.modules)
    tmp = Path(tempfile.mkdtemp(dir="/tmp"))
    try:
        source = Path(hooks.__file__).resolve().parent / "manifest_db.py"
        raw = tmp / b"invalid-\xff-manifest_db.py".decode("utf-8", "surrogateescape")

        shutil.copyfile(source, raw)
        mod = hooks._load_by_path(_MANIFEST_DB_NAME, str(raw))
        assert mod is not None
        mod_file = getattr(mod, "__file__", "") or ""
        assert mod_file.endswith("manifest_db.py")
        again = hooks._load_by_path(_MANIFEST_DB_NAME, str(raw))
        assert again is mod
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for key in list(sys.modules):
            if key not in before:
                sys.modules.pop(key, None)


def test_loaded_module_name_is_a_valid_identifier():
    """A path-loaded manifest_db name is an identifier stored under that name."""
    first = hooks.manifest_db
    assert first is not None
    original = os.path.realpath(first.__file__)
    mod = hooks._load_by_path(_MANIFEST_DB_NAME, original)
    name = mod.__name__
    assert name == sys.modules[name].__name__
    assert name.isidentifier()
    assert "@" not in name
    assert "/" not in name


def test_manifest_doc_pickles():
    """ManifestDoc from a path-loaded manifest_db round-trips as the same class."""
    first = hooks.manifest_db
    assert first is not None
    original = os.path.realpath(first.__file__)
    mod = hooks._load_by_path(_MANIFEST_DB_NAME, original)
    doc = mod.load_manifest_text("| Skill | Source |\n| --- | --- |\n| alpha | src |\n")
    loaded = pickle.loads(pickle.dumps(doc))
    assert type(loaded) is type(doc)
    assert loaded.skills == doc.skills


def test_bare_branch_conflict_raises(monkeypatch):
    """The bare name must not hide a keyed slot that holds a different file."""
    first = hooks.manifest_db
    assert first is not None
    original = os.path.realpath(first.__file__)
    sys_key = _manifest_db_sys_key(original)
    before = set(sys.modules)
    try:
        with tempfile.TemporaryDirectory(dir=_temp_root()) as tmp:
            other_path = Path(tmp) / "other_manifest_db.py"
            other_path.write_text("OTHER = True\n", encoding="utf-8")
            other = types.ModuleType("other_manifest_db")
            other.__file__ = str(other_path.resolve())
            other.OTHER = True
            for key in list(sys.modules):
                if (
                    key == _MANIFEST_DB_NAME
                    or key.startswith(_MANIFEST_DB_NAME + "@")
                    or key.startswith(_MANIFEST_DB_NAME + "_")
                ):
                    monkeypatch.delitem(sys.modules, key, raising=False)
            monkeypatch.setitem(sys.modules, "manifest_db", first)
            monkeypatch.setitem(sys.modules, sys_key, other)
            with pytest.raises(ImportError) as raised:
                hooks._load_by_path(_MANIFEST_DB_NAME, original)
            assert raised.value.args == (original,)
            assert hooks.manifest_db is first
            assert sys.modules[sys_key] is other
            assert sys.modules["manifest_db"] is first
    finally:
        for key in list(sys.modules):
            if key not in before:
                sys.modules.pop(key, None)
