"""HERMES-WRITTEN contract test — pinned boundary contract for skill-manifest Phase 1.

Written by the orchestrator (2026-10-16), NOT by the builder. The builder must
NOT modify or delete this file; if it conflicts with BRIEF.md the contract wins.

Encodes the parser and probe contracts from DESIGN_SPEC.md §4.1/§5.2 against
the REAL fixture formats (the skill's template + a copy of the live manifest).
Fails red until manifest_db.py and probe.py exist with the agreed API.
"""
from __future__ import annotations

from helpers import _writable_tmp_base

from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"

try:  # guarded import: missing modules must fail by assertion, not ImportError
    import manifest_db
except ImportError:  # noqa: F811
    manifest_db = None

try:
    import probe  # shared/scripts/probe.py, Phase 1 deliverable 2
except ImportError:  # noqa: F811
    probe = None


@pytest.mark.contract
class TestManifestDbContract:
    def test_modules_exist(self) -> None:
        assert manifest_db is not None, (
            "manifest_db.py missing — Phase 1 not implemented (BRIEF.md deliverable 1)"
        )
        assert probe is not None, (
            "probe.py missing — Phase 1 not implemented (BRIEF.md deliverable 2)"
        )

    def test_template_fixture_skills(self) -> None:
        """The template manifest must parse; the File-column table and audit
        sections must NOT contribute skills (exfil of file.md is the hazard)."""
        doc = manifest_db.load_manifest(FIXTURES / "upstream-manifest-template.md")
        assert "skill-name" in doc.skills
        assert "file.md" not in doc.skills
        assert not any(".md" in s for s in doc.skills)
        assert not any("references/" in s for s in doc.skills)

    def test_live_fixture_skills(self) -> None:
        """The live manifest has drifted column counts, backticked names,
        category prefixes, and a `monid` (top-level) suffix. Prose-only tables
        (audit log) must not leak backticked names like endpoint-probe."""
        doc = manifest_db.load_manifest(FIXTURES / "upstream-manifest-live.md")
        expected = {
            "himalaya",
            "harness-engineering",
            "impeccable",
            "hermes-agent-skill-authoring",
            "xiaohongshu-search",
            "monid",
            "website-scraping",
            "reddit-keyless-search",
        }
        assert doc.skills == expected, (
            "parser must key on the FIRST cell of tables whose header contains "
            "an exact 'Skill' column, normalize paths/suffixes, and exclude "
            "File-column and Audit-log tables"
        )
        assert "endpoint-probe" not in doc.skills
        assert "library-rag" not in doc.skills
        assert "note-taking" not in doc.skills
        assert "scrapling" not in doc.skills  # engine TOOL name, not the skill name

    def test_is_manifested_case_insensitive_and_segment_match(self) -> None:
        doc = manifest_db.load_manifest(FIXTURES / "upstream-manifest-live.md")
        assert manifest_db.is_manifested(doc, "HIMALAYA")
        assert manifest_db.is_manifested(doc, "email/himalaya")
        assert manifest_db.is_manifested(doc, "Monid")
        assert not manifest_db.is_manifested(doc, "totally-unknown-skill")

    def test_missing_manifest_fails_open(self) -> None:
        """A missing/unreadable manifest must never raise — hook callers sit
        inside skill_manage's mutation path."""
        doc = manifest_db.load_manifest(FIXTURES / "definitely-missing.md")
        assert doc.skills == set()
        assert len(doc.errors) >= 1

    def test_load_manifest_text_malformed_fails_open(self) -> None:
        """Malformed markdown must yield an empty-but-valid doc, not an
        exception; prose lines are not tables."""
        doc = manifest_db.load_manifest_text("# no tables here\nplain prose\n")
        assert doc.skills == set()
        assert not doc.errors


@pytest.mark.contract
class TestProbeContract:
    def test_list_skill_names_prunes_excluded_dirs(self) -> None:
        """Only dirs with a direct SKILL.md count; .archive/__pycache__ and
        dot-dirs are pruned at any depth."""
        probe.list_skill_names  # API presence
        with tempfile_dir() as root:
            (root / "email" / "himalaya").mkdir(parents=True)
            (root / "email" / "himalaya" / "SKILL.md").write_text("---\n")
            (root / "solo-skill").mkdir()
            (root / "solo-skill" / "SKILL.md").write_text("---\n")
            (root / ".archive" / "solo-skill").mkdir(parents=True)
            (root / ".archive" / "solo-skill" / "SKILL.md").write_text("---\n")
            (root / "x" / "__pycache__").mkdir(parents=True)
            (root / "x" / "__pycache__" / "SKILL.md").write_text("---\n")
            (root / "no-skills-here").mkdir()
            got = probe.list_skill_names(root)
        assert got == {"himalaya", "solo-skill"}
        # nested dir WITHOUT a direct SKILL.md is not a skill unit
        with tempfile_dir() as root:
            (root / "email").mkdir()
            (root / "README.md").write_text("x")
            assert probe.list_skill_names(root) == set()

    def test_list_skill_names_missing_root(self) -> None:
        """Missing skills_root must fail open with an empty inventory."""
        assert probe.list_skill_names(Path("/nonexistent/root/xyz")) == set()

    def test_compute_pending_arrivals_and_gone(self) -> None:
        probe.compute_pending  # API presence
        current = {"a", "b", "c", "New-Arrival", "dismissed-thing"}
        manifested = {"a"}
        dismissed = {"dismissed-thing"}
        known = {"a", "b", "old-skill"}
        pending, gone = probe.compute_pending(current, manifested, dismissed, known)
        # Orchestrator correction 2026-10-16: per BRIEF rule, pending =
        # current − (manifested ∪ dismissed ∪ known). "c" is on disk but
        # untracked/unknown, so it pends alongside the case-normalized
        # "New-Arrival" → "new-arrival". "b" is known, "a" manifested,
        # "dismissed-thing" dismissed — none of those pend.
        assert pending == {"c", "new-arrival"}
        # known names that vanished from disk are reported for cleanup
        assert gone == {"old-skill"}

    def test_compute_pending_manisfested_new_skill_not_pending(self) -> None:
        """A skill manifested BETWEEN baselines must never pend."""
        pending, gone = probe.compute_pending(
            {"a", "b"}, {"a", "b"}, set(), {"a"}
        )
        assert pending == set()  # b newly manifested → not pending despite !known
        assert gone == set()


def tempfile_dir():
    import tempfile

    class _T:
        def __enter__(self):
            self.t = tempfile.TemporaryDirectory(dir=_writable_tmp_base())
            self.p = Path(self.t.name)
            return self.p

        def __exit__(self, *a):
            self.t.cleanup()
            return False

    return _T()