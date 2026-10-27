"""Contract tests for the contrastive-advisor hooks.

Hazard being defended: a hook that silently blocks or rewrites calls, drops
its advisory because of a return-shape mismatch, or never fires in
production because the package context differs from the test context.

Test-suite rules on this project:
- Real Hermes contracts: pre_llm_call returns {"context": str} or None
  (agent/turn_context.py); ctx.state.get/set are the profile-scoped
  quota-bounded JSON store.
- The loader-fidelity tests run in SUBPROCESSES with clean sys.modules —
  the plain-module vs package-context split is what bit kanban-mirror v0.1.0.
- Reviewer sandboxes are read-only on /root: pytest tmp may show OSError
  Errno 30 in THEIR runs; count only assertion failures as regressions.
"""

import importlib.util
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

HERE = pathlib.Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent

spec = importlib.util.spec_from_file_location("advisor", PLUGIN_DIR / "advisor.py")
advisor = importlib.util.module_from_spec(spec)
sys.modules["advisor"] = advisor
spec.loader.exec_module(advisor)

CONTEXT = [{"role": "user", "content": "hi"}]  # non-empty: not turn 1


class State:
    def __init__(self):
        self._d = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


## Reserved config roots per live core (hermes_cli/plugins_state.py).
_RESERVED_ROOTS = {"model", "plugins", "security", "settings"}


class Ctx:
    """Test double that mirrors the REAL core get_config contract: relative
    plugin keys only; a reserved root raises ValueError (core logs a
    Rejected-config-path warning and re-raises); missing entry raises."""
    DEFAULT_SETTINGS = {"enabled": True}

    def __init__(self, tmpdir, settings=None, *, missing=False):
        self.state = State()
        self.hermes_home = tmpdir
        self._settings = (
            self.DEFAULT_SETTINGS if settings is None else dict(settings)
        )
        self._missing = missing

    def get_config(self, name):
        if isinstance(name, str) and name.split(".")[0].lower() in _RESERVED_ROOTS:
            raise ValueError("rejected: reserved root")
        if self._missing:
            raise KeyError(name)
        return self._settings.get(name)


def _settings(tmpdir, **over):
    return {
        "enabled": True,
        "probe_root": str(pathlib.Path(tmpdir) / "skill-probes"),
        "ttl_minutes": 30,
        **over,
    }


BODY = {
    "action": "patch",
    "name": "ascii-art",
    "old_text": "3. **Propose candidates.** step",
    "new_text": "## Pitfalls\nnew step body long enough to matter here",
}


# --- heuristic: what counts as a behavioral edit ------------------------------


def test_skill_manage_body_edit_is_behavioral(tmp_path):
    hit = advisor.is_behavioral_edit("skill_manage", BODY, _settings(str(tmp_path)))
    assert hit is not None and hit[0] == "ascii-art"


def test_skill_manage_operations_array_is_behavioral(tmp_path):
    """Batch shape: the marker must come from the operations array itself —
    the base fields are markerless, so skipping `operations` fails this."""
    base = dict(BODY, old_text="plain filler text", new_text="plain replacement")
    args = dict(base, operations=[{"action": "patch", "old_text": "a", "new_text": "## Pitfalls"}])
    assert advisor.is_behavioral_edit("skill_manage", args, _settings(str(tmp_path))) is not None
    # and the markerless base alone stays silent
    assert advisor.is_behavioral_edit("skill_manage", base, _settings(str(tmp_path))) is None


def test_skill_manage_create_action_is_behavioral(tmp_path):
    """The marker must come from `content` (base fields markerless)."""
    args = {"action": "create", "name": "my-new-skill",
            "old_text": "filler text", "content": "## Procedure\nsteps"}
    assert advisor.is_behavioral_edit("skill_manage", args, _settings(str(tmp_path))) is not None
    assert advisor.is_behavioral_edit(
        "skill_manage", dict(args, content="just some prose body"), _settings(str(tmp_path))
    ) is None


