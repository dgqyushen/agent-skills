"""Probe inventory and pending/gone arithmetic. Each test names the hazard."""
from __future__ import annotations

from helpers import _writable_tmp_base

import tempfile
from pathlib import Path

import probe


def _root():
    class _T:
        def __enter__(self):
            self.t = tempfile.TemporaryDirectory(dir=_writable_tmp_base())
            self.p = Path(self.t.name)
            return self.p

        def __exit__(self, *a):
            self.t.cleanup()
            return False

    return _T()


def _skill(root: Path, rel: str) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\n", encoding="utf-8")


def test_list_skill_names_leaf_dir_with_skill_md() -> None:
    """Only a directory that directly contains SKILL.md is a skill unit."""
    with _root() as root:
        _skill(root, "email/himalaya")
        _skill(root, "solo-skill")
        (root / "email").mkdir(exist_ok=True)
        (root / "no-skills-here").mkdir()
        (root / "README.md").write_text("x", encoding="utf-8")
        got = probe.list_skill_names(root)
    assert got == {"himalaya", "solo-skill"}


def test_list_skill_names_prunes_excluded_and_dot_dirs() -> None:
    """Manual copies into .archive/__pycache__/.obsidian must not invent arrivals."""
    with _root() as root:
        _skill(root, "kept")
        _skill(root, ".archive/solo-skill")
        _skill(root, "x/__pycache__/hidden")
        _skill(root, ".curator_backups/old")
        _skill(root, ".hub/bundled")
        _skill(root, ".pytest_cache/ghost")
        _skill(root, ".obsidian/notes")
        got = probe.list_skill_names(root)
    assert got == {"kept"}


def test_list_skill_names_prunes_nested_inside_excluded() -> None:
    """A SKILL.md under a pruned ancestor is still inside a pruned tree."""
    with _root() as root:
        _skill(root, ".archive/nested/deep/skill")
        got = probe.list_skill_names(root)
    assert got == set()


def test_list_skill_names_missing_root_fails_open() -> None:
    """A missing skills_root must not raise from a session-start probe."""
    assert probe.list_skill_names(Path("/nonexistent/root/xyz")) == set()


def test_list_skill_names_lowercases_leaf() -> None:
    """Leaf names keep original case; compute_pending still lowercases for match."""
    with _root() as root:
        _skill(root, "Email/Himalaya")
        got = probe.list_skill_names(root)
    assert got == {"Himalaya"}


def test_list_skill_names_root_itself_excluded() -> None:
    """Walking .archive as the root must not report archived skills as live."""
    with _root() as outer:
        archived = outer / ".archive"
        _skill(archived, "ghost")
        assert probe.list_skill_names(archived) == set()


def test_compute_pending_new_arrival_only() -> None:
    """Already-known and manifested names must not re-pend every session."""
    pending, gone = probe.compute_pending(
        current={"a", "b", "c", "New-Arrival", "dismissed-thing"},
        manifested={"a"},
        dismissed={"dismissed-thing"},
        known={"a", "b", "old-skill"},
    )
    assert pending == {"c", "new-arrival"}
    assert gone == {"old-skill"}


def test_compute_pending_manifested_between_baselines_not_pending() -> None:
    """A skill manifested since the last baseline must never nag."""
    pending, gone = probe.compute_pending(
        {"a", "b"}, {"a", "b"}, set(), {"a"}
    )
    assert pending == set()
    assert gone == set()


def test_compute_pending_case_insensitive_on_all_sets() -> None:
    """Case drift between disk, manifest, and state would double-count arrivals."""
    pending, gone = probe.compute_pending(
        current={"Foo", "Bar"},
        manifested={"FOO"},
        dismissed=set(),
        known={"bar", "VANISHED"},
    )
    assert pending == set()
    assert gone == {"vanished"}


def test_compute_pending_empty_sets() -> None:
    """Idle probe with no skills must stay silent."""
    pending, gone = probe.compute_pending(set(), set(), set(), set())
    assert pending == set()
    assert gone == set()


def test_excluded_dir_names_constant() -> None:
    """Callers prune by this set; shrinking it would resurface archived skills."""
    assert probe.EXCLUDED_DIR_NAMES == {
        ".archive",
        ".curator_backups",
        "__pycache__",
        ".hub",
        ".pytest_cache",
    }


def test_f5_symlinked_skill_directory_is_found() -> None:
    """Per-skill symlink installs must surface the symlink leaf as a skill."""
    with _root() as root:
        _skill(root, "real/sub")
        linked = root / "linked"
        linked.mkdir()
        (linked / "skill-dir").symlink_to(root / "real" / "sub")
        got = probe.list_skill_names(root)
    assert "sub" in got
    assert "skill-dir" in got


def test_f5_symlink_cycle_does_not_hang() -> None:
    """A directory symlink cycle must not infinite-loop the walk."""
    with _root() as root:
        _skill(root, "kept")
        a = root / "loop_a"
        b = root / "loop_b"
        a.mkdir()
        b.mkdir()
        (a / "to_b").symlink_to(b)
        (b / "to_a").symlink_to(a)
        got = probe.list_skill_names(root)
    assert "kept" in got


def test_f5b_dot_named_root_is_walked() -> None:
    """The leading-dot prune applies to children, not a caller-chosen root name."""
    with _root() as outer:
        root = outer / ".skills-root"
        _skill(root, "visible-skill")
        got = probe.list_skill_names(root)
    assert "visible-skill" in got


def test_f5b_excluded_root_name_still_empty() -> None:
    """Walking .archive as the root still returns empty (excluded set)."""
    with _root() as outer:
        archived = outer / ".archive"
        _skill(archived, "ghost")
        assert probe.list_skill_names(archived) == set()


def test_f11_case_collision_keeps_first_seen() -> None:
    """Two leaves that collide case-insensitively keep the first-seen spelling."""
    with _root() as root:
        _skill(root, "a/Widget")
        _skill(root, "b/widget")
        got = probe.list_skill_names(root)
    assert len(got) == 1
    assert next(iter(got)).lower() == "widget"
    # Deterministic walk (sorted dirnames) visits a/ before b/, so Widget wins.
    assert got == {"Widget"}


def test_retired_archive_dirs_are_not_skills(tmp_path):
    """No-trim archive convention: retired-* dirs never enter the skill list."""
    from pathlib import Path
    import probe

    root = tmp_path / "skills"
    real = root / "agent-ops" / "real-skill" / "references" / "retired-skill-md-2026-10-archive"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("---\nname: real-skill\n")
    (root / "agent-ops" / "real-skill" / "SKILL.md").write_text("---\n")
    assert probe.list_skill_names(root) == {"real-skill"}
