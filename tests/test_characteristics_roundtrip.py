"""Check `render()` against the parser that will actually read it.

Every other characteristics test measures Maestro's mirror of `batch.py` against
itself, which cannot detect the failure that matters: the mirror drifting from
the real thing. Stage 7 hands `batch.py` a file Maestro *wrote*, so if the two
parsers disagree the chore runner silently does less — a dropped row gets no
metadata, a dropped tag list gets no tags, and both exit 0.

Skipped when the Scraper checkout isn't on hand. Point `MAESTRO_SCRAPER_REPO` at
it to run this locally; it is the one test that needs the other repo.
"""

import os
import sys
from pathlib import Path

import pytest

from maestro.characteristics import parse, render, subset
from tests.conftest import CHAR, SLUGS

_CANDIDATES = [os.environ.get("MAESTRO_SCRAPER_REPO"),
               "/workspace/platform-scraper",
               str(Path.home() / "platform-scraper")]


@pytest.fixture(scope="module")
def real():
    for c in _CANDIDATES:
        if c and (Path(c) / "batch.py").is_file():
            sys.path.insert(0, c)
            try:
                import batch
            except ImportError as e:  # its own deps are not Maestro's problem
                pytest.skip(f"platform-scraper found at {c} but not importable: {e}")
            return batch
    pytest.skip("platform-scraper checkout not found (set MAESTRO_SCRAPER_REPO)")


def _plan(real, text, **opts):
    parsed = real.parse_characteristics(text)
    opts.setdefault("fixmdx", "off")
    steps, warns = real.build_plan(parsed, opts)
    return parsed, {s["name"]: s["sub"] for s in steps}, warns


def test_a_full_render_round_trips(real):
    parsed, _, _ = _plan(real, render(parse(CHAR)))
    assert [p["slug"] for p in parsed["problems"]] == SLUGS
    assert parsed["tags"] == ["implementation, arrays", "observation, arrays"]


@pytest.mark.parametrize("keep", [[SLUGS[0]], [SLUGS[1]], SLUGS])
def test_every_subset_keeps_its_tags_aligned(real, keep):
    """The whole stage-7 split rests on this: filter rows, filter tags with them."""
    parsed, steps, warns = _plan(real, render(subset(parse(CHAR), keep)))
    assert [p["slug"] for p in parsed["problems"]] == keep
    assert not any("tags skipped" in w for w in warns)
    sub = steps["metadata"]
    assert sub[sub.index("--category") + 1].split("\n") == \
        [{SLUGS[0]: "implementation, arrays", SLUGS[1]: "observation, arrays"}[s] for s in keep]
    assert sub[sub.index("--difficulty") + 1].split("\n") == \
        [{SLUGS[0]: "Easy", SLUGS[1]: "Medium"}[s] for s in keep]


def test_tags_mode_reaches_the_flag_that_protects_existing_tags(real):
    """`--category` calls fill(), so a pre-existing problem needs --category-add."""
    text = render(subset(parse(CHAR), [SLUGS[1]]))
    _, reset, _ = _plan(real, text, tags_mode="reset")
    _, add, _ = _plan(real, text, tags_mode="add")
    assert "--category-add" not in reset["metadata"]
    assert "--category-add" in add["metadata"]


def test_a_one_problem_subset_still_produces_steps(real):
    """A table the real parser rejects yields zero steps and exit 0 — silently."""
    _, steps, _ = _plan(real, render(subset(parse(CHAR), [SLUGS[0]])),
                        divisions="Electi", tags_mode="reset")
    assert set(steps) == {"metadata", "division"}


def test_maestro_and_batch_agree_on_the_authored_file(real):
    """Not just on what Maestro writes — on what the problem developer wrote."""
    mine, theirs = parse(CHAR), real.parse_characteristics(CHAR)
    assert [r.slug for r in mine.rows] == [p["slug"] for p in theirs["problems"]]
    assert mine.tags == theirs["tags"]
    assert mine.name == theirs["name"]
    assert [r.difficulty for r in mine.rows] == \
        [real._DIFF.get(p["group"], "") for p in theirs["problems"]]
    assert [r.has_subtasks for r in mine.rows] == [p["has_subtasks"] for p in theirs["problems"]]
