import json

from maestro.characteristics import parse, precheck
from maestro.checks import ok
from tests.conftest import CHAR


def mf(set_dir):
    return json.loads((set_dir / "MANIFEST.json").read_text())


def codes(f):
    return {x.check for x in f}


# ------------------------------------------------- parser mirrors batch.py

def test_parses_the_reference_shape():
    c = parse(CHAR)
    assert c.name == "edu-arrays-20260725"
    assert [r.slug for r in c.rows] == ["edu-arrays-running-max", "edu-arrays-largest-gap"]
    assert c.tags == ["implementation, arrays", "observation, arrays"]
    assert c.tags_will_apply


def test_general_heading_is_case_sensitive():
    """`## general` finds nothing — zero problems, exit 0, a silent no-op."""
    c = parse(CHAR.replace("## General", "## general"))
    assert not c.general_found and c.rows == []


def test_tags_heading_is_case_insensitive():
    """Asymmetric with General, and mirroring that asymmetry is the point."""
    c = parse(CHAR.replace("## Suggested tags", "## SUGGESTED TAGS"))
    assert len(c.tags) == 2


def test_column_order_is_irrelevant_and_extras_ignored():
    swapped = CHAR.replace(
        "| idx | slug | title | languages | group | tests | subtasks | checker | TL | ML |",
        "| group | slug | nonsense | title | languages | subtasks |",
    ).replace(
        "| 1 | edu-arrays-running-max | Running Maximum | EN, RU | easy | 41 (2+39) | none | ncmp (native) | 1 s | 256 MB |",
        "| easy | edu-arrays-running-max | zzz | Running Maximum | EN, RU | none |",
    ).replace(
        "| 2 | edu-arrays-largest-gap | Largest Gap | EN, RU | medium | 47 (2+45) | none | ncmp (native) | 1 s | 256 MB |",
        "| medium | edu-arrays-largest-gap | zzz | Largest Gap | EN, RU | none |",
    )
    c = parse(swapped)
    assert [r.slug for r in c.rows] == ["edu-arrays-running-max", "edu-arrays-largest-gap"]
    assert c.rows[0].difficulty == "Easy"


def test_row_without_a_slug_is_dropped():
    c = parse(CHAR.replace("| 2 | edu-arrays-largest-gap |", "| 2 | none |"))
    assert [r.slug for r in c.rows] == ["edu-arrays-running-max"]


def test_unrecognised_group_yields_no_difficulty():
    assert parse(CHAR.replace("| easy |", "| Very Easy |")).rows[0].difficulty == ""


def test_none_tokens_drive_subtask_detection():
    c = parse(CHAR)
    assert c.rows[0].has_subtasks is False
    assert parse(CHAR.replace("| none | ncmp", "| S0(0), S1(100) | ncmp")).rows[0].has_subtasks


def test_tags_none_sentinel():
    c = parse(CHAR.replace("1. implementation, arrays\n2. observation, arrays", "[none]"))
    assert c.tags == [] and not c.tags_will_apply


def test_tag_numbers_are_decorative():
    """`1. / 3. / 2.` still aligns by position — the digit is never checked."""
    c = parse(CHAR.replace("2. observation, arrays", "7. observation, arrays"))
    assert c.tags == ["implementation, arrays", "observation, arrays"]


# ---------------------------------------------------------------- precheck

def test_clean_file_passes(set_dir):
    assert ok(precheck(set_dir / "characteristics.md", mf(set_dir)))


def test_mistyped_heading_is_reported_as_a_silent_noop(set_dir):
    p = set_dir / "characteristics.md"
    p.write_text(CHAR.replace("## General", "## general"), encoding="utf-8")
    f = precheck(p, mf(set_dir))
    assert "C-1" in codes(f)
    assert "exit 0" in next(x for x in f if x.check == "C-1").message


def test_tag_row_count_mismatch_is_an_error(set_dir):
    """The highest-value check: batch.py drops ALL tags and still exits 0."""
    p = set_dir / "characteristics.md"
    p.write_text(CHAR.replace("2. observation, arrays\n", ""), encoding="utf-8")
    f = precheck(p, mf(set_dir))
    assert "C-2" in codes(f)
    assert "drop ALL tags" in next(x for x in f if x.check == "C-2").message


def test_bad_group_flagged_before_it_silently_does_nothing(set_dir):
    p = set_dir / "characteristics.md"
    p.write_text(CHAR.replace("| easy |", "| Very Easy |"), encoding="utf-8")
    assert "C-3" in codes(precheck(p, mf(set_dir)))


def test_slug_set_checked_in_both_directions(set_dir):
    m = mf(set_dir)
    m["problems"].append({**m["problems"][0], "slug": "edu-arrays-ghost", "tags": ["arrays"]})
    f = precheck(set_dir / "characteristics.md", m)
    assert any(x.check == "C-4" and x.slug == "edu-arrays-ghost" for x in f)


def test_extra_row_not_in_manifest_flagged(set_dir):
    p = set_dir / "characteristics.md"
    last = "| 2 | edu-arrays-largest-gap | Largest Gap | EN, RU | medium | 47 (2+45) | none | ncmp (native) | 1 s | 256 MB |"
    p.write_text(CHAR.replace(
        last,
        last + "\n| 3 | edu-arrays-phantom | Phantom | EN, RU | hard | 5 (1+4) | none | ncmp (native) | 1 s | 256 MB |",
    ), encoding="utf-8")
    f = precheck(p, mf(set_dir))
    assert any(x.check == "C-4" and x.slug == "edu-arrays-phantom" for x in f)


def test_reordered_tag_lines_caught_against_the_manifest(set_dir):
    """File order is what batch.py obeys; the manifest is what's authoritative."""
    p = set_dir / "characteristics.md"
    p.write_text(CHAR.replace(
        "1. implementation, arrays\n2. observation, arrays",
        "1. observation, arrays\n2. implementation, arrays",
    ), encoding="utf-8")
    f = precheck(p, mf(set_dir))
    assert "C-5" in codes(f)


def test_mixed_language_sets_cannot_be_one_batch(set_dir):
    m = mf(set_dir)
    m["problems"][1]["languages"] = ["EN", "RU", "TJ"]
    f = precheck(set_dir / "characteristics.md", m)
    assert "C-6" in codes(f)
    assert "split" in next(x for x in f if x.check == "C-6").message


def test_missing_file_reported(set_dir):
    assert "C-1" in codes(precheck(set_dir / "nope.md", mf(set_dir)))


def test_blank_line_truncates_the_table_silently():
    """A blank line mid-table drops every row after it — no error, no warning.

    Worth pinning: it is how a hand-edited file loses problems without any signal,
    and the precheck's C-4 both-directions comparison is what catches the result.
    """
    last = "| 1 | edu-arrays-running-max | Running Maximum | EN, RU | easy | 41 (2+39) | none | ncmp (native) | 1 s | 256 MB |"
    c = parse(CHAR.replace(last, last + "\n"))
    assert [r.slug for r in c.rows] == ["edu-arrays-running-max"]


def test_truncated_table_surfaces_as_a_missing_slug(set_dir):
    last = "| 1 | edu-arrays-running-max | Running Maximum | EN, RU | easy | 41 (2+39) | none | ncmp (native) | 1 s | 256 MB |"
    p = set_dir / "characteristics.md"
    p.write_text(CHAR.replace(last, last + "\n"), encoding="utf-8")
    f = precheck(p, mf(set_dir))
    assert any(x.check == "C-4" and x.slug == "edu-arrays-largest-gap" for x in f)
