"""Stage 0 — the instruction that goes *to* the author.

The brief is the other half of `feedback.py`. That one turns a rejection into
something actionable; this one tries to prevent the rejection, by stating the
per-set parameters and the rules most often broken before any work starts.

The design constraint is the same one that shapes everything else here: **it must
not be able to drift from the gate.** A hand-written brief saying "use tags from
this list" is correct on the day it is written and wrong the day the vocabulary
changes, and nothing anywhere notices — the author keeps working from a stale
copy and Maestro keeps rejecting sets for a rule the author was never given.

So the brief is *generated* from the constants the checkers actually use.
`VOCABULARY`, `SLUG_RE` and `SUPPORTED_SCHEMA` are read from `manifest`, not
retyped, which makes the two physically the same fact. A test pins that.

What it deliberately does **not** do is restate the contracts. Those are long,
they already exist as documents, and a summary of a contract is a second source
of truth for it. `--with-contracts` inlines them verbatim for a session that does
not have them; otherwise the brief names them and stops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .checks import Finding, Severity
from .manifest import SLUG_RE, SUPPORTED_SCHEMA, VOCABULARY

#: The contracts an author works from, in the order they need them. Named rather
#: than summarised — see the module docstring.
CONTRACTS = (
    ("OUTPUT_CONTRACT.md", "what a set folder and each archive must contain"),
    ("CHARACTERISTICS_SPEC.md", "the difficulty rubric, the tag vocabulary, the limits rule"),
    ("MANIFEST_SPEC.md", "the manifest schema and the write protocol"),
    ("PREFLIGHT.md", "the checks to run before writing MANIFEST.json"),
)

GROUPS = ("easy", "medium", "hard")


@dataclass(frozen=True, slots=True)
class Brief:
    """One set's parameters. Everything else in the brief is derived."""

    name: str
    """`set.name` in the manifest, and the run's identity in Maestro forever."""

    prefix: str = ""
    """Shared slug prefix. Drives the per-prefix sections of `characteristics.md`."""

    topic: str = ""
    mix: dict[str, int] = field(default_factory=dict)
    """Problems per difficulty group. The count is its sum — there is no separate
    total to disagree with it."""

    languages: tuple[str, ...] = ("EN",)
    notes: str = ""

    @property
    def count(self) -> int:
        return sum(self.mix.values())


def parse_mix(spec: str) -> dict[str, int]:
    """`easy=2,medium=2,hard=1` — or `2:2:1` in easy/medium/hard order."""
    spec = spec.strip()
    if not spec:
        return {}
    if ":" in spec and "=" not in spec:
        parts = spec.split(":")
        if len(parts) != len(GROUPS):
            raise ValueError(f"a positional mix needs {len(GROUPS)} parts "
                             f"({'/'.join(GROUPS)}), got {len(parts)}")
        return {g: int(n) for g, n in zip(GROUPS, parts, strict=True) if int(n)}
    out: dict[str, int] = {}
    for item in spec.split(","):
        group, _, n = item.partition("=")
        group = group.strip().lower()
        if group not in GROUPS:
            raise ValueError(f"unknown difficulty group {group!r}; expected one of "
                             f"{', '.join(GROUPS)}")
        out[group] = int(n)
    return {g: n for g, n in out.items() if n}


def check(brief: Brief, existing: set[str] | None = None) -> list[Finding]:
    """Everything wrong with the brief itself, before it is sent. Checks B-1…B-4.

    Cheap, and each one is a rejection that would otherwise happen after the set
    was authored — which is the expensive place to discover that a name was
    already taken or a prefix cannot appear in a legal slug.
    """
    out: list[Finding] = []

    def err(code: str, msg: str) -> None:
        out.append(Finding(code, Severity.ERROR, msg))

    # B-1 — `set_name` is UNIQUE in the store, and ingest returns ALREADY_INGESTED
    # for a repeat. Authoring a whole set against a taken name means delivering it
    # into silence.
    if not brief.name:
        err("B-1", "the set has no name")
    elif existing and brief.name in existing:
        err("B-1", f"a run named {brief.name!r} already exists — pick another name, "
                   "or a redelivery of that set rather than a new brief")

    # B-2 — the prefix has to be able to *start* a legal slug, or every slug the
    # author derives from it fails M-5 after the fact.
    if brief.prefix and not SLUG_RE.match(brief.prefix):
        err("B-2", f"prefix {brief.prefix!r} cannot begin a legal slug "
                   f"(must match {SLUG_RE.pattern})")

    # B-3
    if not brief.mix:
        err("B-3", "no difficulty mix — the brief would not say how many problems to write")
    elif brief.count < 1:
        err("B-3", "the difficulty mix sums to zero problems")

    # B-4 — the manifest carries the language identities; a set that mixes them
    # cannot be chored in one pass (C-6), so it is worth refusing here too.
    if not brief.languages:
        err("B-4", "no statement languages")
    return out


def _tags() -> list[str]:
    """The vocabulary, wrapped for reading rather than as one long line."""
    ordered = sorted(VOCABULARY)
    rows, row = [], []
    for tag in ordered:
        row.append(f"`{tag}`")
        if len(row) == 4:
            rows.append(", ".join(row))
            row = []
    if row:
        rows.append(", ".join(row))
    return rows


