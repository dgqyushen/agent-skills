"""Tests for the contrastive-refinement helper script.

Contract under test:
- assign_ids() maps every label to a unique anonymous id, deterministically
  (same inputs -> same mapping) with a seeded shuffle, matching the Harness^2
  blinded-selection convention: labels sorted, then a seeded shuffle.
- check_lesson() accepts a lesson only when cue, lesson, evidence and
  confidence are all present and non-empty — the evidence gate. A lesson
  without evidence must fail loudly: that is the whole point of the port.
"""

import importlib.util
import pathlib
import subprocess
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "contrast.py"

spec = importlib.util.spec_from_file_location("contrast", SCRIPT)
contrast = importlib.util.module_from_spec(spec)
spec.loader.exec_module(contrast)


# --- assign_ids: determinism + coverage ------------------------------------


def test_assign_ids_is_deterministic():
    labels = ["agg1", "agg2", "base1", "dom1", "inv1"]
    first = contrast.assign_ids(labels, task_id="t1", k=1, mode="agg")
    second = contrast.assign_ids(labels, task_id="t1", k=1, mode="agg")
    assert first == second


def test_assign_ids_different_seed_may_differ_but_stays_valid():
    """A different (task, k, mode) seed must still give a complete mapping."""
    labels = ["inv1", "dom1", "agg1"]
    other = contrast.assign_ids(labels, task_id="t2", k=2, mode="seq")
    assert sorted(other.values()) == sorted(labels)


def test_assign_ids_is_bijective():
    labels = ["inv1", "dom1", "base1", "c1", "agg1", "c2"]
    mapping = contrast.assign_ids(labels)
    assert len(mapping) == len(labels)
    assert sorted(mapping.values()) == sorted(labels)
    ids = list(mapping)
    assert len(set(ids)) == len(ids)