def test_skill_manage_old_string_family_is_behavioral(tmp_path):
    """Real-shape mirror, 2026-10-19 live miss: skill_manage accepts the
    patch-tool field family (old_string/new_string) — signal exists ONLY in
    `new_string`, so skipping that field fails this test."""
    markerless = "a plain replacement body long enough to clear the floor " * 8  # >400 chars, no marker
    op_alias = {"action": "patch", "name": "ascii-art", "old_text": "x", "new_text": "y"}
    s = _settings(str(tmp_path))
    hit = advisor.is_behavioral_edit("skill_manage", dict(op_alias, new_string=markerless), s)
    assert hit is not None and hit[0] == "ascii-art", "new_string alone must carry the signal"
    # signal only in old_string: same union requirement
    hit_old = advisor.is_behavioral_edit("skill_manage", dict(op_alias, old_string=markerless), s)
    assert hit_old is not None, "old_string alone must carry the signal"
    # and the alias-base alone (no cross-family fields) stays silent as before
    assert advisor.is_behavioral_edit("skill_manage", op_alias, s) is None


def test_skill_manage_operations_array_old_string_family(tmp_path):
    """Array ops accept the patch-tool family too — signal only inside the
    op's `new_string`, markerless base fields elsewhere."""
    args = {"action": "patch", "name": "ascii-art",
            "old_text": "plain filler", "new_text": "plain filler",
            "operations": [{"action": "patch",
                            "new_string": "## Pitfalls\n" + "more body text " * 40}]}
    assert advisor.is_behavioral_edit("skill_manage", args, _settings(str(tmp_path))) is not None


def test_skill_manage_pure_batch_shape_fires_per_op(tmp_path):
    """Pure batch shape: ONLY `operations`, each entry carries its own name+
    action (the live 2026-10-19 regression). Top-level action/name read
    empty, so the old top-level-only detector returned None and the advisory
    never queued. Two ops on two skills -> two hits, first-appearance order."""
    args = {
        "operations": [
            {"action": "create", "name": "skill-alpha", "content": "## Pitfalls\nbody text"},
            {"action": "delete", "name": "skill-skip"},
            {"action": "patch", "name": "skill-beta", "old_string": "x", "new_string": "## Verification\nbody text"},
        ]
    }
    hits = advisor.behavioral_hits("skill_manage", args, _settings(str(tmp_path)))
    assert [h[0] for h in hits] == ["skill-alpha", "skill-beta"]

    # same name twice folds into one candidate
    args2 = {
        "operations": [
            {"action": "patch", "name": "skill-gamma", "old_string": "a", "new_string": "b"},
            {"action": "patch", "name": "skill-gamma", "new_text": "## Quick Reference\nbody"},
        ]
    }
    hits2 = advisor.behavioral_hits("skill_manage", args2, _settings(str(tmp_path)))
    assert len(hits2) == 1 and hits2[0][0] == "skill-gamma"

    # single-hit contract preserved for the same call
    first = advisor.is_behavioral_edit("skill_manage", args, _settings(str(tmp_path)))
    assert first is not None and first == hits[0]


def test_patch_tool_old_string_paths(tmp_path):
    args = {
        "path": "/root/.hermes/skills/creative/ascii-art/SKILL.md",
        "old_string": "middle line",
        "new_string": "replaced" + "x" * 500,  # long: clears the 400-char floor
    }
    assert advisor.is_behavioral_edit("patch", args, _settings(str(tmp_path))) is not None


def test_frontmatter_only_edit_is_silent(tmp_path):
    s = _settings(str(tmp_path))
    args = {"action": "patch", "name": "ascii-art", "old_text": "version: 1.0.0", "new_text": "version: 1.0.1"}
    assert advisor.is_behavioral_edit("skill_manage", args, s) is None


def test_long_markerless_edit_is_behavioral(tmp_path):
    """The 400-char floor: a big rewrite without a header still counts."""
    s = _settings(str(tmp_path))
    args = {"action": "patch", "name": "ascii-art", "old_text": "old body", "new_text": "n" * 500}
    assert advisor.is_behavioral_edit("skill_manage", args, s) is not None


def test_short_markerless_edit_is_silent(tmp_path):
    s = _settings(str(tmp_path))
    args = {"action": "patch", "name": "ascii-art", "old_text": "old body", "new_text": "new body"}
    assert advisor.is_behavioral_edit("skill_manage", args, s) is None


def test_traversal_name_is_never_detected(tmp_path):
    s = _settings(str(tmp_path))
    for bad in ("../../..", "/root", "..", "a/b", "Not A Skill", "", "X" * 100):
        assert advisor.is_behavioral_edit("skill_manage", dict(BODY, name=bad), s) is None


