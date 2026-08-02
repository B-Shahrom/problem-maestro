"""The nine division names, and nothing else.

Division access is one of the three values a batch chooses for itself — see
`settings.py`, which holds the choosing. What lives here is only the vocabulary,
because the vocabulary is the part that belongs to somebody else.

It is mirrored from the Scraper's `division_access.py`, and it is closed. That
matters because `division set` exits `1` on a name it does not know, and it does
so at the *end* of the chore chain, after `fixmdx` and `metadata` have already
run and been paid for. A picker that cannot express a bad name is worth more
than a message explaining one.

Kept in its own module rather than inside `settings` so that the one thing owned
by another repo has one place to be wrong, and one test pinning it there. The
helpers that used to be here — split, normalise, render, describe — moved to
`settings`, which does the same job for all three per-batch settings. Two copies
of the canonicalisation meant the round-trip pin against the Scraper could hold
on a copy nothing ran.
"""

from __future__ import annotations

#: The nine divisions, in the order the platform's modal lists them.
#: `test_scraper_roundtrip.py` fails if this and `division_access.DIVISIONS`
#: ever disagree, because a name Maestro offers that the Scraper does not know
#: is a chore chain that dies at its last step.
DIVISIONS = ("Tier 3", "Tier 2", "Tier 1", "Electi",
             "Division D", "Division C", "Division B", "Division A", "Division A+")
