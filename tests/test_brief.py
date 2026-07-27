"""The brief, and the one property that makes it worth generating."""

from __future__ import annotations

from pathlib import Path

import pytest

from maestro import brief as B
from maestro.manifest import SLUG_RE, SUPPORTED_SCHEMA, VOCABULARY

CONTRACTS = Path(__file__).resolve().parent.parent / "docs" / "contracts"


def _b(**kw) -> B.Brief:
    return B.Brief(**{"name": "edu-arrays-20260801", "prefix": "edu-arrays",
                      "mix": {"easy": 2, "medium": 2, "hard": 1}, **kw})


# ------------------------------------------------------- it cannot drift


def test_the_brief_carries_the_vocabulary_the_gate_enforces():
    """The whole reason this is generated rather than written.

    A hand-written brief is correct the day it is written. If the vocabulary
    grows a tag, an author working from a stale copy never uses it and an author
    who guesses gets rejected by M-9 for a rule they were never given.
    """
    body = B.render(_b())
    for tag in VOCABULARY:
        assert f"`{tag}`" in body, f"{tag} is enforced by M-9 but absent from the brief"
    assert str(len(VOCABULARY)) in body


def test_the_brief_carries_the_slug_pattern_and_schema_the_gate_enforces():
    body = B.render(_b())
    assert SLUG_RE.pattern in body
    for version in SUPPORTED_SCHEMA:
        assert version in body


def test_the_brief_names_every_contract_that_exists():
    """A contract the author is never pointed at is one they cannot follow."""
    named = {name for name, _ in B.CONTRACTS}
    on_disk = {p.name for p in CONTRACTS.glob("*.md")}
    assert not (named - on_disk), "the brief cites a contract that does not exist"
    body = B.render(_b())
    for name in named:
        assert name in body


def test_the_redelivery_name_matches_what_feedback_would_tell_them():
    """Two modules telling the author two different folder names is worse than one."""
    from maestro import feedback

    b = _b()
    assert feedback.redelivery_name(Path("/w") / b.name) in B.render(b)


# ----------------------------------------------------------- the mix


@pytest.mark.parametrize("spec,want", [
    ("easy=2,medium=2,hard=1", {"easy": 2, "medium": 2, "hard": 1}),
    ("2:2:1", {"easy": 2, "medium": 2, "hard": 1}),
    ("0:3:0", {"medium": 3}),
    ("hard=4", {"hard": 4}),
    ("", {}),
])
def test_parse_mix(spec, want):
    assert B.parse_mix(spec) == want


def test_an_unknown_group_is_refused_rather_than_dropped():
    with pytest.raises(ValueError, match="trivial"):
        B.parse_mix("trivial=3")


def test_a_short_positional_mix_is_refused():
    """`2:1` would otherwise silently mean something the operator did not write."""
    with pytest.raises(ValueError, match="3 parts"):
        B.parse_mix("2:1")


def test_the_count_is_the_mix_and_cannot_disagree_with_it():
    b = _b(mix={"easy": 3, "hard": 2})
    assert b.count == 5
    assert "Write **5** problem(s)" in B.render(b)


# --------------------------------------------------------- brief checks


def test_a_taken_set_name_is_caught_before_the_set_is_written():
    """`set_name` is UNIQUE — authoring against a taken name delivers into silence."""
    found = B.check(_b(), existing={"edu-arrays-20260801"})
    assert [f.check for f in found] == ["B-1"]
    assert "already exists" in found[0].message


def test_a_prefix_that_cannot_start_a_legal_slug_is_caught_now():
    found = B.check(_b(prefix="Edu Arrays"))
    assert [f.check for f in found] == ["B-2"]


def test_an_empty_mix_is_a_brief_that_asks_for_nothing():
    assert [f.check for f in B.check(_b(mix={}))] == ["B-3"]


def test_a_good_brief_passes():
    assert B.check(_b(), existing={"other-set"}) == []


# ------------------------------------------------------ inlined contracts


def test_with_contracts_inlines_them_verbatim():
    body = B.render(_b(), contracts_dir=CONTRACTS)
    for name, _ in B.CONTRACTS:
        assert (CONTRACTS / name).read_text(encoding="utf-8").strip() in body


def test_a_missing_contract_is_announced_not_skipped(tmp_path):
    """Three of four contracts, shipped quietly, is a set authored against a rule
    the author never saw."""
    (tmp_path / "PREFLIGHT.md").write_text("preflight", encoding="utf-8")
    body = B.render(_b(), contracts_dir=tmp_path)
    assert body.count("**MISSING**") == 3
    assert "do not author against it" in body


def test_render_is_stable():
    b = _b()
    assert B.render(b) == B.render(b)