def test_excluded_skill_is_silent(tmp_path):
    s = _settings(str(tmp_path))
    assert advisor.is_behavioral_edit("skill_manage", dict(BODY, name="contrastive-refinement"), s) is None


def test_plugin_namespaced_skill_is_silent(tmp_path):
    s = _settings(str(tmp_path))
    assert advisor.is_behavioral_edit("skill_manage", dict(BODY, name="cos:daily-briefing"), s) is None


def test_basenames_must_match_exactly(tmp_path):
    s = _settings(str(tmp_path))
    args = {"path": "/root/.hermes/skills/x/NOTSKILL.md", "content": "## Pitfalls"}
    assert advisor.is_behavioral_edit("write_file", args, s) is None


def test_write_file_backslash_path_normalized(tmp_path):
    s = _settings(str(tmp_path))
    args = {"path": "C:\\hermes\\skills\\ascii-art\\SKILL.md", "content": "## Pitfalls"}
    assert advisor.is_behavioral_edit("write_file", args, s) is not None


def test_disabled_config_is_always_silent(tmp_path):
    s = {"enabled": False}
    assert advisor.is_behavioral_edit("skill_manage", BODY, s) is None


def test_non_dict_args_is_silent(tmp_path):
    assert advisor.is_behavioral_edit("skill_manage", None, _settings(str(tmp_path))) is None


def test_observer_never_mutates_args(tmp_path):
    ctx = Ctx(str(tmp_path))
    original = dict(BODY)
    advisor.on_pre_tool_call(ctx, tool_name="skill_manage", args=dict(BODY), session_id="sess-1")
    assert BODY == original


def test_custom_markers_and_exclusions(tmp_path):
    s = _settings(str(tmp_path), markers=("## Zones",), excluded_skills=("ascii-art",))
    hit = advisor.is_behavioral_edit(
        "skill_manage", {"action": "patch", "name": "zones-skill", "new_text": "## Zones\nx"}, s
    )
    assert hit is not None
    assert advisor.is_behavioral_edit("skill_manage", dict(BODY, name="ascii-art"), s) is None


# --- observer: pending storage -------------------------------------------------


def test_observer_stores_pending_with_tool_and_session(tmp_path):
    ctx = Ctx(str(tmp_path))
    assert advisor.on_pre_tool_call(ctx, tool_name="skill_manage", args=dict(BODY), session_id="sess-1") is None
    item = ctx.state.get("v1")["pending"][-1]
    assert item["skill"] == "ascii-art" and item["tool"] == "skill_manage" and item["session_id"] == "sess-1"


def test_observer_newest_wins_and_bounded(tmp_path):
    ctx = Ctx(str(tmp_path))
    for name in ("a", "b", "c", "d", "e"):
        advisor.on_pre_tool_call(ctx, tool_name="skill_manage", args=dict(BODY, name=name), session_id="s")
    pending = ctx.state.get("v1")["pending"]
    assert [p["skill"] for p in pending] == ["c", "d", "e"]


def test_observer_fail_open_and_logs(tmp_path, capsys):
    ctx = Ctx(str(tmp_path))
    ctx.state.set = lambda k, v: (_ for _ in ()).throw(RuntimeError("disk full"))
    out = advisor.on_pre_tool_call(ctx, tool_name="skill_manage", args=dict(BODY))
    assert out is None
    assert "contrastive-advisor" in capsys.readouterr().err


def test_state_read_failure_logs_and_defaults(tmp_path, capsys):
    ctx = Ctx(str(tmp_path))
    ctx.state.get = lambda k, default=None: (_ for _ in ()).throw(RuntimeError("corrupt"))
    args = {"path": "/x/skills/ascii-art/SKILL.md", "content": "## Pitfalls"}
    assert advisor.on_pre_tool_call(ctx, tool_name="patch", args=args) is None
    assert "state read failed" in capsys.readouterr().err


# --- injector ------------------------------------------------------------------


def test_injection_shape_is_context_dict(tmp_path):
    ctx = Ctx(str(tmp_path))
    advisor.on_pre_tool_call(ctx, tool_name="skill_manage", args=dict(BODY), session_id="sess-1")
    out = advisor.on_pre_llm_call(ctx, session_id="sess-1", user_message="x", is_first_turn=False)
    assert isinstance(out, dict) and list(out) == ["context"]
    assert "ascii-art" in out["context"]