def test_assign_ids_format_a_then_aa():
    ids = contrast.assign_ids([f"l{i}" for i in range(30)])
    assert all(len(i) in (1, 2) for i in ids)
    assert all(i.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ") == "" for i in ids)
    # 28th id is AB (26 letters, then AA, AB)
    assert "AB" in set(ids)


def test_assign_ids_rejects_empty():
    with pytest.raises(ValueError):
        contrast.assign_ids([])


def test_assign_ids_rejects_duplicates():
    with pytest.raises(ValueError):
        contrast.assign_ids(["inv1", "inv1"])


# --- check_lesson: the evidence gate ---------------------------------------


COMPLETE = (
    "cue: When a ledger includes credits and debits\n"
    "lesson: Include both signs when summing amounts.\n"
    "evidence: Removing the positive-only filter changed the probe total.\n"
    "confidence: high for this observed contrast\n"
)


def test_check_lesson_accepts_complete_lesson():
    ok, problems = contrast.check_lesson(COMPLETE)
    assert ok, problems


def test_check_lesson_rejects_missing_evidence():
    body = COMPLETE.replace("evidence: Removing", "xignored: Removing")
    ok, problems = contrast.check_lesson(body)
    assert not ok
    assert any("evidence" in p.lower() for p in problems)


def test_check_lesson_rejects_empty_evidence():
    body = COMPLETE.replace(
        "evidence: Removing the positive-only filter changed the probe total.",
        "evidence:",
    )
    ok, problems = contrast.check_lesson(body)
    assert not ok


def test_check_lesson_rejects_trivial_text():
    ok, problems = contrast.check_lesson("")
    assert not ok


# --- new behaviors from the review gate -------------------------------------


def test_assign_ids_strips_labels_before_dedup():
    """CLI strips labels; the API must normalize identically (reviewer R1)."""
    with pytest.raises(ValueError):
        contrast.assign_ids(["a ", "a"])


def test_assign_ids_rejects_blank_labels():
    with pytest.raises(ValueError):
        contrast.assign_ids(["a", "   "])


def test_assign_ids_does_not_mutate_input():
    labels = ["b", "a", "c"]
    contrast.assign_ids(labels, task_id="t")
    assert labels == ["b", "a", "c"]


def test_anon_id_exact_sequence():
    seq = [contrast._anon_id(i) for i in range(28)]
    assert seq[:3] == ["A", "B", "C"]
    assert seq[25] == "Z"
    assert seq[26] == "AA"
    assert seq[27] == "AB"


def test_cli_ids_prints_generation_order():
    """28 labels must print A,B,C..Z,AA,AB — generation order, not sorted (R2)."""
    labels = ",".join(f"l{i}" for i in range(28))
    proc = _cli("ids", "--labels", labels)
    assert proc.returncode == 0
    printed_ids = [line.split("\t")[0] for line in proc.stdout.strip().splitlines()]
    expected = [contrast._anon_id(i) for i in range(28)]
    assert printed_ids == expected


def test_cli_ids_rejects_duplicates_with_exit_1():
    proc = _cli("ids", "--labels", "a,a")
    assert proc.returncode == 1
    assert "error" in (proc.stdout + proc.stderr).lower()


def test_cli_ids_rejects_empty_with_exit_1():
    proc = _cli("ids", "--labels", "")
    assert proc.returncode == 1


def test_check_lesson_rejects_placeholder_evidence():
    for placeholder in ("n/a", "-", "none", "TODO"):
        body = f"cue: x\nlesson: y\nevidence: {placeholder}\nconfidence: high\n"
        ok, problems = contrast.check_lesson(body)
        assert not ok, placeholder
        assert any("evidence" in p.lower() for p in problems)


def test_check_lesson_tolerates_bullets_and_bold():
    body = (
        "- **cue**: When a ledger mixes credits and debits\n"
        "- **lesson**: Sum both signs.\n"
        "- **evidence**: The base total omitted credits.\n"
        "- **confidence**: high for this contrast\n"
    )
    ok, problems = contrast.check_lesson(body)
    assert ok, problems


# --- CLI contract -----------------------------------------------------------


def _cli(*args, stdin=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_cli_ids_prints_mapping():
    proc = _cli("ids", "--labels", "inv1,dom1,base1", "--task", "t1")
    assert proc.returncode == 0
    assert "inv1" in proc.stdout and "dom1" in proc.stdout and "base1" in proc.stdout


def test_cli_lesson_ok_on_complete():
    proc = _cli("lesson-check", stdin=COMPLETE)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_cli_lesson_fails_without_evidence():
    body = COMPLETE.replace("evidence: Removing", "note: Removing")
    proc = _cli("lesson-check", stdin=body)
    assert proc.returncode == 1
    assert "evidence" in (proc.stdout + proc.stderr).lower()


# --- round 2: markdown-wrapped and template values (R2 gate) -----------------


def _lesson_with(evidence):
    return (
        "cue: A ledger with credits and debits\n"
        "lesson: Sum both signs.\n"
        f"evidence: {evidence}\n"
        "confidence: high for this contrast\n"
    )


def test_check_lesson_rejects_markdown_wrapped_placeholders():
    for ev in ("**n/a**", "`n/a`", "*none*", "N/A.", "** todo **"):
        ok, problems = contrast.check_lesson(_lesson_with(ev))
        assert not ok, ev
        assert any("evidence" in pl.lower() for pl in problems)


def test_check_lesson_rejects_bold_wrapped_colon_empty():
    """Opus R3 P1: pin the fix itself — real cue, so the failure must come
    from the EMPTY bold-wrapped evidence, not from placeholder cue text."""
    empty = (
        "cue: A ledger with both signs\n"
        "lesson: Sum both signs.\n"
        "- **evidence:**\n"
        "confidence: high\n"
    )
    ok, problems = contrast.check_lesson(empty)
    assert not ok
    assert any("evidence" in pr.lower() for pr in problems)


def test_check_lesson_rejects_whole_template_values():
    """A value that is ONLY a `<...>` tag is rejected; a value that merely
    CONTAINS a tag (e.g. "When <situation>") passes — the gate is syntactic
    and the contrast step judges quality (documented limit)."""
    body = (
        "cue: When <situation>\n"
        "lesson: <the rule>\n"
        "evidence: <the observed contrast from the probe run>\n"
        "confidence: <scope of validity>\n"
    )
    ok, problems = contrast.check_lesson(body)
    assert not ok
    assert len(problems) == 3
    assert not any("cue" in p for p in problems)


def test_check_lesson_placeholder_gate_on_all_fields():
    """A placeholder as the WHOLE value of any field is rejected."""
    values = dict(
        cue="A ledger with credits",
        lesson="Sum both signs.",
        evidence="The base total omitted credits.",
        confidence="high",
    )
    for field in contrast.LESSON_FIELDS:
        vals = dict(values)
        vals[field] = "n/a"
        body = "".join(f"{f}: {v}\n" for f, v in vals.items())
        ok, problems = contrast.check_lesson(body)
        assert not ok, field


def test_check_lesson_accepts_sentences_containing_placeholder_words():
    """Only the WHOLE value counts as a placeholder; sentences stay valid."""
    body = _lesson_with("The probe output mentioned none of the required columns.")
    assert contrast.check_lesson(body)[0]


def test_check_lesson_accepts_bold_wrapped_real_value():
    body = _lesson_with("**The base total omitted the credits.**")
    assert contrast.check_lesson(body)[0]


def test_check_lesson_accepts_evidence_built_from_tags():
    """Codex R3 boundary probe: meaningful evidence assembled from <tag>
    spans must PASS — the template rule covers only ONE bare tag."""
    body = (
        "cue: A ledger with both signs\n"
        "lesson: Sum both signs.\n"
        "evidence: <baseline> omitted credits; <candidate> included credits</candidate>\n"
        "confidence: high\n"
    )
    ok, problems = contrast.check_lesson(body)
    assert ok, problems


def test_check_lesson_rejects_single_bare_template_tag():
    body = "cue: x\nlesson: y\nevidence: <the observed contrast>\nconfidence: high\n"
    ok, problems = contrast.check_lesson(body)
    assert not ok
    assert any("evidence" in p.lower() for p in problems)


def test_cli_ids_rejects_blank_in_middle():
    """CLI must not silently drop empty entries (Codex regression probe)."""
    proc = _cli("ids", "--labels", "base,,dom")
    assert proc.returncode == 1
    assert "error" in (proc.stdout + proc.stderr).lower()


def test_cli_ids_labels_are_stripped_not_dropped():
    """' base ' and 'base' are the same label -> duplicate error, exit 1."""
    proc = _cli("ids", "--labels", " base ,base")
    assert proc.returncode == 1
