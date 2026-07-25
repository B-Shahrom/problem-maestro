"""The correction request, and the two properties it is worth having."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from maestro import feedback
from maestro.checks import Finding, Severity
from maestro.ingest import Inspection, Verdict

MAESTRO = Path(__file__).resolve().parent.parent / "maestro"

#: Any string literal shaped like a check code, anywhere in the package. Matching
#: the literal rather than the `err("X-1", …)` call is deliberate: `manifest.py`
#: passes codes through a `(key, check)` tuple, so a call-shaped pattern would
#: miss M-11 and the completeness test would pass while the table had a hole.
CODE = re.compile(r'"([A-Z]-\d+)"')


def _codes() -> set[str]:
    found: set[str] = set()
    for path in MAESTRO.glob("*.py"):
        if path.name == "feedback.py":
            continue  # its own table would make the check circular
        found |= set(CODE.findall(path.read_text(encoding="utf-8")))
    return found


def _insp(*findings: Finding, verdict=Verdict.INVALID, checked=("manifest",)) -> Inspection:
    return Inspection(Path("/w/edu-arrays"), verdict, list(findings),
                      "edu-arrays-20260725", checked=frozenset(checked))


def test_every_emitted_check_has_authoring_guidance():
    """A code with no clause degrades the report to a bare log line, silently."""
    emitted = _codes()
    assert emitted, "the scan found no check codes at all — the pattern has rotted"
    assert not (emitted - set(feedback.CLAUSES)), (
        "these checks are emitted but have no entry in feedback.CLAUSES, so an author "
        "would be told what was observed and not what rule it breaks")


def test_clause_table_names_a_real_document():
    """`Where:` has to be findable, or it is decoration."""
    docs = Path(__file__).resolve().parent.parent / "docs"
    for code, clause in feedback.CLAUSES.items():
        if clause.where == "—":
            continue
        name = clause.where.split()[0].split("§")[0].strip()
        assert list(docs.rglob(name)), f"{code} cites {name}, which does not exist"


def test_report_carries_rule_and_fix_not_just_the_observation():
    body = feedback.render(_insp(
        Finding("C-2", Severity.ERROR, "4 tag line(s) against 5 General row(s)")))
    assert "C-2" in body
    assert "4 tag line(s) against 5 General row(s)" in body
    assert feedback.CLAUSES["C-2"].rule in body
    assert feedback.CLAUSES["C-2"].fix in body


def test_report_names_the_families_that_did_not_run():
    """The property the report exists for: an absent check must not read as a pass."""
    body = feedback.render(_insp(Finding("M-5", Severity.ERROR, "duplicate slug", "a")))
    assert "## Not checked" in body
    assert feedback.FAMILIES["characteristics"] in body
    assert feedback.FAMILIES["importer"] in body
    assert feedback.FAMILIES["manifest"] not in body


def test_a_fully_checked_set_has_no_not_checked_section():
    body = feedback.render(_insp(
        Finding("C-3", Severity.ERROR, "group 'trivial' is not easy/medium/hard", "a"),
        checked=("manifest", "characteristics", "importer")))
    assert "## Not checked" not in body


def test_findings_group_by_problem():
    body = feedback.render(_insp(
        Finding("M-4", Severity.ERROR, "count mismatch"),
        Finding("M-5", Severity.ERROR, "duplicate slug", "b-two"),
        Finding("M-9", Severity.ERROR, "tag outside the vocabulary", "b-two"),
        Finding("M-12", Severity.ERROR, "compiled binary", "a-one"),
    ))
    # The set as a whole first, then problems in slug order — the order the author
    # works in, not the order the checkers happened to run in.
    heads = re.findall(r"^### (.+)$", body, re.M)
    assert heads == ["the set as a whole", "a-one", "b-two"]


def test_warnings_are_separated_from_blockers():
    body = feedback.render(_insp(
        Finding("M-9", Severity.ERROR, "unknown tag", "a"),
        Finding("M-14", Severity.WARN, "delivery=partial"),
    ))
    assert "## Blocking — 1" in body
    assert "## Advisory — 1" in body
    assert body.index("## Blocking") < body.index("## Advisory")


def test_incomplete_reads_as_wait_not_as_wrong():
    wrong = feedback.render(_insp(Finding("M-2", Severity.ERROR, "sha256 mismatch", "a")))
    waiting = feedback.render(_insp(
        Finding("M-2", Severity.ERROR, "b.zip is missing from the set folder", "b"),
        verdict=Verdict.INCOMPLETE))
    assert "refused at the gate" in wrong
    assert "unfinished rather than wrong" in waiting


def test_unknown_code_is_loud_rather_than_absent():
    body = feedback.render(_insp(Finding("Z-9", Severity.ERROR, "something new")))
    assert "something new" in body
    assert "gap in Maestro" in body


@pytest.mark.parametrize("name,want", [
    ("edu-arrays", "edu-arrays-r2"),
    ("edu-arrays-r2", "edu-arrays-r3"),
    ("edu-arrays-r9", "edu-arrays-r10"),
    ("2026-05-r1", "2026-05-r2"),
])
def test_redelivery_name_bumps_rather_than_collides(name, want):
    assert feedback.redelivery_name(Path("/w") / name) == want


def test_render_is_stable_across_calls():
    """No clock, no environment — so `write` can compare and stay quiet."""
    insp = _insp(Finding("M-4", Severity.ERROR, "count mismatch"))
    assert feedback.render(insp) == feedback.render(insp)


# ----------------------------------------------------------------- writing


def test_write_creates_a_sibling_not_a_member_of_the_set(tmp_path):
    set_dir = tmp_path / "edu-arrays"
    set_dir.mkdir()
    insp = Inspection(set_dir, Verdict.INVALID,
                      [Finding("M-4", Severity.ERROR, "count mismatch")],
                      checked=frozenset({"manifest"}))
    path = feedback.write(insp)
    assert path == tmp_path / "edu-arrays.REJECTED.md"
    assert path.is_file()
    # Inside the folder it would become part of the delivery, and a later
    # cross-check of "every file is accounted for" would trip over it.
    assert not list(set_dir.iterdir())


def test_write_is_quiet_when_nothing_changed(tmp_path):
    set_dir = tmp_path / "edu-arrays"
    set_dir.mkdir()
    insp = Inspection(set_dir, Verdict.INVALID,
                      [Finding("M-4", Severity.ERROR, "count mismatch")],
                      checked=frozenset({"manifest"}))
    assert feedback.write(insp) is not None
    assert feedback.write(insp) is None, "a repeated rejection must not rewrite the file"


def test_write_rewrites_when_the_findings_change(tmp_path):
    set_dir = tmp_path / "edu-arrays"
    set_dir.mkdir()
    first = Inspection(set_dir, Verdict.INVALID,
                       [Finding("M-4", Severity.ERROR, "count mismatch")],
                       checked=frozenset({"manifest"}))
    feedback.write(first)
    second = Inspection(set_dir, Verdict.INVALID,
                        [Finding("M-5", Severity.ERROR, "duplicate slug", "a")],
                        checked=frozenset({"manifest"}))
    assert feedback.write(second) is not None
    assert "duplicate slug" in feedback.path_for(set_dir).read_text(encoding="utf-8")


def test_a_set_that_becomes_ready_loses_its_rejection_notice(tmp_path):
    set_dir = tmp_path / "edu-arrays"
    set_dir.mkdir()
    feedback.write(Inspection(set_dir, Verdict.INVALID,
                              [Finding("M-4", Severity.ERROR, "count mismatch")],
                              checked=frozenset({"manifest"})))
    assert feedback.path_for(set_dir).is_file()
    feedback.write(Inspection(set_dir, Verdict.READY, [], checked=frozenset({"manifest"})))
    assert not feedback.path_for(set_dir).exists(), (
        "a rejection notice beside an accepted delivery is worse than no notice")


def test_a_read_only_watch_folder_does_not_break_the_sweep(tmp_path, monkeypatch):
    set_dir = tmp_path / "edu-arrays"
    set_dir.mkdir()

    def boom(*_a, **_k):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "write_text", boom)
    insp = Inspection(set_dir, Verdict.INVALID,
                      [Finding("M-4", Severity.ERROR, "count mismatch")],
                      checked=frozenset({"manifest"}))
    assert feedback.write(insp) is None  # reported, not raised