def test_injector_advises_probe_status_missing(tmp_path):
    ctx = Ctx(str(tmp_path))
    advisor.on_pre_tool_call(ctx, tool_name="skill_manage", args=dict(BODY), session_id="s")
    out = advisor.on_pre_llm_call(ctx, session_id="s", user_message="x", is_first_turn=False)
    expected = str(pathlib.Path(tmp_path) / "skill-probes" / "ascii-art")
    assert expected in out["context"] and "missing" in out["context"]


def test_injector_requires_both_cycle_files(tmp_path):
    probe = pathlib.Path(tmp_path) / "skill-probes" / "ascii-art" / "cycle-1"
    probe.mkdir(parents=True)
    (probe / "selection.md").write_text("# only selection\n")  # no probe.md
    ctx = Ctx(str(tmp_path))
    advisor.on_pre_tool_call(ctx, tool_name="skill_manage", args=dict(BODY), session_id="s")
    out = advisor.on_pre_llm_call(ctx, session_id="s", user_message="x", is_first_turn=False)
    assert "missing" in out["context"]


def test_injector_found_when_full_cycle_present(tmp_path):
    probe = pathlib.Path(tmp_path) / "skill-probes" / "ascii-art" / "cycle-1"
    probe.mkdir(parents=True)
    (probe / "probe.md").write_text("# Probe\n")
    (probe / "selection.md").write_text("# Blind selection\n")
    ctx = Ctx(str(tmp_path))
    advisor.on_pre_tool_call(ctx, tool_name="skill_manage", args=dict(BODY), session_id="s")
    out = advisor.on_pre_llm_call(ctx, session_id="s", user_message="x", is_first_turn=False)
    assert "found: 1 cycle" in out["context"]


def test_injector_fires_exactly_once(tmp_path):
    ctx = Ctx(str(tmp_path))
    advisor.on_pre_tool_call(ctx, tool_name="skill_manage", args=dict(BODY), session_id="s")
    kw = dict(session_id="s", user_message="x", is_first_turn=False)
    assert advisor.on_pre_llm_call(ctx, **kw) is not None
    assert advisor.on_pre_llm_call(ctx, **kw) is None


def test_injector_serves_newest_first(tmp_path):
    ctx = Ctx(str(tmp_path))
    for name in ("older-skill", "newest-skill"):
        advisor.on_pre_tool_call(ctx, tool_name="skill_manage", args=dict(BODY, name=name), session_id="s")
    out = advisor.on_pre_llm_call(ctx, session_id="s", user_message="x", is_first_turn=False)
    assert "newest-skill" in out["context"]
    second = advisor.on_pre_llm_call(ctx, session_id="s", user_message="x", is_first_turn=False)
    assert second is not None and "older-skill" in second["context"]


def test_all_expired_queue_is_reclaimed(tmp_path):
    """Muse/Codex: the trimmed queue must persist even with no valid item."""
    ctx = Ctx(str(tmp_path))
    advisor.observe_edit(ctx, "stale", str(pathlib.Path(tmp_path) / "p"))
    store = ctx.state.get("v1", {})
    store["pending"][0]["created_at"] = (datetime.now() - timedelta(hours=5)).isoformat()
    ctx.state.set("v1", store)
    assert advisor.on_pre_llm_call(ctx, session_id="s", user_message="x", is_first_turn=False) is None
    assert ctx.state.get("v1", {})["pending"] == []


def test_first_turn_skipped_but_item_survives(tmp_path):
    ctx = Ctx(str(tmp_path))
    advisor.observe_edit(ctx, "ascii-art", str(pathlib.Path(tmp_path) / "skill-probes" / "ascii-art"))
    assert advisor.on_pre_llm_call(
        ctx, session_id="s", user_message="x", is_first_turn=True, conversation_history=[]
    ) is None
    out = advisor.on_pre_llm_call(
        ctx, session_id="s", user_message="x", is_first_turn=False, conversation_history=CONTEXT
    )
    assert out is not None


