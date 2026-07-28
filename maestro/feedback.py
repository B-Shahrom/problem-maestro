"""Turn a rejection into a correction request the author can act on.

Today a rejected set produces one line in a tick report and one row in the
dashboard. That is enough for an operator to know something was refused and
nothing like enough for the person who has to fix it: the finding says what
Maestro *observed* (`C-2: 4 tag line(s) against 5 General row(s)`) but not which
rule was broken, not what the consequence would have been, and not what to change.
The author is a separate actor reached over a chat window, so everything they are
not told has to be re-derived by a human reading Maestro's source.

This module closes that gap by pairing every check code with the contract clause
it enforces. The output is a markdown document written next to the rejected
folder — readable by the author directly today, and the natural payload for an
API-driven author lane later, since a correction request is exactly what that lane
would need to send.

Two properties matter more than the prose:

**It says what was *not* checked.** `inspect()` runs the characteristics and
importer checks only once the manifest is clean, so a manifest-level rejection
leaves the rest of the set entirely unexamined. A report that lists four findings
and stops implies four things are wrong; the truth is four things are wrong *and
the remaining two thirds of the checks never ran*. Fixing the four and expecting
acceptance is then a wasted round trip. `Inspection.checked` records which
families actually executed and `render` reports the absences by name.

**It is stable.** `render` takes no clock and no environment, so re-inspecting an
unchanged folder produces a byte-identical document. `write` compares before
writing, which is what lets the sweep call it every tick — a changed report then
means the set changed, not that time passed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .checks import Finding, Severity
from .ingest import Inspection, Verdict

#: Written beside the rejected folder, not inside it. The set folder is the
#: delivery and Maestro never edits a delivery; a sibling also cannot be mistaken
#: for part of the package if the folder is copied on. `candidates()` yields only
#: directories, so a stray `.md` in the watch folder is never re-inspected.
SUFFIX = ".REJECTED.md"

#: The check families `inspect()` can run, and what each is called in a report.
FAMILIES = {
    "manifest": "the manifest cross-checks (M-*)",
    "characteristics": "the characteristics pre-check (C-*)",
    "importer": "the importer pre-flight (P-*)",
}


@dataclass(frozen=True, slots=True)
class Clause:
    """What a check enforces, where that is written down, and what to change."""

    rule: str
    where: str
    fix: str


#: Check code → authoring guidance. Keyed by the codes the checkers emit; see
#: `test_feedback.py`, which fails if the package grows a code with no entry here.
#: A gap would otherwise degrade silently into "the message is the whole story",
#: which is the failure mode this module exists to remove.
CLAUSES: dict[str, Clause] = {
    "M-0": Clause(
        "`MANIFEST.json` is written last, by atomic rename, and only after your own "
        "PREFLIGHT passes.",
        "MANIFEST_SPEC.md §1 Write protocol",
        "Deliver the manifest. Until it exists Maestro treats the folder as still "
        "being copied and keeps waiting — nothing is lost, but nothing starts either."),
    "M-1": Clause(
        "`schema_version` names a schema this Maestro build understands.",
        "MANIFEST_SPEC.md §2 Schema",
        "Emit one of the versions named in the message. An unknown version is refused "
        "rather than read best-effort, because a field that changed meaning would "
        "otherwise be read with its old one."),
    "M-2": Clause(
        "Every `problems[].archive` exists in the folder, and its `sha256` and `bytes` "
        "match the file on disk.",
        "MANIFEST_SPEC.md §3 M-2",
        "Regenerate the manifest from the archives you actually shipped — hash after "
        "the last write to them, never before. A size mismatch is also how Maestro "
        "recognises a copy still in flight, so a stale size delays the set instead of "
        "rejecting it."),
    "M-3": Clause(
        "Every `.zip` in the folder is referenced by exactly one manifest entry.",
        "MANIFEST_SPEC.md §3 M-3",
        "List the archive in `problems[]` or leave it out of the delivery. An "
        "unreferenced zip is either a problem that will never be imported or a "
        "leftover from an earlier export, and Maestro cannot tell which."),
    "M-4": Clause(
        "`set.problem_count` equals the length of `problems[]`.",
        "MANIFEST_SPEC.md §3 M-4",
        "Derive `problem_count` from the list rather than writing it by hand."),
    "M-5": Clause(
        "`slug` values are unique, match the slug pattern, and none ends in `-tests`.",
        "MANIFEST_SPEC.md §3 M-5",
        "Rename the problem. The slug is the join key for the entire pipeline — "
        "Polygon, ElectiCode and the audit all address the problem by it — so it "
        "cannot be corrected after import without breaking the identity map."),
    "M-6": Clause(
        "`characteristics.md` exists and its `sha256` matches.",
        "MANIFEST_SPEC.md §3 M-6",
        "Re-hash `characteristics.md` after the last edit to it. A mismatch here "
        "almost always means the file was touched after the manifest was written."),
    "M-7": Clause(
        "Every General-table row has a manifest entry and vice versa.",
        "MANIFEST_SPEC.md §3 M-7",
        "Maestro enforces this as C-4; see that entry."),
    "M-8": Clause(
        "Per problem, `group`, `languages`, `tests.total`, `checker`, TL and ML agree "
        "between the manifest and `characteristics.md`.",
        "MANIFEST_SPEC.md §3 M-8",
        "Maestro enforces the limits half as C-7; see that entry."),
    "M-9": Clause(
        "Every `tags[]` entry is in the closed vocabulary.",
        "CHARACTERISTICS_SPEC.md §4 Tags",
        "Use a vocabulary tag, or ask the operator to widen the vocabulary for this "
        "set deliberately. An unknown tag is not rejected by the platform — it is "
        "created in the tag store, silently, and then exists for everyone forever."),
    "M-10": Clause(
        "Non-sample subtask points sum to 100, and subtask 0 is worth 0.",
        "MANIFEST_SPEC.md §3 M-10",
        "Rebalance the subtask points. Subtask 0 is the samples and scores nothing."),
    "M-11": Clause(
        "A non-null `tests_archive` names a file that exists, whose base slug has a "
        "manifest entry.",
        "MANIFEST_SPEC.md §3 M-11",
        "Ship the tests archive named in the manifest, or set `tests_archive` to null."),
    "M-12": Clause(
        "Each archive holds exactly one root folder named `{slug}`, no compiled "
        "binaries, no path traversal, and no uppercase or non-ASCII entry names.",
        "MANIFEST_SPEC.md §3 M-12",
        "Rebuild the archive. The root folder name is what the importer derives the "
        "problem's identity from, so a mismatch there is not cosmetic."),
    "M-13": Clause(
        "`preflight.status` is `pass` with `checks_failed: 0`.",
        "PREFLIGHT.md",
        "Run your own PREFLIGHT and fix what it reports before delivering. Waivers "
        "are permitted but stop the run for an operator to acknowledge, so a set "
        "delivered with waivers is a set that waits."),
    "M-15": Clause(
        "A time or memory limit that departs from the default (1 s / 256 MB) carries "
        "a non-null `limits_rationale`.",
        "CHARACTERISTICS_SPEC.md §5 Time and memory limits",
        "State why, from the measurement: \"TL 2s: reference worst case 0.81s on "
        "n=2·10^5 adversarial, 2.5x margin\". Without one an intentional bump and a "
        "typo are the same edit, and a limit is the one authored value that fails "
        "nothing when it is wrong — not the import, the build, the verify or the "
        "audit, only a correct solution weeks later."),
    "M-16": Clause(
        "The reference solution's `measured_worst_s` fits inside its own "
        "`time_limit_s`, with margin.",
        "CHARACTERISTICS_SPEC.md §5 Time and memory limits",
        "Raise the limit or make the reference faster. At or above the limit the "
        "intended solution TLEs on your own measurement; under 2x margin it passes "
        "today and fails on a slower judge, which is worse because it looks fine "
        "until it does not."),
    "M-14": Clause(
        "`set.delivery == \"partial\"` routes to the update path, not a fresh import.",
        "MANIFEST_SPEC.md §3 M-14",
        "Nothing to fix. This tells the operator which path to take."),

    "C-1": Clause(
        "`characteristics.md` has a `## General` heading followed by a table with a "
        "header row, a separator row, and one row per problem.",
        "CHARACTERISTICS_SPEC.md §2 File layout",
        "Emit the heading exactly — it is matched case-sensitively. A mistyped "
        "heading parses as zero problems, and the chore runner then exits 0 having "
        "done nothing at all, which reads downstream as success."),
    "C-2": Clause(
        "There is exactly one `## Suggested tags` line per General row, in the same "
        "order.",
        "CHARACTERISTICS_SPEC.md §2 File layout",
        "Emit one tag line per row. A count mismatch makes the chore runner drop "
        "*every* tag in the set and still report success — the highest-value check "
        "here, because nothing later notices the tags are missing."),
    "C-3": Clause(
        "Each row's `group` is `easy`, `medium` or `hard`.",
        "CHARACTERISTICS_SPEC.md §3.2 Buckets",
        "Use one of the three buckets. Any other value leaves the difficulty unset "
        "with no error raised."),
    "C-4": Clause(
        "The General table lists exactly the manifest's slugs, once each.",
        "MANIFEST_SPEC.md §3 M-7",
        "Reconcile the two files. A slug in only one of them is a problem that gets "
        "either no metadata or metadata with nothing to attach it to."),
    "C-5": Clause(
        "Tag line *k* belongs to General row *k*.",
        "CHARACTERISTICS_SPEC.md §2 File layout",
        "Move the tag line whenever you move a row. Alignment is positional — the "
        "printed numbers are decorative and the parser does not read them — so "
        "reordering rows alone mis-tags the whole set without any error."),
    "C-6": Clause(
        "All problems in one delivery declare the same language set.",
        "CHARACTERISTICS_SPEC.md §2 File layout",
        "Split the delivery. The chore runner applies a single global `--targets` per "
        "run, so a mixed-language set cannot be expressed in one pass."),
    "C-7": Clause(
        "The TL and ML columns in `characteristics.md` agree with the manifest's "
        "`limits`.",
        "CHARACTERISTICS_SPEC.md §5 Time and memory limits",
        "Make the two copies agree. Only the manifest is ever acted on — the columns "
        "are read by nothing, and no tool Maestro drives can set them yet — so a "
        "disagreement is resolved silently in the manifest's favour and surfaces much "
        "later as unexplained TLE on a correct solution."),

    "P-0": Clause(
        "The manifest is cross-checked against the importer's own dry run.",
        "PREFLIGHT.md",
        "Nothing to fix. The importer was unreachable, so this set was validated "
        "locally only and may still fail at import."),
    "P-1": Clause(
        "Every archive opens in the importer.",
        "MANIFEST_SPEC.md §3 M-12",
        "Re-export the archive as a Polygon package. The importer's parser is what "
        "decides what gets created, so its verdict stands even if the zip opens "
        "elsewhere."),
    "P-2": Clause(
        "The problems the importer would create are exactly the ones the manifest "
        "declares.",
        "MANIFEST_SPEC.md §3 M-12",
        "The importer takes the slug from the archive's internal root folder name, "
        "not from the filename and not from the manifest. Rename that folder."),
    "P-3": Clause(
        "`tests.total` matches the number of tests the importer finds.",
        "MANIFEST_SPEC.md §2 Schema",
        "Regenerate the count from the archive rather than from the generator."),
    "P-4": Clause(
        "`components` matches the checker, solution and validator actually present.",
        "MANIFEST_SPEC.md §2 Schema",
        "Include the component or correct the claim. A missing checker or solution "
        "fails the Polygon build minutes later with an error that never mentions the "
        "manifest."),
    "P-5": Clause(
        "Each archive carries a statement the importer can parse.",
        "OUTPUT_CONTRACT.md",
        "Include a parseable statement. An archive without one imports as an empty "
        "shell that still builds, still verifies, and still uploads."),
    "P-6": Clause(
        "A full delivery contains full problem packages, not tests-only packs.",
        "MANIFEST_SPEC.md §3 M-14",
        "Export the whole problem. A tests-only pack appends tests to an existing "
        "problem instead of creating one, so the statement, checker and solution are "
        "silently absent."),

    "L-1": Clause(
        "The authored time and memory limits are what the platform ends up showing.",
        "CHARACTERISTICS_SPEC.md §5 Time and memory limits",
        "Confirm the manifest's `limits` is what you intended. If it is, this is a "
        "pipeline fault rather than an authoring one: the value is sent at import and "
        "read back from the catalog, so a mismatch means one of those two steps is."),
    "L-2": Clause(
        "The applied limits are verified after upload.",
        "CHARACTERISTICS_SPEC.md §5 Time and memory limits",
        "Nothing to fix. The scrape did not carry the limit fields, so the round trip "
        "could not be closed — and nothing else checks it."),
    "D-1": Clause(
        "Every uploaded problem carries the division access the run asked for.",
        "electicode-fields.md (docs/analysis)",
        "Nothing to fix in the delivery. Re-run the division step for the slugs named."),
    "D-2": Clause(
        "Division access is verified after the chores.",
        "electicode-fields.md (docs/analysis)",
        "Nothing to fix. The scrape did not carry `division_access`, so the grant "
        "could not be confirmed — re-check with a paged scrape."),

    # B-* fire before any authoring starts, so their reader is the operator
    # writing the brief rather than the author correcting a set. They are here
    # anyway: the rule is that no check code in this package exists without
    # guidance, and a prefix carve-out is how that rule would quietly rot.
    "B-1": Clause(
        "A set name is used once. `set_name` is UNIQUE in the store, and a repeat "
        "returns ALREADY_INGESTED.",
        "author-lane.md §1 (docs/analysis)",
        "Pick another name for a new set. If this is a correction to a set that was "
        "already ingested, it is a redelivery, not a new brief — and a redelivery of "
        "an *ingested* set has to be dealt with on the platform, not at the gate."),
    "B-2": Clause(
        "The slug prefix can begin a legal slug.",
        "MANIFEST_SPEC.md §3 M-5",
        "Fix the prefix before sending the brief. Every slug derived from it would "
        "otherwise fail M-5 after the whole set was authored."),
    "B-3": Clause(
        "The brief states how many problems to write, per difficulty group.",
        "author-lane.md §1 (docs/analysis)",
        "Give a difficulty mix. The count is its sum, so there is no separate total "
        "that could disagree with it."),
    "B-4": Clause(
        "The brief names the statement languages.",
        "CHARACTERISTICS_SPEC.md §2 File layout",
        "Name at least one language, and the same set for every problem in the set — "
        "the chore runner applies a single global `--targets` per run (C-6)."),
}

_UNKNOWN = Clause(
    "No authoring guidance is recorded for this check.",
    "—",
    "Treat the message above as the whole instruction, and tell the operator this "
    "code reached a report without a clause — that is a gap in Maestro, not in the set.")

_REV = re.compile(r"-r(\d+)$")


def redelivery_name(set_dir: Path | str) -> str:
    """What the corrected folder should be called.

    A correction arrives as a *new* folder rather than an edit to the rejected
    one, so that the thing Maestro validated and the thing it later imports are
    the same bytes. Editing in place also races the sweep, which may read the
    folder halfway through the fix and reject it for a different reason.
    """
    name = Path(set_dir).name
    if m := _REV.search(name):
        return f"{name[:m.start()]}-r{int(m.group(1)) + 1}"
    return f"{name}-r2"


def _group(findings: list[Finding]) -> list[tuple[str | None, list[Finding]]]:
    """Set-level findings first, then one group per slug, each in check order.

    Grouping by slug rather than by check code follows how the fix is actually
    made: the author opens one problem and corrects everything wrong with it,
    rather than sweeping the set once per rule.
    """
    order: list[str | None] = []
    by_slug: dict[str | None, list[Finding]] = {}
    for f in findings:
        if f.slug not in by_slug:
            by_slug[f.slug] = []
            order.append(f.slug)
        by_slug[f.slug].append(f)
    order.sort(key=lambda s: (s is not None, s or ""))
    return [(s, by_slug[s]) for s in order]


def _item(f: Finding) -> list[str]:
    c = CLAUSES.get(f.check, _UNKNOWN)
    return [
        f"**{f.check}** — {f.message}",
        "",
        f"- *Rule:* {c.rule}",
        f"- *Where:* {c.where}",
        f"- *Fix:* {c.fix}",
        "",
    ]


def _not_checked(insp: Inspection) -> list[str]:
    """The families that did not run, named.

    This is the section the report exists for. `inspect()` gates the
    characteristics and importer checks behind a clean manifest, so a
    manifest-level rejection says nothing whatsoever about the rest of the set —
    and a list of findings with no such note reads as a complete account of what
    is wrong.
    """
    missing = [label for key, label in FAMILIES.items() if key not in insp.checked]
    if not missing:
        return []
    return [
        "## Not checked",
        "",
        "These checks did not run, so this report is **not** a complete account of "
        "the set. Expect a second round.",
        "",
        *[f"- {m}" for m in missing],
        "",
    ]


def render(insp: Inspection) -> str:
    """The correction request, as markdown. Pure — same input, same bytes."""
    name = insp.set_name or insp.set_dir.name
    errs = [f for f in insp.findings if f.severity is Severity.ERROR]
    warns = [f for f in insp.findings if f.severity is Severity.WARN]

    out = [f"# Correction request — {name}", ""]

    if insp.verdict is Verdict.INCOMPLETE:
        out += [
            f"`{insp.set_dir.name}` looks **unfinished rather than wrong**. Maestro will "
            "keep re-reading it, so a delivery still in progress needs nothing from you "
            "except time. If the delivery is in fact complete, the items below are real "
            "and the set will wait forever.",
            "",
        ]
    else:
        out += [
            f"`{insp.set_dir.name}` was refused at the gate. Nothing was imported, "
            "uploaded, or changed anywhere — the set never left the watch folder.",
            "",
        ]

    out += [
        "## How to redeliver",
        "",
        f"Correct the items below and deliver a **new folder** named "
        f"`{redelivery_name(insp.set_dir)}`. Do not edit the rejected folder in place: "
        "the sweep reads it on a timer and would see it mid-fix.",
        "",
        "Re-run your own PREFLIGHT before delivering — every check here is downstream "
        "of one of yours.",
        "",
    ]

    if errs:
        out += [f"## Blocking — {len(errs)}", ""]
        for slug, group in _group(errs):
            out += [f"### {slug or 'the set as a whole'}", ""]
            for f in group:
                out += _item(f)

    if warns:
        out += [
            f"## Advisory — {len(warns)}",
            "",
            "These did not block the set. They are here because each one names "
            "something that fails silently downstream.",
            "",
        ]
        for slug, group in _group(warns):
            out += [f"### {slug or 'the set as a whole'}", ""]
            for f in group:
                out += _item(f)

    if not errs and not warns:
        out += ["No findings were recorded.", ""]

    out += _not_checked(insp)
    out += [
        "---",
        "",
        "*For the operator:* remove or archive the rejected folder once the correction "
        "arrives. The sweep re-reads every folder it has not ingested, so a folder left "
        "in place is re-reported on every tick.",
        "",
    ]
    return "\n".join(out)


def path_for(set_dir: Path | str) -> Path:
    set_dir = Path(set_dir)
    return set_dir.parent / f"{set_dir.name}{SUFFIX}"


def write(insp: Inspection, dest: Path | str | None = None) -> Path | None:
    """Write the report beside the set folder if it says something new.

    Returns the path when the file was created or changed, and `None` when there
    was nothing to say — which is what makes this safe to call from the sweep on
    every tick. A rewritten report therefore means the *set* changed, not that
    another tick went by.

    A set that has become READY has its stale report deleted, for the same
    reason: a rejection notice sitting next to an accepted delivery is worse than
    no notice at all.
    """
    target = Path(dest) if dest else path_for(insp.set_dir)
    if insp.verdict in (Verdict.READY, Verdict.ALREADY_INGESTED):
        if target.is_file():
            target.unlink()
            return target
        return None

    body = render(insp)
    try:
        if target.is_file() and target.read_text(encoding="utf-8") == body:
            return None
        target.write_text(body, encoding="utf-8")
    except OSError:
        # The watch folder can be a read-only mount or a network share having a
        # bad minute. Failing to *explain* a rejection must not turn into failing
        # the sweep that found it — the rejection is already recorded elsewhere.
        return None
    return target
