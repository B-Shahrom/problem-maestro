"""`characteristics.md` — parse as `batch.py` parses, then check what it cannot.

The parser here deliberately mirrors `batch.py:131` (`parse_characteristics`) and
`batch.py:146` (`build_plan`) rather than reading the file the way a human would.
The point is to predict *what the chore runner will actually do*, so a divergence
between the two would defeat the exercise.

That matters because the real parser is **lenient and never raises**. A malformed
file does not fail — it silently does less: a mistyped heading yields zero
problems and exit 0; a tag/row count mismatch drops every tag and exits 0. None of
those have a failure signal anywhere downstream, and the ElectiCode audit is
presence-only, so a row with difficulty set but no tags looks healthy. If Maestro
does not catch them here, nothing will.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .checks import Finding, Severity

_TITLE_RE = re.compile(r"^#\s+Characteristics\s*[—:-]\s*(.+)$", re.M)
#: Case-**sensitive**, unlike the tags heading. `## general` finds nothing, which
#: yields zero problems and a clean exit — a whole batch no-ops and reports success.
_GENERAL_RE = re.compile(r"^##\s+General\s*$", re.M)
_TAGS_RE = re.compile(r"^##\s+Suggested tags\s*$", re.M | re.I)
_TAG_LINE_RE = re.compile(r"^\s*\d+\.\s*(.*\S)\s*$")
_NONE_TOKENS = {"none", "-", "", "[none]", "n/a"}
_DIFF = {"easy": "Easy", "medium": "Medium", "hard": "Hard"}
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _number(cell: str) -> float | None:
    """The leading number in a `2 s` / `256 MB` cell, or None if there isn't one."""
    m = _NUM_RE.search(cell or "")
    return float(m.group()) if m else None


@dataclass(slots=True)
class Row:
    slug: str
    title: str = ""
    group: str = ""
    subtasks: str = ""
    languages: str = ""
    tl: str = ""
    ml: str = ""
    """Time and memory limits, verbatim (`2 s`, `256 MB`).

    `batch.py` parses neither — nothing in the ElectiCode half can set a limit,
    because the platform renders both read-only from the imported package. They
    are captured here purely so C-7 can cross-check them against the manifest,
    which *is* what Maestro sends to Polygon. Two authored copies of the same
    fact, and until C-7 nothing compared them.
    """

    @property
    def difficulty(self) -> str:
        """What `batch.py` will send. An unrecognised group silently yields ''."""
        return _DIFF.get(self.group.strip().lower(), "")

    @property
    def time_limit_s(self) -> float | None:
        return _number(self.tl)

    @property
    def memory_limit_mb(self) -> float | None:
        return _number(self.ml)

    @property
    def has_subtasks(self) -> bool:
        return self.subtasks.strip().lower() not in _NONE_TOKENS


@dataclass(slots=True)
class Characteristics:
    name: str = ""
    rows: list[Row] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    general_found: bool = False

    @property
    def tags_will_apply(self) -> bool:
        """`build_plan` drops every tag when the counts disagree (`batch.py:195-197`)."""
        return bool(self.tags) and len(self.tags) == len(self.rows)


def parse(text: str) -> Characteristics:
    out = Characteristics()
    if m := _TITLE_RE.search(text):
        out.name = m.group(1).strip()

    lines = text.splitlines()
    if g := _GENERAL_RE.search(text):
        out.general_found = True
        start = text[: g.start()].count("\n") + 1
        table = []
        for ln in lines[start:]:
            s = ln.strip()
            if s.startswith("|"):
                table.append(s)
            elif table:
                break
        # header + separator + ≥1 data row
        if len(table) >= 3:
            headers = [c.strip().lower() for c in table[0].strip("|").split("|")]
            for raw in table[2:]:
                cells = [c.strip() for c in raw.strip("|").split("|")]
                get = lambda k: cells[headers.index(k)] if k in headers and headers.index(k) < len(cells) else ""  # noqa: E731
                slug = get("slug")
                if slug.strip().lower() in _NONE_TOKENS:
                    continue  # the only column whose absence drops a row
                out.rows.append(Row(slug=slug, title=get("title"), group=get("group"),
                                    subtasks=get("subtasks"), languages=get("languages"),
                                    tl=get("tl"), ml=get("ml")))

    if t := _TAGS_RE.search(text):
        start = text[: t.start()].count("\n") + 1
        for ln in lines[start:]:
            s = ln.strip()
            if s.startswith("#") or s == "---":
                break
            if s == "[none]":
                out.tags = []
                break
            if m := _TAG_LINE_RE.match(ln):
                out.tags.append(m.group(1).strip())
    return out


