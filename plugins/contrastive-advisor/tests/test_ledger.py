"""Fire-ledger tests for contrastive-advisor (added 2026-10-26, v0.1.1).

Hazard being defended: the ledger is the ONLY durable record of delivered
advisories (fires are in-band; no gateway log line exists). A swallowed write
failure, a row written on a silent hook, or a crash during the atomic
replace would make fires unprovable at the trial review.

Same rules as test_advisor.py: real core contracts, reviewer sandboxes may
raise OSError Errno 30 on /root — count only assertion failures.
"""

import importlib.util
import json
import pathlib
import sys
from datetime import datetime, timedelta

HERE = pathlib.Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent

spec = importlib.util.spec_from_file_location("advisor", PLUGIN_DIR / "advisor.py")
advisor = importlib.util.module_from_spec(spec)
sys.modules["advisor"] = advisor
spec.loader.exec_module(advisor)


def _settings(tmpdir, **over):
    s = {
        "enabled": True,
        "probe_root": str(pathlib.Path(tmpdir) / "skill-probes"),
        "ttl_minutes": 30,
        "ledger_enabled": True,
    }
    s.update(over)
    return s


def _make_cycle(root: pathlib.Path, skill: str, tag: str):
    d = root / "skill-probes" / skill / tag
    d.mkdir(parents=True)
    (d / "probe.md").write_text("probe")
    (d / "selection.md").write_text("selection")
    return d


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
        self._settings = dict(settings or {"enabled": True})

    def get_config(self, name):
        return self._settings.get(name)


def test_record_fire_appends_row(tmp_path):
    s = _settings(str(tmp_path))
    advisor.record_fire(s, "ascii-art", "skill_manage", "sess-1",
                        "missing: create .../", "advisory text")
    lines = (tmp_path / "skill-probes" / "fire-ledger.jsonl").read_text().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["skill"] == "ascii-art"
    assert row["tool"] == "skill_manage"
    assert row["session_id"] == "sess-1"
    assert row["evidence"].startswith("missing")
    assert row["answered_at"] is None
    assert row["advisory"] == "advisory text"
    assert "fired_at" in row


def test_ledger_disabled_writes_nothing(tmp_path):
    s = _settings(str(tmp_path), ledger_enabled=False)
    advisor.record_fire(s, "ascii-art", "skill_manage", "s", "missing", "ad")
    assert not (tmp_path / "skill-probes" / "fire-ledger.jsonl").exists()


def test_evidence_found_at_fire_marks_answered(tmp_path):
    s = _settings(str(tmp_path))
    advisor.record_fire(s, "ascii-art", "skill_manage", "s",
                        "found: 1 cycle(s), latest 2026-10-25", "ad")
    row = json.loads(
        (tmp_path / "skill-probes" / "fire-ledger.jsonl").read_text().splitlines()[0]
    )
    assert row["answered_at"] is not None


def test_later_fire_closes_prior_open_row(tmp_path):
    """A cycle that appeared AFTER a prior open fire closes that row on the
    next fire for the same skill. A cycle older than the fire must NOT."""
    s = _settings(str(tmp_path))
    advisor.record_fire(s, "ascii-art", "skill_manage", "s1", "missing: ...", "ad-1")
    # an OLD cycle (before the fire): must not close
    old_cycle = _make_cycle(tmp_path, "ascii-art", "20260101-old")
    old_time = datetime(2026, 1, 1).timestamp()
    import os
    os.utime(old_cycle / "selection.md", (old_time, old_time))
    advisor.record_fire(s, "ascii-art", "skill_manage", "s2", "missing: ...", "ad-2")
    rows = [json.loads(l) for l in
            (tmp_path / "skill-probes" / "fire-ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["answered_at"] is None  # old cycle predates the fire
    # a NEW cycle (after the fire) closes row 0 when row 3 appends
    _make_cycle(tmp_path, "ascii-art", "20261026-answer")
    advisor.record_fire(s, "ascii-art", "skill_manage", "s3", "missing: ...", "ad-3")
    rows = [json.loads(l) for l in
            (tmp_path / "skill-probes" / "fire-ledger.jsonl").read_text().splitlines()]
    assert rows[0]["answered_at"] is not None
    assert rows[0]["cycle"].endswith("20261026-answer")
    assert rows[1]["answered_at"] is not None  # same skill, also now answerable
    assert rows[2]["answered_at"] is None  # newest fire, not yet answered


def test_malformed_ledger_lines_tolerated(tmp_path):
    p = tmp_path / "skill-probes"
    p.mkdir(parents=True)
    (p / "fire-ledger.jsonl").write_text(
        'not json at all\n{"skill": "old-skill", "fired_at": "2026-10-20T09:00:00"}\n\n'
    )
    s = _settings(str(tmp_path))
    advisor.record_fire(s, "ascii-art", "skill_manage", "s", "missing", "ad")
    rows = [json.loads(l) for l in (p / "fire-ledger.jsonl").read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["skill"] == "old-skill"  # malformed lines dropped, good rows kept


def test_missing_probe_root_creates_it(tmp_path):
    s = _settings(str(tmp_path))
    advisor.record_fire(s, "ascii-art", "skill_manage", "s", "missing", "ad")
    assert (tmp_path / "skill-probes" / "fire-ledger.jsonl").is_file()


def test_pending_advisory_records_fire(tmp_path):
    """End-to-end: observe -> inject writes exactly one ledger row and keeps
    the advisory return contract {\"context\": str}."""
    root = tmp_path
    ctx = Ctx(str(root))
    s = advisor.load_settings(ctx)  # probe_root defaults to <home>/skill-probes
    advisor.observe_edit(ctx, "wiki-curator-ops", str(root / "skill-probes" / "wiki-curator-ops"),
                         tool="skill_manage", session_id="sess-9")
    out = advisor.pending_advisory(ctx, s)
    assert isinstance(out, dict) and isinstance(out["context"], str)
    ledger = root / "skill-probes" / "fire-ledger.jsonl"
    rows = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["skill"] == "wiki-curator-ops"
    # inject-once: a second turn must NOT add another row
    advisor.pending_advisory(ctx, s)
    rows = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert len(rows) == 1