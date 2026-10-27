"""Shared pytest hooks for skill-manifest.

Import paths come from pytest.ini ``pythonpath`` when present. A repo-root
pytest run (CI) provides no pythonpath for the plugin, so this conftest also
puts the plugin dir and shared/scripts on sys.path — bare imports of the
loader modules must resolve in both layouts.
"""
from __future__ import annotations

import os
import shutil
import sys

import pytest


def _ensure_import_paths() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    plugin_dir = os.path.dirname(here)
    shared_scripts = os.path.join(plugin_dir, "shared", "scripts")
    for p in (here, plugin_dir, shared_scripts):
        if p not in sys.path:
            sys.path.insert(0, p)


_ensure_import_paths()


def pytest_configure(config) -> None:
    config.addinivalue_line(
        "markers", "contract: pinned orchestrator boundary contract"
    )


def _isolate_session_cache() -> None:
    import hooks

    hooks._session_signatures.clear()
    hooks._sessions_loaded = False


def _skills_root_is_explicit(ctx) -> bool:
    try:
        raw = ctx.get_config("skills_root", "")
    except Exception:
        return False
    if isinstance(raw, os.PathLike) and not isinstance(raw, str):
        return True
    return isinstance(raw, str) and bool(raw.strip())


@pytest.fixture(autouse=True)
def _default_skills_root_before_ensure(tmp_path, monkeypatch):
    """A test that leaves skills_root unset must not lock under the real Hermes home.

    The default is applied at the start of ensure-cron, before the lock path
    is resolved. A test that sets skills_root keeps that path.
    """
    import hooks

    real = hooks.ensure_cron
    default_root = str(tmp_path / "skills")

    def ensure_cron(ctx=None):
        target = hooks._CTX if ctx is None else ctx
        if target is not None and not _skills_root_is_explicit(target):
            config = getattr(target, "config", None)
            if isinstance(config, dict):
                config["skills_root"] = default_root
        return real(ctx)

    monkeypatch.setattr(hooks, "ensure_cron", ensure_cron)


@pytest.fixture(autouse=True)
def _stub_import_cron(monkeypatch):
    """Keep session-start tests off the host cron store.

    A test that patches ``hooks._import_cron`` in its body runs after this
    fixture and replaces the stub for that test only.
    """
    import hooks

    monkeypatch.setattr(hooks, "_import_cron", lambda: None)
    _isolate_session_cache()
    yield
    _isolate_session_cache()
