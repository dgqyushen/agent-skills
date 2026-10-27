"""Manifest linter (F12): advisory findings, never a rewriter, never raises."""
from __future__ import annotations

from pathlib import Path

import manifest_lint
import manifest_db

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

WRECKED = """
### Whole skills

| Skill | Source repo | Last sync |
|-------|-------------|-----------|
| `stale-skill` | https://example.com/a | 2026-01-01 |
| `stale-skill` | https://example.com/b | not-a-date |
| `jagged` | only-one |
|  |  | 2026-01-01 |
| --- | --- | --- |
| `ok-skill` | https://example.com/c | 2026-01 |

| Skill | Source repo |
|-------|-------------|

| dangling | no-sep |
"""


def test_f12_lint_template_fixture_has_zero_errors() -> None:
    """The template fixture is well-formed; lint must not raise and has no errors."""
    text = (FIXTURES / "upstream-manifest-template.md").read_text(encoding="utf-8")
    report = manifest_lint.lint_manifest(text)
    assert report.ok is True
    assert not any(f.severity == "error" for f in report.findings)


def test_f12_lint_live_fixture_finds_column_drift() -> None:
    """Live impeccable-row drift is an L2-columns finding; lint never raises."""
    text = (FIXTURES / "upstream-manifest-live.md").read_text(encoding="utf-8")
    report = manifest_lint.lint_manifest(text)
    l2 = [f for f in report.findings if f.check_id == "L2-columns"]
    assert len(l2) >= 1
    assert all(f.severity == "warn" for f in l2)
    assert not any(f.check_id == "L5-date-format" for f in report.findings)


def test_f12_lint_wrecked_manifest_hits_every_check_id() -> None:
    """A synthetic wrecked manifest must trigger each check_id at least once."""
    report = manifest_lint.lint_manifest(
        WRECKED, on_disk={"disk-only", "ok-skill"}
    )
    ids = {f.check_id for f in report.findings}
    assert "L1-structure" in ids
    assert "L2-columns" in ids
    assert "L3-duplicate" in ids
    assert "L4-stale-disk" in ids
    assert "L4-untracked" in ids
    assert "L5-date-format" in ids
    assert "L6-empty-required" in ids
    assert report.ok is False


def test_f12_lint_garbage_does_not_raise() -> None:
    """Garbage input still yields a report; the linter never raises."""
    report = manifest_lint.lint_manifest(None)  # type: ignore[arg-type]
    assert hasattr(report, "findings")
    assert isinstance(report.ok, bool)
    report2 = manifest_lint.lint_manifest("")
    assert report2.ok is True


def test_f12_lint_report_shape() -> None:
    """LintFinding carries check_id, severity, line, message; ok tracks errors."""
    report = manifest_lint.lint_manifest("| dangling |\n")
    assert report.findings
    f = report.findings[0]
    assert f.check_id
    assert f.severity in {"error", "warn", "info"}
    assert isinstance(f.line, int)
    assert f.message
    assert report.ok is (not any(x.severity == "error" for x in report.findings))


def test_f12_same_key_in_different_tables_is_not_duplicate() -> None:
    """Layer 1 + Layer 2 may share a key; L3 is per-table only."""
    text = (
        "| Skill | Source repo |\n"
        "|-------|-------------|\n"
        "| `shared` | https://example.com |\n"
        "\n"
        "| Skill | Repo category |\n"
        "|-------|---------------|\n"
        "| `shared` | cat/ |\n"
    )
    report = manifest_lint.lint_manifest(text)
    assert not any(f.check_id == "L3-duplicate" for f in report.findings)


def test_f12_l4_skipped_when_on_disk_is_none() -> None:
    """Disk cross-checks run only when on_disk is supplied."""
    text = (
        "| Skill | Source repo |\n"
        "|-------|-------------|\n"
        "| `only-on-paper` | https://example.com |\n"
    )
    report = manifest_lint.lint_manifest(text)
    assert not any(f.check_id.startswith("L4") for f in report.findings)


def test_f12_reuses_manifest_db_normalization() -> None:
    """Linter keys must match manifest_db so parser and lint agree on a name."""
    text = (
        "| Skill | Source repo | Last sync |\n"
        "|-------|-------------|----------|\n"
        "| `email/himalaya` (see PORT_NOTES.md) | https://ex.com | 2026-01-01 |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    report = manifest_lint.lint_manifest(text, on_disk={"himalaya"})
    assert "himalaya" in doc.skills
    assert not any(f.check_id == "L4-stale-disk" for f in report.findings)


def test_f14_drifted_row_has_l2_not_l5() -> None:
    """Column-drifted rows are L2 only; L5 must not read a date from the wrong cell."""
    text = (
        "| Skill | A | B | Last sync | Status |\n"
        "|-------|---|---|-----------|--------|\n"
        "| `jagged` | a | ✅ current | leftover |\n"
        "| `ok-skill` | a | b | not-a-date | ok |\n"
        "| `dated` | a | b | 2026-01-01 | ok |\n"
    )
    report = manifest_lint.lint_manifest(text)
    by_line = {}
    for f in report.findings:
        by_line.setdefault(f.line, []).append(f.check_id)
    jagged_line = None
    bad_date_line = None
    good_date_line = None
    for table in manifest_db.scan_tables(text).tables:
        for row, rln in zip(table.data, table.data_lines):
            key = row[0] if row else ""
            if "jagged" in key:
                jagged_line = rln
            elif "ok-skill" in key:
                bad_date_line = rln
            elif "dated" in key:
                good_date_line = rln
    assert jagged_line is not None
    assert "L2-columns" in by_line.get(jagged_line, [])
    assert "L5-date-format" not in by_line.get(jagged_line, [])
    assert bad_date_line is not None
    assert "L5-date-format" in by_line.get(bad_date_line, [])
    assert good_date_line is not None
    assert "L5-date-format" not in by_line.get(good_date_line, [])


def _l5_sync_table(*dates: str) -> str:
    rows = "\n".join(f"| `s{i}` | x | {d} |" for i, d in enumerate(dates))
    return (
        "| Skill | Source repo | Last sync |\n"
        "|-------|-------------|----------|\n"
        f"{rows}\n"
    )


def test_f15_impossible_calendar_dates_rejected() -> None:
    """After the YYYY-MM-DD shape check, L5 must reject dates strptime cannot parse."""
    text = _l5_sync_table(
        "2026-13-01",
        "2026-01-32",
        "2026-13-99",
        "2026-02-30",
        "2026-02-29",
        "2025-02-29",
        "2024-02-29",
        "2026-01-01",
        "2026-02",
    )
    report = manifest_lint.lint_manifest(text)
    flagged = {
        row[2]
        for table in manifest_db.scan_tables(text).tables
        for row, rln in zip(table.data, table.data_lines)
        if any(
            f.check_id == "L5-date-format" and f.line == rln
            for f in report.findings
        )
    }
    assert "2026-13-01" in flagged
    assert "2026-01-32" in flagged
    assert "2026-13-99" in flagged
    assert "2026-02-30" in flagged
    assert "2025-02-29" in flagged
    assert "2026-02-29" in flagged
    assert "2024-02-29" not in flagged
    assert "2026-01-01" not in flagged
    assert "2026-02" not in flagged
    assert manifest_lint._ok_sync_date("2024-02-29") is True
    assert manifest_lint._ok_sync_date("2026-02-29") is False
    assert manifest_lint._ok_sync_date("2025-02-29") is False
    assert manifest_lint._ok_sync_date("2026-13-99") is False
    assert manifest_lint._ok_sync_date("2026-02-30") is False
