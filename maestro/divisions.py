"""Which divisions a batch is granted to, chosen per batch rather than per install.

Division access was a single config value, which made it a property of the
*installation* when it is really a property of the *delivery*: one set is for
Electi, the next is a Tier 3 course, the one after that is for nobody yet.
Editing `config.json` and restarting between batches is the wrong shape, and it
is also the shape that produced a run with no divisions at all — the value was
simply left at its default and the chore step vanished from the plan.

So a run carries its own selection, and the config value is only the default it
starts from. Three states, and the difference between the last two matters:

* `None`  — never chosen. Inherit whatever the config says.
* `""`    — chosen, deliberately none. Do not grant, and do not fall back.
* `"..."` — grant exactly these.

The vocabulary is closed and mirrored from the Scraper's `division_access.py`.
It is nine fixed names on the platform's own modal, so an operator picking from
a list cannot produce the failure a free-text field invites: `division set`
exits `1` on an unknown name, which happens *minutes* into a chore chain, after
`fixmdx` and `metadata` have already run and been paid for.
"""

from __future__ import annotations

#: The nine divisions, in the order the platform's modal lists them.
#: Mirrored from `division_access.DIVISIONS`; `test_scraper_roundtrip.py` fails
#: if the two ever disagree, because a name Maestro offers that the Scraper does
#: not know is a chore chain that dies at its last step.
DIVISIONS = ("Tier 3", "Tier 2", "Tier 1", "Electi",
             "Division D", "Division C", "Division B", "Division A", "Division A+")

_CANON = {d.lower(): d for d in DIVISIONS}


def split(spec: str | None) -> list[str]:
    """The names in a comma- or newline-separated selection, in order, no blanks."""
    return [d.strip() for d in (spec or "").replace("\n", ",").split(",") if d.strip()]


def normalise(spec: str | None) -> tuple[list[str], list[str]]:
    """`(canonical names, unrecognised names)` — matched case-insensitively.

    The same mapping `division_access._normalize_divisions` applies, done here so
    a typo is caught when it is *chosen* rather than when it is *acted on*. The
    tool's own rejection is correct but arrives at the end of the chore chain,
    with the earlier steps already applied and paid for.
    """
    out: list[str] = []
    unknown: list[str] = []
    for item in split(spec):
        if (canon := _CANON.get(item.lower())) is not None:
            if canon not in out:
                out.append(canon)
        else:
            unknown.append(item)
    return out, unknown


def render(names: list[str] | tuple[str, ...]) -> str:
    """The stored form: canonical names, modal order, comma-separated."""
    chosen = {n for n in names}
    return ", ".join(d for d in DIVISIONS if d in chosen)


def describe(spec: str | None) -> str:
    """What to show an operator, including for the two ways of having none."""
    if spec is None:
        return "(not chosen — using the configured default)"
    return render(split(spec)) or "(none — no division access will be granted)"