def test_history_fallback_when_flag_absent(tmp_path):
    ctx = Ctx(str(tmp_path))
    advisor.observe_edit(ctx, "ascii-art", str(pathlib.Path(tmp_path) / "p"))
    assert advisor.on_pre_llm_call(ctx, session_id="s", user_message="x") is None  # turn 1
    out = advisor.on_pre_llm_call(ctx, session_id="s", user_message="x", conversation_history=CONTEXT)
    assert out is not None


def test_advisory_capped_even_with_long_name(tmp_path):
    ctx = Ctx(str(tmp_path))
    advisor.observe_edit(ctx, "x" * 64, str(pathlib.Path(tmp_path) / "p"))
    out = advisor.on_pre_llm_call(ctx, session_id="s", user_message="x", is_first_turn=False)
    assert len(out["context"]) <= advisor.INJECT_HARD_CAP


def test_injector_fail_open(tmp_path, capsys):
    ctx = Ctx(str(tmp_path))
    ctx.state.get = lambda k, default=None: (_ for _ in ()).throw(RuntimeError("corrupt"))
    assert advisor.on_pre_llm_call(ctx, session_id="s", user_message="x", is_first_turn=False) is None
    assert "state read failed" in capsys.readouterr().err


def test_disabled_ctx_means_no_store_write(tmp_path):
    ctx = Ctx(str(tmp_path), settings={"enabled": False})
    before = dict(ctx.state._d)
    advisor.on_pre_tool_call(ctx, tool_name="skill_manage", args=dict(BODY), session_id="s")
    advisor.on_pre_llm_call(ctx, session_id="s", user_message="x", is_first_turn=False)
    assert ctx.state._d == before


# --- config shapes ---------------------------------------------------------------


def test_settings_relative_keys(tmp_path):
    # Live contract: each key is a relative plugin path
    # (plugins.entries.<id>.settings.<key>); no whole-dict shape exists.
    ctx = Ctx(str(tmp_path), settings={"enabled": True, "probe_root": "/a"})
    assert advisor.load_settings(ctx)["probe_root"] == "/a"


def test_settings_root_request_never_made(tmp_path):
    # 'settings' is a RESERVED config root: core raises before any read.
    # load_settings must never request it and still work — each key is read
    # on its own, so one rejected path costs only that key.
    class RejectingCtx(Ctx):
        def __init__(self):
            super().__init__(str(tmp_path), settings={"enabled": True})
            self.requested = []

        def get_config(self, name):
            self.requested.append(name)
            return super().get_config(name)

    ctx = RejectingCtx()
    s = advisor.load_settings(ctx)
    assert "settings" not in ctx.requested
    assert s["enabled"] is True


def test_disabled_explicit_false(tmp_path):
    ctx = Ctx(str(tmp_path), settings={"enabled": False})
    assert advisor.load_settings(ctx) == {"enabled": False}


def test_enabled_absent_disables(tmp_path):
    # Opt-in: settings entry present but no explicit enabled: true → disabled.
    ctx = Ctx(str(tmp_path), settings={"probe_root": "/a"})
    assert advisor.load_settings(ctx) == {"enabled": False}


def test_excluded_skills_as_string_plus_default_union(tmp_path):
    ctx = Ctx(str(tmp_path), settings={"enabled": True, "excluded_skills": "my-skill"})
    s = advisor.load_settings(ctx)
    assert isinstance(s["excluded_skills"], tuple)
    assert "my-skill" in s["excluded_skills"] and "contrastive-refinement" in s["excluded_skills"]


def test_partial_rejection_keeps_other_keys(tmp_path):
    # One key read raising does not disable the rest (reserved roots, hostile stubs).
    ctx = Ctx(str(tmp_path))
    inner = ctx.get_config

    def hostile(name):
        if name == "probe_root":
            raise RuntimeError("boom")
        return inner(name)

    ctx.get_config = hostile  # type: ignore[method-assign]
    s = advisor.load_settings(ctx)
    assert s["enabled"] is True  # enabled survived the rejected sibling key
    assert s["probe_root"].endswith("skill-probes")  # rejected key fell back to default


def test_missing_config_disables(tmp_path):
    ctx = Ctx(str(tmp_path), missing=True)  # get_config raises: entry absent
    assert advisor.load_settings(ctx) == {"enabled": False}


# --- probe evidence bounds ----------------------------------------------------------


