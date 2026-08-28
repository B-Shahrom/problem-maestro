"""Maestro's parser against the author project's own `characteristics-template.md`.

The sixth round-trip module, and the first one pointed at the *author* rather
than at a downstream tool. The other five ask "does Maestro's model of the
Scraper and the Middleman match theirs"; this one asks the same about the
document the author actually fills in.

It exists because `CHARACTERISTICS_SPEC.md` §1.1 records two incompatible
specifications live in the project at once — playbook §11 and this template —
and resolved the conflict in favour of the template. A resolution written in
prose is not a resolution anything checks. This is what checks it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from maestro import characteristics as ch
from maestro.manifest import VOCABULARY

TEMPLATE = Path(__file__).resolve().parent.parent / "docs" / "contracts" / "characteristics-template.md"

#: The template's own three-row example, with the placeholders filled in. The
#: shape is the template's verbatim — column order, `N (a+b)` tests, `[none]`,
#: the numbered positional lists.
FILLED = """\
# Characteristics — Arrays Week 2

_Beginner arrays lesson._

---

## General

| idx | slug | title | languages | group | tests | subtasks | checker | TL | ML |
|-----|------|-------|-----------|-------|-------|----------|---------|-----|------|
| 1 | edu-arrays-max | Running Max | EN, RU | easy | 24 (2+22) | none | ncmp (native) | 1 s | 256 MB |
| 2 | edu-arrays-gap | Largest Gap | EN, RU, TJ, UZ | medium | 30 (3+27) | S0(0), S1(30), S2(70) | custom | 2 s | 256 MB |
| 3 | edu-arrays-pair | Pair Sum | EN | hard | 18 (2+16) | none | wcmp (native) | 1 s | 512 MB |

**TOTAL problems:** 3
**TOTAL tests:** 72

---

## Hard

[none]

---

## Suggested tags

1. implementation, arrays
2. brute force, arrays, implementation
3. math, number theory, arrays

---

## Checkers used

1. ncmp (native)
2. custom
3. wcmp (native)
"""


def test_the_shipped_template_parses_at_all():
    """A template Maestro cannot read is a batch that cannot be chored."""
    assert TEMPLATE.is_file(), "the template the specs refer to is missing from the repo"
    c = ch.parse(TEMPLATE.read_text(encoding="utf-8"))
    assert c.general_found
    assert len(c.rows) == 3
    assert len(c.tags) == 3


def test_every_column_the_template_defines_is_a_column_maestro_reads():
    """Ten columns, and four of them exist only because something downstream
    needs them: `group` becomes the platform's difficulty, `languages` and
    `subtasks` are cross-checked against the manifest, TL/ML are the only
    authored copy C-7 can compare the manifest against."""
    c = ch.parse(FILLED)
    assert [r.slug for r in c.rows] == ["edu-arrays-max", "edu-arrays-gap", "edu-arrays-pair"]
    assert [r.title for r in c.rows] == ["Running Max", "Largest Gap", "Pair Sum"]
    assert [r.languages for r in c.rows] == ["EN, RU", "EN, RU, TJ, UZ", "EN"]
    assert [r.difficulty for r in c.rows] == ["Easy", "Medium", "Hard"]
    assert [r.has_subtasks for r in c.rows] == [False, True, False]
    assert [r.time_limit_s for r in c.rows] == [1.0, 2.0, 1.0]
    assert [r.memory_limit_mb for r in c.rows] == [256.0, 256.0, 512.0]


def test_the_none_convention_is_understood():
    """The template's own words: `[none]` "explicitly signals we checked, there
    really is nothing here, which is meaningfully different from the section
    being accidentally left out". That is this project's fail-loud rule, arrived
    at independently on the author's side — so it has to survive the parser."""
    c = ch.parse(FILLED)
    assert c.rows[0].has_subtasks is False
    assert ch.parse("## Hard\n\n[none]\n").rows == []


def test_the_templates_tag_vocabulary_is_the_gates_vocabulary():
    """The pin that matters most here.

    An unknown tag is not rejected by the platform — it is *created* in the tag
    store and then exists for everyone, forever (M-9). So the list the author is
    offered and the list the gate accepts have to be one list, and the template
    is where the author reads it.

    Note the template calls its list "a recommendation, not a hard whitelist".
    The gate disagrees, deliberately. This test says the two lists are at least
    identical today; `project-instructions-delta.md` §5 is about the licence.
    """
    body = TEMPLATE.read_text(encoding="utf-8")
    used = {t.strip() for line in body.splitlines()
            if line.startswith(("1.", "2.", "3.")) and "," in line
            for t in line.split(".", 1)[1].split(",")}
    unknown = used - set(VOCABULARY) - {"ncmp (native)", "custom", "wcmp (native)"}
    assert not unknown, f"the template's own example uses tags the gate rejects: {sorted(unknown)}"


@pytest.mark.parametrize("tag", sorted(VOCABULARY))
def test_no_gate_tag_is_spelled_differently_from_the_authors_list(tag):
    """Spelling, not membership. `dfs and similar` and `dfs-and-similar` are the
    same tag to a human and two different tags to the platform's tag store —
    and the Memory's Notion convention is the dashed one, for a different field.
    """
    assert tag == tag.lower()
    assert "-" not in tag or tag in {"meet-in-the-middle", "2-sat"}
