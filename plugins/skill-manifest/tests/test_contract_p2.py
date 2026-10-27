"""HERMES-WRITTEN contract test — Phase 2 (hooks + registration).

Pinned by the orchestrator 2026-10-16. The builder must NOT modify or delete
this file; on conflict the contract wins. It fails red until hooks.py,
__init__.py, and plugin.yaml exist with the agreed API (BRIEF-P2.md).

Boundary contracts pinned here:
1. Package-exec loader fidelity: register() must succeed when __init__.py is
   loaded the EXACT way the gateway loader does (synthetic package, no
   sys.path) — kanban-mirror v0.1.0 failed exactly this and shipped broken.
2. Plain-module import form must ALSO work (the plugin's own test suite).
3. pre_llm_call return shape + turn-1 skip: {"context": str} | None.
4. Observer must survive an internal exception (it fires inside core's
   skill_manage mutation path).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent

try:
    import hooks as hooks_mod
except ImportError:
    hooks_mod = None

HAS_INIT = (PLUGIN_DIR / "__init__.py").is_file()
HAS_PLUGIN_YAML = (PLUGIN_DIR / "plugin.yaml").is_file()


class FakeState:
    def __init__(self):
        self._d = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class FakeRegistration:
    def __init__(self, event, cb):
        self.event, self.cb = event, cb


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
        self.hooks.append(FakeRegistration(hook_name, callback))
        return callback


@pytest.fixture(scope="module")
def init_module():
    """Load __init__.py as a PLAIN module with the plugin dir + shared/scripts
    on sys.path (the form the gateway loader does NOT provide)."""
    if not HAS_INIT:
        pytest.xfail("Phase 2 __init__.py not implemented yet")
    import importlib.util

    sys.path.insert(0, str(PLUGIN_DIR))
    sys.path.insert(0, str(PLUGIN_DIR / "shared" / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "skill_manifest_plain_init", PLUGIN_DIR / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    yield mod
    sys.path[:] = [p for p in sys.path if not p.startswith(str(PLUGIN_DIR))]


@pytest.mark.contract
class TestLoaderContract:
    def test_files_exist(self):
        assert HAS_INIT, "__init__.py missing — Phase 2 not implemented"
        assert HAS_PLUGIN_YAML, "plugin.yaml missing — Phase 2 not implemented"
        assert hooks_mod is not None, "hooks.py missing — Phase 2 not implemented"

    def test_gateway_package_exec_register(self):
        """Exec __init__.py AS A PACKAGE (submodule_search_locations, __path__
        set, NO plugin dir on sys.path) and CALL register(ctx) — the exact
        gateway loader context. register must not raise and must wire all
        three hooks."""
        import importlib.util

        parent_pkg = types.ModuleType("hermes_plugins")
        sys.modules.setdefault("hermes_plugins", parent_pkg)
        name = "hermes_plugins.skill_manifest"
        spec = importlib.util.spec_from_file_location(
            name, PLUGIN_DIR / "__init__.py",
            submodule_search_locations=[str(PLUGIN_DIR)],
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        parent_pkg.__path__ = []
        spec.loader.exec_module(mod)
        ctx = FakeCtx()
        mod.register(ctx)
        events = {r.event for r in ctx.hooks}
        assert events == {"on_skill_lifecycle", "on_session_start", "pre_llm_call"}, (
            "register(ctx) must wire exactly the three declared hooks"
        )

    def test_plain_module_register(self, init_module):
        ctx = FakeCtx()
        init_module.register(ctx)
        assert {r.event for r in ctx.hooks} == {
            "on_skill_lifecycle", "on_session_start", "pre_llm_call"
        }

    def test_plugin_yaml_declares_the_same_hooks(self):
        import yaml

        y = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
        assert y["name"] == "skill-manifest"
        assert set(y["provides_hooks"]) == {
            "on_skill_lifecycle", "on_session_start", "pre_llm_call"
        }
        assert "requires_env" not in y or not y["requires_env"]


@pytest.mark.contract
class TestHookContract:
    def test_observer_survives_internal_exception(self, init_module):
        """An observer raising inside skill_manage's mutation path would break
        skill creation — every hook must be a closed fail-open body."""
        ctx = FakeCtx()
        init_module.register(ctx)
        obs = next(r for r in ctx.hooks if r.event == "on_skill_lifecycle").cb
        orig = init_module.hooks._resolve_skill_status
        init_module.hooks._resolve_skill_status = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            obs(action="created", skill_name="whatever")  # must NOT raise
        finally:
            init_module.hooks._resolve_skill_status = orig

    def test_pre_llm_call_shape_and_turn1_skip(self, init_module):
        ctx = FakeCtx()
        init_module.register(ctx)
        fn = next(r for r in ctx.hooks if r.event == "pre_llm_call").cb
        # pending empty -> None
        assert fn(session_id="s1", user_message="hello", is_first_turn=True) is None
        # arrange pending state, then turn 1 -> still None
        ctx.state.set("v1", {"pending": ["x-arrived"], "dismissed": [], "lint_summary": {}})
        assert fn(session_id="s1", user_message="hello", is_first_turn=True) is None
        # turn 2 -> advisory with context key, bounded
        out = fn(session_id="s1", user_message="hello", is_first_turn=False)
        assert out["context"].startswith("⚠️ skill-manifest"), out
        assert len(out["context"]) < 900
        assert "x-arrived" in out["context"]

    def test_disabled_zero_io(self, init_module, monkeypatch):
        ctx = FakeCtx()
        ctx.config["disabled"] = True
        init_module.register(ctx)
        fn = next(r for r in ctx.hooks if r.event == "pre_llm_call").cb
        ctx.state.set("v1", {"pending": ["x"], "dismissed": [], "lint_summary": {}})
        assert fn(session_id="s1", user_message="m", is_first_turn=False) is None
        obs = next(r for r in ctx.hooks if r.event == "on_skill_lifecycle").cb
        assert obs(action="created", skill_name="y") is None