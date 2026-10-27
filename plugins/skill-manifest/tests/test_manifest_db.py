"""Parser behaviors from DESIGN_SPEC.md §4.1. Each test names the hazard."""
from __future__ import annotations

from helpers import _writable_tmp_base

import tempfile
import time
from pathlib import Path

import manifest_db

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"


def test_missing_path_fails_open() -> None:
    """Missing manifest must not raise inside skill_manage's mutation path."""
    doc = manifest_db.load_manifest("/nonexistent/UPSTREAM_MANIFEST.md")
    assert doc.skills == set()
    assert len(doc.errors) == 1
    assert doc.path == "/nonexistent/UPSTREAM_MANIFEST.md"


def test_unreadable_directory_path_fails_open() -> None:
    """A directory given as the manifest path is unreadable and must not raise."""
    with tempfile.TemporaryDirectory(dir=_writable_tmp_base()) as d:
        doc = manifest_db.load_manifest(d)
    assert doc.skills == set()
    assert len(doc.errors) == 1
    assert doc.path == d


def test_empty_file_is_empty_doc() -> None:
    """Empty manifest is valid idle state, not a parse failure."""
    with tempfile.TemporaryDirectory(dir=_writable_tmp_base()) as d:
        path = Path(d) / "UPSTREAM_MANIFEST.md"
        path.write_text("", encoding="utf-8")
        doc = manifest_db.load_manifest(path)
    assert doc.skills == set()
    assert doc.errors == []
    assert doc.path == str(path)


def test_no_tables_yields_no_errors() -> None:
    """Prose with no pipe-tables is not malformed; do not warn-as-error."""
    doc = manifest_db.load_manifest_text("# heading\n\njust words\n")
    assert doc.skills == set()
    assert doc.errors == []
    assert doc.path == ""