def subset(c: Characteristics, slugs: Iterable[str]) -> Characteristics:
    """The same document restricted to `slugs`, preserving order.

    Stage 7 needs this twice over. `batch.py --tags-mode` is a **whole-run**
    setting while `exists` is per-problem, so a batch mixing fresh and
    pre-existing problems has to be split into two invocations. Quarantined
    problems have to come out of both — they were never uploaded, so choring them
    would target a slug that does not exist on the platform.

    Tags travel with their rows or not at all. `build_plan` pairs tag line *k* to
    row *k* positionally, so filtering rows without filtering tags in lockstep
    would mis-tag every problem after the first gap. When the counts already
    disagree the tags were going to be dropped wholesale anyway, so the subset
    drops them explicitly rather than inventing an alignment.
    """
    keep = set(slugs)
    aligned = c.tags_will_apply
    rows, tags = [], []
    for i, row in enumerate(c.rows):
        if row.slug not in keep:
            continue
        rows.append(row)
        if aligned:
            tags.append(c.tags[i])
    return Characteristics(name=c.name, rows=rows, tags=tags,
                           general_found=c.general_found)


_COLUMNS = ("#", "slug", "title", "group", "subtasks", "languages")


def render(c: Characteristics) -> str:
    """Emit a document `batch.py parse_characteristics` reads back unchanged.

    A cell containing `|` is refused rather than escaped: the real parser splits
    rows on a bare `|` with no escape syntax at all, so any accommodation here
    would produce a file that round-trips through Maestro and then silently
    mis-parses in the tool that matters.
    """
    def cell(value: str, slug: str, column: str) -> str:
        if "|" in value:
            raise ValueError(
                f"{slug}: the {column} contains '|', which would split the "
                f"characteristics table into the wrong columns: {value!r}"
            )
        return value

    out = [f"# Characteristics — {c.name}" if c.name else "# Characteristics — batch", ""]
    out += ["## General", "",
            "| " + " | ".join(_COLUMNS) + " |",
            "|" + "|".join("---" for _ in _COLUMNS) + "|"]
    for i, r in enumerate(c.rows, 1):
        out.append("| " + " | ".join([
            str(i),
            cell(r.slug, r.slug, "slug"),
            cell(r.title, r.slug, "title"),
            cell(r.group, r.slug, "group"),
            cell(r.subtasks or "none", r.slug, "subtasks"),
            cell(r.languages, r.slug, "languages"),
        ]) + " |")

    # Tag lines are bullets, not table cells, so `|` is safe in them.
    out += ["", "## Suggested tags", ""]
    out += [f"{i}. {t}" for i, t in enumerate(c.tags, 1)] or ["[none]"]
    return "\n".join(out) + "\n"