def test_unknown_branch_when_glob_fails(tmp_path):
    probe = pathlib.Path(tmp_path) / "skill-probes" / "ascii-art"
    probe.mkdir(parents=True)
    orig = pathlib.Path.glob
    pathlib.Path.glob = lambda self, pattern: (_ for _ in ()).throw(OSError("sandbox"))
    try:
        status = advisor.probe_evidence(str(probe))
    finally:
        pathlib.Path.glob = orig
    assert status.startswith("unknown")


def test_cycle_depth_bounded(tmp_path):
    root = pathlib.Path(tmp_path) / "skill-probes" / "ascii-art"
    deep = root / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "selection.md").write_text("x")
    (deep / "probe.md").write_text("x")
    status = advisor.probe_evidence(str(root))
    assert "missing" in status  # depth 3 is out of the bounded scan


def test_selection_without_probe_md_is_not_a_cycle(tmp_path):
    root = pathlib.Path(tmp_path) / "skill-probes" / "ascii-art"
    (root / "c1").mkdir(parents=True)
    (root / "c1" / "selection.md").write_text("x")
    assert "missing" in advisor.probe_evidence(str(root))


# --- loader fidelity (subprocesses: clean sys.modules each run) ---------------------

LOADER_DRIVER = r'''
import importlib.util, pathlib, sys, tempfile, json

plugin_dir = sys.argv[1]
mode = sys.argv[2]  # "package" (gateway-like) or "plain" (fallback import)

class State:
    def __init__(self): self._d = {}
    def get(self, key, default=None): return self._d.get(key, default)
    def set(self, key, value): self._d[key] = value

home = tempfile.mkdtemp()

if mode == "package":
    import types
    parent = types.ModuleType("hermes_plugins")
    parent.__path__ = [str(pathlib.Path(plugin_dir).parent)]
    sys.modules["hermes_plugins"] = parent
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.contrastive_advisor", plugin_dir + "/__init__.py",
        submodule_search_locations=[plugin_dir])
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [plugin_dir]
    sys.modules[spec.name] = module  # Opus M6: register BEFORE exec_module
else:
    spec = importlib.util.spec_from_file_location(
        "contrastive_advisor_plain", plugin_dir + "/__init__.py")
    module = importlib.util.module_from_spec(spec)

spec.loader.exec_module(module)

adv = module._load_advisor()
if mode == "package":
    # The relative import must actually resolve — the fallback would
    # append the plugin dir to sys.path and bare-import `advisor`.
    assert adv.__package__ == "hermes_plugins.contrastive_advisor", adv.__package__

hooks = {}
class Ctx:
    def register_hook(self, event, fn): hooks[event] = fn
    def __init__(self):
        self.state = State()
        self.hermes_home = home
    def get_config(self, name):
        # Real core contract: relative keys; reserved roots rejected.
        assert name not in ("settings", "model", "plugins", "security"), name
        return {"enabled": True}.get(name)

ctx = Ctx()
module.register(ctx)
assert set(hooks) == {"pre_tool_call", "pre_llm_call", "on_session_start"}, hooks

body = {"action": "patch", "name": "ascii-art",
        "old_text": "old", "new_text": "## Pitfalls\nnew"}
assert hooks["pre_tool_call"](tool_name="skill_manage", args=body) is None

result = hooks["pre_llm_call"](session_id="s", user_message="x", is_first_turn=False)
after = ctx.state._d.get("v1", {})
print(json.dumps({"inject": result, "pending_after": after.get("pending", [])}))
'''


def _run_driver(mode):
    return subprocess.run(
        [sys.executable, "-c", LOADER_DRIVER, str(PLUGIN_DIR), mode],
        capture_output=True, text=True, timeout=60, cwd=str(HERE),
    )


@pytest.mark.parametrize("mode", ["package", "plain"])
def test_loader_context_wires_and_fires(mode):
    import json as _json
    proc = _run_driver(mode)
    assert proc.returncode == 0, proc.stderr[-800:]
    payload = [line for line in proc.stdout.strip().splitlines() if line.startswith("{")][-1]
    data = _json.loads(payload)
    inject = data["inject"]
    assert isinstance(inject, dict) and "ascii-art" in inject["context"]
    assert data["pending_after"] == [] or all(
        item.get("skill") != "ascii-art" for item in data["pending_after"]
    )