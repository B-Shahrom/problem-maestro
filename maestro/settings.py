"""The three values a batch chooses for itself, rather than inheriting.

`divisions`, `targets` and `list_url` were all config keys, which made each a
property of the *installation*. None of them is. Which divisions a set is
granted to, which languages its statements are translated into, and which
contest list it joins are properties of the *delivery* — the last one obviously
so, since it names one specific contest.

Editing `config.json` and restarting between batches is the wrong shape for
that, and it is the shape that shipped a run with no divisions at all: the
default was never changed, and the chore step simply vanished from the plan.

So each is a per-run override with the config value as its default, and all
three share one rule with three states, because the middle one is the whole
point:

* `None`  — never chosen. Inherit the config.
* `""`    — chosen, deliberately empty. Do **not** fall back.
* `"..."` — use exactly this.

Two of the three are closed vocabularies, mirrored from the Scraper and pinned
to it by `test_scraper_roundtrip.py`. That matters because both tools reject an
unrecognised value with exit `1` — `division set` at the *end* of the chore
chain, after `fixmdx` and `metadata` have already run and been paid for. A
picker that cannot express a bad value is worth more than a message explaining
one.
"""

from __future__ import annotations

from dataclasses import dataclass

from .divisions import DIVISIONS

#: Statement languages, mirrored from `problem_editor._LANG_NAMES`. `en` is the
#: translate *source* by default, so it is rarely a target — but the tool accepts
#: it and Maestro does not second-guess a deliberate choice.
LANGUAGES = ("en", "ru", "tg", "uz")


@dataclass(frozen=True, slots=True)
class Setting:
    """One per-batch override: what it is, and what values it may take."""

    key: str
    label: str
    help: str
    vocabulary: tuple[str, ...] | None = None
    """`None` means free text — there is nothing to check it against."""

    empty: str = "none"
    """How to describe a deliberate empty, in this setting's own terms."""

    @property
    def is_list(self) -> bool:
        return self.vocabulary is not None


FIELDS: dict[str, Setting] = {
    "divisions": Setting(
        "divisions", "division access",
        "Who can see these problems. The division step is absent from the chore "
        "plan entirely when this is empty.",
        DIVISIONS, "no division access will be granted"),
    "targets": Setting(
        "targets", "translation targets",
        "Statements are translated from en into these. No translate step runs "
        "when this is empty.",
        LANGUAGES, "no statements will be translated"),
    "list_url": Setting(
        "list_url", "contest list",
        "The Contest Manage / lesson Edit URL this batch's problems are added to "
        "and reordered in. No list step runs when this is empty.",
        None, "the problems will not be added to any list"),
}


def split(spec: str | None) -> list[str]:
    """The values in a comma- or newline-separated selection, in order, no blanks."""
    return [v.strip() for v in (spec or "").replace("\n", ",").split(",") if v.strip()]


def normalise(key: str, spec: str | None) -> tuple[list[str], list[str]]:
    """`(accepted values, unrecognised ones)` for one setting.

    Free-text settings accept whatever they are given — there is no vocabulary to
    check against, and inventing one would reject URLs that work.
    """
    field = FIELDS[key]
    if field.vocabulary is None:
        value = (spec or "").strip()
        return ([value] if value else []), []

    canon = {v.lower(): v for v in field.vocabulary}
    out: list[str] = []
    unknown: list[str] = []
    for item in split(spec):
        if (found := canon.get(item.lower())) is not None:
            if found not in out:
                out.append(found)
        else:
            unknown.append(item)
    return out, unknown


def render(key: str, values: list[str] | tuple[str, ...]) -> str:
    """The stored form. Vocabulary settings keep the vocabulary's own order.

    Not the order the operator ticked them in: a stable form means two identical
    selections compare equal, which is what lets the dashboard tell a changed
    choice from a re-rendered one.
    """
    field = FIELDS[key]
    if field.vocabulary is None:
        return (values[0] if values else "").strip()
    chosen = set(values)
    return ", ".join(v for v in field.vocabulary if v in chosen)


def describe(key: str, spec: str | None) -> str:
    """What to show an operator — including for the two different ways of empty."""
    if spec is None:
        return "(not chosen — using the configured default)"
    return render(key, split(spec)) or f"({FIELDS[key].empty})"


def effective(key: str, chosen: str | None, configured: str) -> str:
    """What this batch actually gets.

    The run's own choice wins, and `""` is a choice: an operator who unticked
    everything asked for none, not for whatever the install happens to be
    configured with. Only "never chosen" inherits.
    """
    return configured if chosen is None else chosen