def test_file_column_table_does_not_contribute() -> None:
    """A File-header table leaking file.md would fake a manifested skill."""
    text = (
        "| File | Source |\n"
        "|------|--------|\n"
        "| `category/skill/references/file.md` | repo |\n"
        "| Skill | Source |\n"
        "|-------|--------|\n"
        "| `real-skill` | repo |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert doc.skills == {"real-skill"}
    assert "file.md" not in doc.skills


def test_audit_log_table_does_not_leak_backticked_names() -> None:
    """Audit prose rows mention skills; those names must not count as manifested."""
    text = (
        "| Date | Layer(s) | Actions taken |\n"
        "|------|----------|---------------|\n"
        "| 2026-01-01 | 2 | Published `endpoint-probe`; refreshed `library-rag`. |\n"
        "| Skill | Source |\n"
        "|-------|--------|\n"
        "| `kept-skill` | repo |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert doc.skills == {"kept-skill"}
    assert "endpoint-probe" not in doc.skills
    assert "library-rag" not in doc.skills


def test_header_must_contain_exact_skill_cell() -> None:
    """A lowercase 'skill' header is not the template's Skill column."""
    text = (
        "| skill | Source |\n"
        "|-------|--------|\n"
        "| `sneaky` | repo |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert doc.skills == set()


def test_first_cell_is_the_key_even_when_columns_drift() -> None:
    """Header-index keying fails on live rows that drop a column."""
    text = (
        "| Skill | A | B | C |\n"
        "|-------|---|---|---|\n"
        "| `short-row` | only-one |\n"
        "| `full-row` | a | b | c |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert doc.skills == {"short-row", "full-row"}


def test_normalize_backticks_bold_links_paren_path_punct() -> None:
    """Unnormalized keys would miss the same skill written four different ways."""
    text = (
        "| Skill | Source |\n"
        "|-------|--------|\n"
        "| `email/himalaya` | x |\n"
        "| **Monid** (top-level) | x |\n"
        "| [wrapped](https://example.com/x) | x |\n"
        "| `research/xiaohongshu-search.` | x |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert "himalaya" in doc.skills
    assert "monid" in doc.skills
    assert "wrapped" in doc.skills
    assert "xiaohongshu-search" in doc.skills
    assert manifest_db.is_manifested(doc, "HIMALAYA")
    assert manifest_db.is_manifested(doc, "email/himalaya")


def test_skip_empty_and_separator_data_rows() -> None:
    """A --- leftover in the first cell must not become a skill name."""
    text = (
        "| Skill | Source |\n"
        "|-------|--------|\n"
        "|  | x |\n"
        "| --- | x |\n"
        "| `ok-skill` | x |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert doc.skills == {"ok-skill"}


def test_reject_md_and_references_keys_in_skill_table() -> None:
    """Defense in depth: file-shaped keys in a Skill table still must not count."""
    text = (
        "| Skill | Source |\n"
        "|-------|--------|\n"
        "| `category/skill/references/styles.md` | x |\n"
        "| `notes.md` | x |\n"
        "| `good-skill` | x |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert doc.skills == {"good-skill"}
    assert not any(".md" in s for s in doc.skills)
    assert not any("references/" in s for s in doc.skills)


def test_section_headings_do_not_contaminate_rows() -> None:
    """Headings between tables are not rows; only pipe-lines form tables."""
    text = (
        "## Layer 1\n"
        "\n"
        "| Skill | Source |\n"
        "|-------|--------|\n"
        "| `alpha` | x |\n"
        "\n"
        "### Engine dependencies\n"
        "\n"
        "| Skill | Tool |\n"
        "|-------|------|\n"
        "| `beta` | `scrapling` |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert doc.skills == {"alpha", "beta"}
    assert "scrapling" not in doc.skills


def test_table_without_separator_is_ignored() -> None:
    """A lone header row is not a GFM table and must not yield skills."""
    doc = manifest_db.load_manifest_text("| Skill | Source |\n| `orphan` | x |\n")
    assert doc.skills == set()


def test_template_and_live_fixtures() -> None:
    """Real template/live formats are the production input; fixtures must parse."""
    template = manifest_db.load_manifest(FIXTURES / "upstream-manifest-template.md")
    assert "skill-name" in template.skills
    assert template.errors == []
    live = manifest_db.load_manifest(FIXTURES / "upstream-manifest-live.md")
    assert live.skills == {
        "himalaya",
        "harness-engineering",
        "impeccable",
        "hermes-agent-skill-authoring",
        "xiaohongshu-search",
        "monid",
        "website-scraping",
        "reddit-keyless-search",
    }
    assert live.errors == []


def test_is_manifested_unknown_is_false() -> None:
    """Unknown names must stay unmanifested so the probe can pend them."""
    doc = manifest_db.load_manifest_text(
        "| Skill | Source |\n|-------|--------|\n| `known` | x |\n"
    )
    assert not manifest_db.is_manifested(doc, "totally-unknown-skill")
    assert not manifest_db.is_manifested(doc, "")


def test_f1_escaped_pipe_does_not_create_phantom_skill_header() -> None:
    """A File header with an escaped \\| Skill cell must not qualify as Skill."""
    text = (
        "| File | Source \\| Skill |\n"
        "|------|----------------|\n"
        "| `notes` | repo |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert doc.skills == set()
    assert "notes" not in doc.skills
    assert "skill" not in doc.skills


def test_f2_separator_shaped_data_row_keeps_neighbors() -> None:
    """An all-separator row in the data section must not restart the table."""
    doc = manifest_db.load_manifest_text(
        "| Skill | x |\n|---|---|\n| `alpha` | y |\n| --- | --- |\n| `beta` | z |\n"
    )
    assert "alpha" in doc.skills
    assert "beta" in doc.skills


def test_f3_parenthetical_md_does_not_reject_key() -> None:
    """Cut at '(' before the .md reject so PORT_NOTES.md in a paren stays valid."""
    doc = manifest_db.load_manifest_text(
        "| Skill | x |\n|-------|---|\n| `keep` (see PORT_NOTES.md) | y |\n"
    )
    assert "keep" in doc.skills
    assert manifest_db.is_manifested(doc, "keep")


def test_f4_fenced_table_is_not_parsed() -> None:
    """A fenced example table must not count as manifested (or silence arrivals)."""
    text = (
        "```\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `old-example` | y |\n"
        "```\n"
        "\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `real-skill` | y |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert "old-example" not in doc.skills
    assert "real-skill" in doc.skills


def test_f4_tilde_fenced_table_is_not_parsed() -> None:
    """~~~ fences are the other CommonMark fence and must also be skipped."""
    text = (
        "~~~\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `old-example` | y |\n"
        "~~~\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert "old-example" not in doc.skills


def test_f4_html_comment_table_is_not_parsed() -> None:
    """Commenting out a table is how users retire a row; it must not stay live."""
    text = (
        "<!--\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `old-example` | y |\n"
        "-->\n"
        "\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `real-skill` | y |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert "old-example" not in doc.skills
    assert "real-skill" in doc.skills


def test_f6_bom_prefixed_table_parses() -> None:
    """A UTF-8 BOM before the first table line must not empty the whole doc."""
    body = "| Skill | x |\n|-------|---|\n| `bom-skill` | y |\n"
    with tempfile.TemporaryDirectory(dir=_writable_tmp_base()) as d:
        path = Path(d) / "UPSTREAM_MANIFEST.md"
        path.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
        doc = manifest_db.load_manifest(path)
    assert "bom-skill" in doc.skills


def test_f6_non_utf8_bytes_replace_and_parse() -> None:
    """Invalid UTF-8 must replace, not abandon the file as unreadable."""
    body = "| Skill | x |\n|-------|---|\n| `ok-skill` | y |\n"
    with tempfile.TemporaryDirectory(dir=_writable_tmp_base()) as d:
        path = Path(d) / "UPSTREAM_MANIFEST.md"
        path.write_bytes(body.encode("utf-8") + b"\xff prose\n")
        doc = manifest_db.load_manifest(path)
    assert "ok-skill" in doc.skills


def test_f8_errors_for_rejected_empty_and_missing_separator() -> None:
    """Rejected keys, empty first cells, and a Skill header without a separator."""
    rejected = manifest_db.load_manifest_text(
        "| Skill | Source |\n|-------|--------|\n| `notes.md` | x |\n| `ok-a` | x |\n"
    )
    assert "ok-a" in rejected.skills
    assert len(rejected.errors) >= 1

    empty = manifest_db.load_manifest_text(
        "| Skill | Source |\n|-------|--------|\n|  | x |\n| `ok-b` | x |\n"
    )
    assert "ok-b" in empty.skills
    assert len(empty.errors) >= 1

    missing_sep = manifest_db.load_manifest_text(
        "| Skill | Source |\n| `orphan` | x |\n"
    )
    assert missing_sep.skills == set()
    assert len(missing_sep.errors) >= 1


def test_f8_fixtures_and_prose_stay_error_free() -> None:
    """Fixtures and malformed-prose must keep errors == [] (nothing qualifies)."""
    assert manifest_db.load_manifest(FIXTURES / "upstream-manifest-template.md").errors == []
    assert manifest_db.load_manifest(FIXTURES / "upstream-manifest-live.md").errors == []
    prose = manifest_db.load_manifest_text("# heading\n\njust words\n")
    assert prose.errors == []


def test_f9_is_manifested_non_str_is_false() -> None:
    """is_manifested must fail-open on a non-str name instead of TypeError."""
    doc = manifest_db.load_manifest_text(
        "| Skill | x |\n|-------|---|\n| `known` | y |\n"
    )
    assert manifest_db.is_manifested(doc, None) is False  # type: ignore[arg-type]
    assert manifest_db.is_manifested(doc, 123) is False  # type: ignore[arg-type]


def test_f9_load_manifest_wraps_parse(monkeypatch) -> None:
    """load_manifest must catch _parse exceptions the way load_manifest_text does."""
    with tempfile.TemporaryDirectory(dir=_writable_tmp_base()) as d:
        path = Path(d) / "UPSTREAM_MANIFEST.md"
        path.write_text("| Skill | x |\n|-------|---|\n| `x` | y |\n", encoding="utf-8")

        def _boom(*_a, **_k):
            raise RuntimeError("parse exploded")

        monkeypatch.setattr(manifest_db, "_parse", _boom)
        doc = manifest_db.load_manifest(path)
    assert doc.skills == set()
    assert len(doc.errors) >= 1
    assert doc.path == str(path)


def test_f10_bold_and_backtick_skill_header_qualifies() -> None:
    """Header cells must strip ** and backticks before the exact Skill compare."""
    bold = manifest_db.load_manifest_text(
        "| **Skill** | Source |\n|-----------|--------|\n| `bold-skill` | x |\n"
    )
    assert "bold-skill" in bold.skills
    ticks = manifest_db.load_manifest_text(
        "| `Skill` | Source |\n|---------|--------|\n| `tick-skill` | x |\n"
    )
    assert "tick-skill" in ticks.skills


def test_f13_shorter_fence_closer_does_not_leak_table() -> None:
    """A 3-backtick line must not close a 4-backtick opener (CommonMark run length)."""
    text = (
        "````\n"
        "```\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `leaked` | y |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert "leaked" not in doc.skills
    assert doc.skills == set()


def test_f13_longer_fence_closer_releases_following_table() -> None:
    """A 4-backtick line closes a 3-backtick opener; the later table is live."""
    text = (
        "```\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `inside` | y |\n"
        "````\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `real-skill` | y |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert "inside" not in doc.skills
    assert "real-skill" in doc.skills


def test_f13_info_string_opener_closes_only_on_pure_fence() -> None:
    """```python opens a fence; only a pure-fence line closes it, not another info line."""
    text = (
        "```python\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `inside` | y |\n"
        "```python\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `not-closed` | y |\n"
        "```\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `real-skill` | y |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert "inside" not in doc.skills
    assert "not-closed" not in doc.skills
    assert "real-skill" in doc.skills


def test_f16_nested_parens_in_markdown_link() -> None:
    """Link destinations with balanced nested parens must not leave a trailing ')'."""
    nested = manifest_db.load_manifest_text(
        "| Skill | x |\n|-------|---|\n"
        "| [linked](https://example.org/a(b)) | y |\n"
    )
    assert "linked" in nested.skills
    assert "linked)" not in nested.skills
    assert all(")" not in name for name in nested.skills)
    assert manifest_db._normalize_key("[linked](https://example.org/a(b))") == "linked"

    unbalanced = manifest_db.load_manifest_text(
        "| Skill | x |\n|-------|---|\n"
        "| [linked](https://example.org/a(b) | y |\n"
    )
    assert all(")" not in name for name in unbalanced.skills)
    key = manifest_db._normalize_key("[linked](https://example.org/a(b)")
    assert key is None or ")" not in key


def test_r3a_unclosed_link_candidates_in_one_cell_are_fast() -> None:
    """A cell of ≥3000 unclosed [x]( candidates must not quadratic-backtrack."""
    cell = "[x]( " * 3200
    text = f"| Skill | x |\n|---|---|\n| {cell}| y |\n"
    t0 = time.perf_counter()
    doc = manifest_db.load_manifest_text(text)
    elapsed = time.perf_counter() - t0
    print(f"r3a elapsed: {elapsed:.4f}s")
    assert elapsed < 0.5, f"link scan took {elapsed:.3f}s (bound 0.5s)"
    assert doc.errors == [] or isinstance(doc.skills, set)


def test_r4a_unmatched_open_brackets_in_one_cell_are_linear() -> None:
    """A cell of unmatched '[' must stay linear: 4× size must not exceed 4× time."""
    cases = ((10_000, 0.005), (40_000, 0.020), (160_000, 0.080))
    for n, limit in cases:
        cell = "[" * n
        text = f"| Skill | x |\n|---|---|\n| {cell}| y |\n"
        t0 = time.perf_counter()
        doc = manifest_db.load_manifest_text(text)
        dt = time.perf_counter() - t0
        print(f"r4a n={n} elapsed={dt:.6f}s bound={limit:.3f}s")
        assert dt < limit, f"n={n} took {dt:.3f}s (bound {limit:.3f}s)"
        assert doc.errors == [] or isinstance(doc.skills, set)


def test_r5a_many_open_brackets_then_one_closer_are_linear() -> None:
    """A wall of '[' plus one distant ']' must not re-search that closer."""
    assert manifest_db.LINK_LOOKAHEAD == 2048
    cases = ((100_000, 0.030), (200_000, 0.060), (400_000, 0.120))
    times: list[tuple[int, float, float]] = []
    for n, limit in cases:
        cell = "[" * n + "]"
        text = f"| Skill | x |\n|---|---|\n| {cell}| y |\n"
        t0 = time.perf_counter()
        doc = manifest_db.load_manifest_text(text)
        dt = time.perf_counter() - t0
        times.append((n, dt, limit))
        print(f"r5a n={n} elapsed={dt:.6f}s bound={limit:.3f}s")
        assert dt < limit, f"n={n} took {dt:.3f}s (bound {limit:.3f}s)"
        assert doc.errors == [] or isinstance(doc.skills, set)
    print("r5a timing table:")
    print(f"{'n':>8}  {'elapsed_s':>12}  {'bound_s':>8}")
    for n, dt, limit in times:
        print(f"{n:8d}  {dt:12.6f}  {limit:8.3f}")
    # 2× size must not exceed 3× time (floor avoids noise on sub-ms runs).
    t100, t200, t400 = times[0][1], times[1][1], times[2][1]
    print(f"r5a 2x-check t200/t100={t200 / max(t100, 1e-9):.2f} t400/t200={t400 / max(t200, 1e-9):.2f}")
    floor = 0.001
    assert t200 <= 3 * t100 or t100 < floor
    assert t400 <= 3 * t200 or t200 < floor


def test_r5a_literal_bracket_in_key_quirk_is_acceptable() -> None:
    """A leftover ] in the normalized key (a]) is an accepted link-scan quirk."""
    leftover = manifest_db._normalize_key("[a](b(c)dd)]")
    assert leftover in {"a", "a]"}
    assert leftover is not None and ")" not in leftover
    quirk = manifest_db._normalize_key("[a]](https://example.com)")
    assert quirk in {"a]", "[a]]", "a", "[a]"}


def test_r3b_empty_fence_pair_does_not_join_following_row() -> None:
    """A row after an empty fence pair is not data of the preceding table."""
    text = "| Skill | Last sync |\n|---|---|\n```\n```\n| `post` | invalid |"
    doc = manifest_db.load_manifest_text(text)
    assert "post" not in doc.skills
    assert doc.skills == set()


def test_r3b_table_after_closed_fence_still_parses() -> None:
    """A full table after a closed fence still needs only its own header+separator."""
    text = (
        "```\n"
        "```\n"
        "| Skill | x |\n"
        "|---|---|\n"
        "| `real-skill` | y |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert "real-skill" in doc.skills


def test_r3b_fence_inside_table_data_breaks_run() -> None:
    """header+separator+fence+row → the post-fence row is not part of the table."""
    text = (
        "| Skill | x |\n"
        "|---|---|\n"
        "| `kept` | y |\n"
        "```\n"
        "```\n"
        "| `post` | z |\n"
    )
    doc = manifest_db.load_manifest_text(text)
    assert "kept" in doc.skills
    assert "post" not in doc.skills


def test_r18_size_is_parsed_byte_length_and_survives_mutation() -> None:
    """R18 X2: size == len(bytes read); mutating the file after the load
    returns cannot change the document's size. An unreadable file reports
    size 0 with the read-error branch."""
    prose = "# Notes\nNothing here.\n".encode("utf-8")
    with tempfile.TemporaryDirectory(dir=_writable_tmp_base()) as d:
        path = Path(d) / "UPSTREAM_MANIFEST.md"
        path.write_bytes(prose)
        doc = manifest_db.load_manifest(path)
        assert doc.skills == set()
        assert doc.errors == []
        assert doc.has_table is False
        assert doc.size == len(prose)
        path.write_bytes(b"")
        assert doc.size == len(prose)
        missing = manifest_db.load_manifest(Path(d) / "MISSING.md")
        assert missing.errors != []
        assert missing.size == 0
