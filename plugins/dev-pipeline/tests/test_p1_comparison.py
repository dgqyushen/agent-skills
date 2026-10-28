"""Independent outcome-analysis tests for Dev Pipeline P1."""
import importlib.util
import sys
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parent.parent / "benchmarks/compare_runs.py"
spec = importlib.util.spec_from_file_location("dev_pipeline_p1_compare", PATH)
m = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = m
spec.loader.exec_module(m)


def row(task, variant, *, tier="T1", success=True, elapsed=10,
        input_tokens=120, output_tokens=30, regressions=0, rounds=0):
    return dict(task_id=task, variant=variant, tier=tier, success=success,
                elapsed_s=elapsed, input_tokens=input_tokens,
                output_tokens=output_tokens, regressions=regressions,
                review_rounds=rounds)


def test_reports_paired_cost_and_quality_without_claiming_causation():
    entries = [
        row("a", "baseline", elapsed=10, success=False, regressions=2),
        row("a", "pipeline", elapsed=30, input_tokens=250, success=True, rounds=2),
        row("b", "baseline", elapsed=20, success=True, regressions=1),
        row("b", "pipeline", elapsed=40, input_tokens=350, success=True, rounds=1),
        row("missing-baseline", "pipeline", tier="T3"),
    ]
    report = m.summarize(entries)
    assert report["paired_tasks"] == 2
    assert report["unpaired_tasks"] == 1
    t1 = report["tiers"]["T1"]
    assert t1["pipeline"]["success_rate"] == 1
    assert t1["baseline"]["success_rate"] == 0.5
    assert t1["pipeline_minus_baseline"]["mean_elapsed_s"] == 20
    assert t1["pipeline_minus_baseline"]["mean_regressions"] == -1.5
    assert t1["pipeline"]["mean_review_rounds"] == 1.5


def test_pairs_are_scoped_to_tier():
    rows = [row("a", "baseline", tier="T0"), row("a", "pipeline", tier="T1")]
    with pytest.raises(ValueError, match="no paired"):
        m.summarize(rows)


@pytest.mark.parametrize("bad", [
    {"success": 1},
    {"input_tokens": -1},
    {"input_tokens": 1.5},
    {"elapsed_s": -2},
    {"elapsed_s": float("nan")},
    {"elapsed_s": float("inf")},
    {"input_tokens": float("nan")},
    {"review_rounds": float("inf")},
    {"tier": "T9"},
    {"variant": "secret"},
    {"task_id": ""},
])
def test_invalid_metrics_rejected(bad):
    x = row("x", "pipeline")
    x.update(bad)
    with pytest.raises(ValueError):
        m.validate(x)


def test_duplicate_runs_fail_rather_than_double_count():
    with pytest.raises(ValueError, match="duplicate run"):
        m.summarize([row("x", "baseline"), row("x", "baseline"), row("x", "pipeline")])


def test_no_paired_records_fail_closed():
    with pytest.raises(ValueError, match="no paired"):
        m.summarize([row("x", "baseline")])


def test_nan_elapsed_rejected_before_report_json_is_built():
    bad = row("n", "baseline")
    bad.update({"elapsed_s": float("nan")})
    with pytest.raises(ValueError, match="finite"):
        m.summarize([bad, row("n", "pipeline")])


def test_malformed_json_line_reports_line_number(tmp_path):
    p = tmp_path / "runs.jsonl"
    p.write_text(
        '{"task_id":"a","variant":"baseline"}\n{"not json here"}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="line 2"):
        m.read_rows(p)


def test_blank_lines_are_skipped(tmp_path):
    def line_of(task, variant):
        import json as _json
        return _json.dumps(row(task, variant))

    p = tmp_path / "runs.jsonl"
    p.write_text("\n" + line_of("a", "baseline") + "\n   \n"
                 + line_of("a", "pipeline") + "\n", encoding="utf-8")
    report = m.summarize(m.read_rows(p))
    assert report["paired_tasks"] == 1


def test_huge_integer_counts_rejected_by_instrument_cap():
    a = row("a", "baseline")
    a.update({"input_tokens": 10 ** 400})
    with pytest.raises(ValueError, match="instrument maximum"):
        m.summarize([a, row("a", "pipeline")])


def test_cap_boundary_values_render_strict_json():
    """Values at the cap stay float-exact; the rendered report is strict JSON."""
    a = row("a", "baseline")
    a.update({"input_tokens": m.MAX_NUM, "output_tokens": 0})
    b = row("a", "pipeline")
    b.update({"input_tokens": 1, "output_tokens": 1})
    import json as _json
    rendered = m.render(m.summarize([a, b]))
    parsed = _json.loads(rendered)
    assert parsed["tiers"]["T1"]["baseline"]["mean_tokens"] == m.MAX_NUM
    assert "NaN" not in rendered and "Infinity" not in rendered


def test_render_fails_closed_on_non_finite_report_value():
    report = m.summarize([row("a", "baseline"), row("a", "pipeline")])
    report["tiers"]["T1"]["baseline"]["mean_tokens"] = float("inf")
    with pytest.raises(ValueError, match="non-serializable"):
        m.render(report)
