"""Round-2 confirmation regressions: Codex C2 + Opus C2 residuals.

Standalone imports (same spec-load pattern as test_advisor.py) so the file
runs under any pytest rootdir without a package install.
"""
import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent

_spec = importlib.util.spec_from_file_location("advisor", PLUGIN_DIR / "advisor.py")
advisor = importlib.util.module_from_spec(_spec)
sys.modules["advisor"] = advisor
_spec.loader.exec_module(advisor)


class State:
    def __init__(self):
        self._d = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def set(self, key, value):
        self._d[key] = value


class Ctx:
    def __init__(self, tmpdir, settings=None):
        self.state = State()
        self.hermes_home = tmpdir
        self._settings = {"enabled": True} if settings is None else dict(settings)

    def get_config(self, name):
        # Real core contract: relative keys; reserved roots rejected.
        if name.split(".")[0].lower() in ("model", "plugins", "security", "settings"):
            raise ValueError("rejected: reserved root")
        return self._settings.get(name)


def _settings(tmpdir, **over):
    return {
        "enabled": True,
        "probe_root": str(pathlib.Path(tmpdir) / "skill-probes"),
        "ttl_minutes": 30,
        **over,
    }


def test_render_truncates_at_cap_exactly():
    """Codex C2: old math sliced to 900 and appended the ellipsis = 901.
    Result must never exceed INJECT_HARD_CAP, and be exactly the cap when
    truncation fires (64-char name, long evidence)."""
    name = "a" * 64
    evidence = "found: " + "x" * 2000
    text = advisor.render(name, evidence)
    assert len(text) == advisor.INJECT_HARD_CAP
    assert text.endswith("…")


def test_render_short_text_untouched():
    text = advisor.render("ascii-art", "missing: create probe dirs")
    assert len(text) < advisor.INJECT_HARD_CAP and not text.endswith("…")


def test_cycle_scan_count_capped_and_bounded(tmp_path):
    """Codex probe: many cycle dirs must never materialize the full glob
    set. 30 fresh cycles -> report caps at CYCLE_MAX_COUNT without error."""
    root = pathlib.Path(tmp_path) / "probes"
    for i in range(30):
        d = root / "ascii-art" / f"cycle-{i}"
        d.mkdir(parents=True)
        (d / "probe.md").write_text("p")
        (d / "selection.md").write_text("s")
    status = advisor.probe_evidence(str(root / "ascii-art"))
    assert status.startswith("found: 10 cycle(s)"), status


def test_cycle_scan_max_entries_bounds_visited(tmp_path):
    """Entries visited are bounded by CYCLE_SCAN_MAX_ENTRIES: 30 selection
    dirs without probe.md terminate quickly and report missing."""
    root = pathlib.Path(tmp_path) / "probes"
    for i in range(30):
        d = root / "ascii-art" / f"tag-{i}"
        d.mkdir(parents=True)
        (d / "selection.md").write_text("s")  # no probe.md -> not a cycle
    status = advisor.probe_evidence(str(root / "ascii-art"))
    assert status.startswith("missing"), status


def test_cycle_scan_consumes_at_most_the_cap(tmp_path, monkeypatch):
    """Codex C3: with the cap unreachable via cycles, the SECOND glob pattern
    must not restart scanning with fresh headroom. Count consumed entries by
    instrumenting Path.glob — the shared visited bound caps BOTH patterns."""
    root = pathlib.Path(tmp_path) / "probes" / "ascii-art"
    for i in range(60):
        d = root / f"tag-{i}"
        d.mkdir(parents=True)
        (d / "selection.md").write_text("s")  # no probe.md anywhere
    consumed = {"n": 0}
    original_glob = pathlib.Path.glob

    def counting_glob(self, pattern):
        for p in original_glob(self, pattern):
            consumed["n"] += 1
            yield p

    monkeypatch.setattr(pathlib.Path, "glob", counting_glob)
    status = advisor.probe_evidence(str(root))
    assert status.startswith("missing"), status
    assert consumed["n"] <= advisor.CYCLE_SCAN_MAX_ENTRIES, consumed["n"]


def test_markers_string_wraps_whole(tmp_path):
    """Opus C2: a markers STRING must be one marker, not char-split
    (old tuple('## X') made every '#'/slash match behavioral)."""
    s = _settings(str(tmp_path), markers="## Procedure")
    args = {"action": "patch", "name": "ascii-art",
            "old_text": "a b c", "new_text": "d e f"}  # short, has spaces
    assert advisor.is_behavioral_edit("skill_manage", args, s) is None
    hit = advisor.is_behavioral_edit(
        "skill_manage", dict(args, new_text="## Procedure\nbody"), s)
    assert hit is not None


def test_ttl_zero_and_invalid_fall_back(tmp_path):
    class Raw:
        def __init__(self, settings):
            self._s = dict(settings)
            self._s.setdefault("enabled", True)

        def get_config(self, name):
            # Real core contract: relative keys; reserved roots rejected.
            if name.split(".")[0].lower() in ("model", "plugins", "security", "settings"):
                raise ValueError("rejected: reserved root")
            return self._s.get(name)

    ctx = Raw({"ttl_minutes": 0})
    assert advisor.load_settings(ctx)["ttl_minutes"] == advisor.DEFAULT_TTL_MINUTES
    ctx2 = Raw({})
    assert advisor.load_settings(ctx2)["ttl_minutes"] == advisor.DEFAULT_TTL_MINUTES
    ctx3 = Raw({"ttl_minutes": True})
    assert advisor.load_settings(ctx3)["ttl_minutes"] == advisor.DEFAULT_TTL_MINUTES


def test_plugin_path_skill_md_stays_silent(tmp_path):
    """Opus C2: SKILL.md under a plugins/ dir = namespaced plugin skill."""
    s = _settings(str(tmp_path))
    args = {"path": "/root/.hermes/plugins/cos/skills/daily-briefing/SKILL.md",
            "old_string": "middle", "new_string": "## Pitfalls\nbody"}
    assert advisor.is_behavioral_edit("patch", args, s) is None


def test_no_state_write_when_queue_clean(tmp_path):
    """Opus C2: injector must not write state on every clean turn."""
    ctx = Ctx(str(tmp_path))
    calls = {"n": 0}
    original_set = ctx.state.set

    def counting(key, value):
        calls["n"] += 1
        return original_set(key, value)

    ctx.state.set = counting
    assert advisor.on_pre_llm_call(ctx, session_id="s", user_message="x",
                                   is_first_turn=False) is None
    assert calls["n"] == 0  # clean turn, nothing to reclaim: no write
    advisor.observe_edit(ctx, "ascii-art", str(pathlib.Path(tmp_path) / "p"))
    assert calls["n"] == 1  # observer wrote once
    assert advisor.on_pre_llm_call(ctx, session_id="s", user_message="x",
                                   is_first_turn=False) is not None
    assert calls["n"] == 2  # served the item: one write
    assert advisor.on_pre_llm_call(ctx, session_id="s", user_message="x",
                                   is_first_turn=False) is None
    assert calls["n"] == 2  # empty again: no write