def precheck(char_path: str | Path, manifest: dict[str, Any]) -> list[Finding]:
    """Validate the file against the manifest before `batch.py` is invoked.

    Checks are numbered C-1…C-6 after `characteristics-diff.md` §3.
    """
    char_path = Path(char_path)
    out: list[Finding] = []

    def err(check: str, msg: str, slug: str | None = None) -> None:
        out.append(Finding(check, Severity.ERROR, msg, slug))

    if not char_path.is_file():
        err("C-1", f"{char_path.name} is missing")
        return out
    c = parse(char_path.read_text(encoding="utf-8"))

    # C-1 — a mistyped heading is a silent no-op, so name it explicitly.
    if not c.general_found:
        err("C-1", "no '## General' heading — the heading is case-sensitive, so a batch "
                   "would find zero problems and exit 0 having done nothing")
        return out
    if not c.rows:
        err("C-1", "'## General' found but no data rows parsed (the table needs a header, "
                   "a separator and at least one row)")
        return out

    mproblems: list[dict[str, Any]] = manifest.get("problems", [])
    by_slug = {p.get("slug"): p for p in mproblems}

    # C-2 — the highest-value check: a mismatch drops *every* tag and still exits 0.
    if c.tags and len(c.tags) != len(c.rows):
        err("C-2", f"{len(c.tags)} tag line(s) against {len(c.rows)} General row(s) — "
                   "batch.py would drop ALL tags and still exit 0")
    elif not c.tags:
        out.append(Finding("C-2", Severity.WARN, "no '## Suggested tags' entries — no tags will be applied"))

    # C-3 — an unrecognised group silently means no difficulty.
    for r in c.rows:
        if not r.difficulty:
            err("C-3", f"group {r.group!r} is not easy/medium/hard — the row would get "
                       "no difficulty, silently", r.slug)

    # C-4 — both directions, not just equal counts.
    cslugs = [r.slug for r in c.rows]
    if dupes := {s for s in cslugs if cslugs.count(s) > 1}:
        err("C-4", f"duplicate slug(s) in the General table: {sorted(dupes)}")
    for missing in sorted(set(by_slug) - set(cslugs)):
        err("C-4", "in the manifest but not in characteristics.md", missing)
    for extra in sorted(set(cslugs) - set(by_slug)):
        err("C-4", "in characteristics.md but not in the manifest", extra)

    # C-5 — the manifest is authoritative for pairing; file order is not.
    #
    # batch.py aligns tag line k to row k *by position*, and its line regex captures
    # everything after the digit without checking it — so `1. / 3. / 2.` still aligns
    # 1→row1, 3→row2, 2→row3. Both `idx` and the printed numbers are decorative. A
    # hand-edit that reorders rows without moving their tag lines mis-tags silently.
    if c.tags_will_apply:
        for row, tagline in zip(c.rows, c.tags, strict=True):
            expected = ", ".join(by_slug.get(row.slug, {}).get("tags", []))
            if expected and tagline.strip() != expected:
                err("C-5", f"tag line {tagline!r} does not match the manifest's "
                           f"{expected!r} at this position", row.slug)

    # C-7 — the two authored copies of the limits must agree, because only one of
    # them is ever acted on.
    #
    # The manifest's `limits` is what Maestro sends to Polygon on import. The
    # characteristics' TL/ML columns are read by nothing at all: `batch.py` does not
    # parse them, and ElectiCode renders both fields read-only from the imported
    # package. So a disagreement resolves silently in the manifest's favour, and the
    # authored intent in the characteristics is lost without a trace.
    #
    # That matters because a wrong limit is invisible for a long time. It does not
    # fail the import, the build, the verify, or the audit — it fails *solutions*,
    # later, as unexplained TLEs on correct submissions.
    for r in c.rows:
        lim = by_slug.get(r.slug, {}).get("limits") or {}
        for label, got, want in (("TL", r.time_limit_s, lim.get("time_limit_s")),
                                 ("ML", r.memory_limit_mb, lim.get("memory_limit_mb"))):
            if want is None:
                continue
            if got is None:
                err("C-7", f"the manifest sets {label} {want}, but characteristics.md has no "
                           f"readable {label} — the column is decorative, so this goes "
                           "unnoticed", r.slug)
            elif float(got) != float(want):
                err("C-7", f"{label}: characteristics.md says {got:g}, the manifest says "
                           f"{want:g} — the manifest wins and the difference is silent", r.slug)

    # C-6 — `languages` is captured by the parser and never used; translate targets
    # come from a single global --targets flag, so a set with mixed language sets
    # cannot be expressed in one batch.py run.
    langs = {tuple(p.get("languages", [])) for p in mproblems}
    if len(langs) > 1:
        err("C-6", f"problems declare {len(langs)} different language sets ({sorted(langs)}) — "
                   "batch.py applies one global --targets, so this run must be split")

    return out