def render(brief: Brief, *, contracts_dir: Path | str | None = None) -> str:
    """The brief, as markdown. Pure — no clock, no environment.

    `contracts_dir` inlines the contract documents verbatim, for a session that
    does not already carry them. Left out, they are named and not summarised: a
    summary of a contract is a second copy of it, and the two diverge.
    """
    mix = ", ".join(f"{n} {g}" for g, n in brief.mix.items() if n)
    out = [
        f"# Authoring brief — {brief.name}",
        "",
        f"Write **{brief.count}** problem(s): {mix}.",
        "",
    ]
    if brief.topic:
        out += [f"**Topic.** {brief.topic}", ""]

    out += [
        "## Parameters",
        "",
        "| | |",
        "|---|---|",
        f"| `set.name` | `{brief.name}` |",
        f"| Slug prefix | {f'`{brief.prefix}-…`' if brief.prefix else '(none — pick per problem)'} |",
        f"| Statement languages | {', '.join(brief.languages)} — the same set for **every** problem |",
        f"| `schema_version` | {', '.join(sorted(SUPPORTED_SCHEMA))} |",
        f"| Slug pattern | `{SLUG_RE.pattern}` |",
        "",
    ]
    if brief.notes:
        out += [brief.notes.strip(), ""]

    out += [
        "## Deliverable",
        "",
        f"One folder named `{brief.name}` containing, per problem, `{{slug}}.zip`, plus "
        "`characteristics.md` and `MANIFEST.json` for the set. Write `MANIFEST.json` "
        "**last**, by atomic rename, and only after your own PREFLIGHT passes — its "
        "presence is what tells Maestro the folder is finished.",
        "",
        "Work to these, in this order:",
        "",
    ]
    out += [f"{i}. **{name}** — {what}" for i, (name, what) in enumerate(CONTRACTS, 1)]
    out += [""]

    out += [
        "## The five that get sets rejected",
        "",
        "Not a summary of the contracts — these are the specific rules Maestro's gate "
        "has caught most often, each of which fails **silently** further down if it "
        "gets past.",
        "",
        "1. **One tag line per General row, in row order.** A count mismatch makes the "
        "chore runner drop *every* tag in the set and still exit 0. Alignment is "
        "positional — the printed numbers are not read — so moving a row means moving "
        "its tag line.",
        "2. **Tags come from the closed vocabulary below.** An unknown tag is not "
        "rejected by the platform; it is created in the tag store and then exists for "
        "everyone, forever.",
        "3. **TL/ML must agree between `MANIFEST.json` and `characteristics.md`.** Only "
        "the manifest is ever acted on and the platform renders both fields read-only, "
        "so a disagreement resolves silently in the manifest's favour and surfaces "
        "later as unexplained TLE on a correct solution.",
        "4. **A non-default limit needs `limits_rationale`, from a measurement.** "
        "Default is TL 1 s / ML 256 MB. \"Measured worst case 0.81 s, 2.5× margin\" is "
        "a rationale; \"looks slow\" is not.",
        "5. **Each archive holds exactly one root folder, named for the slug.** The "
        "importer takes the problem's identity from that folder name, not from the "
        "filename and not from the manifest.",
        "",
        "## Tag vocabulary (closed)",
        "",
        f"{len(VOCABULARY)} tags. Anything outside this list fails at the gate.",
        "",
    ]
    out += [f"- {row}" for row in _tags()]
    out += [
        "",
        "## Difficulty",
        "",
        "Score the five axes of `CHARACTERISTICS_SPEC.md` §3.1 (0–3 each), sum to 0–15, "
        "and look the group up: **0–4 easy, 5–9 medium, 10–15 hard**. `idx` is the "
        "§3.3 four-key sort, not authoring order.",
        "",
        f"The mix above ({mix}) is a target for the *scored* groups. If the scores come "
        "out differently, deliver the scores and say so in the set notes — a rubric "
        "bent to hit a quota is worth less than the quota.",
        "",
        "## If the set is rejected",
        "",
        f"Maestro writes a correction request named `{brief.name}.REJECTED.md` beside "
        "the folder. It names the contract clause behind each finding, and lists the "
        "checks that did **not** run — a manifest-level rejection stops before the "
        "characteristics and importer checks, so that report is not a complete account "
        "of the set.",
        "",
        f"Correct it into a **new** folder named `{brief.name}-r2`. Do not edit the "
        "rejected one in place: the watcher reads it on a timer and would see it "
        "mid-fix.",
        "",
    ]

    if contracts_dir is not None:
        out += _inline(Path(contracts_dir))
    return "\n".join(out)


def _inline(contracts_dir: Path) -> list[str]:
    """The contract documents, verbatim, for a session that lacks them.

    A missing file is reported in place rather than skipped. Silently shipping a
    brief with three of four contracts would produce a set authored against a
    contract the author never saw, which is the failure this whole module is
    arranged to avoid.
    """
    out = ["---", "", "# Contracts", ""]
    for name, _ in CONTRACTS:
        path = contracts_dir / name
        out += [f"## {name}", ""]
        try:
            out += [path.read_text(encoding="utf-8").strip(), ""]
        except OSError as e:
            out += [f"> **MISSING** — {path} could not be read ({e}). This brief is "
                    f"incomplete; do not author against it until this contract is "
                    f"supplied.", ""]
    return out
