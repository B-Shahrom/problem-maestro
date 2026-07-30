"""`MANIFEST.json` validation — the cross-checks from `MANIFEST_SPEC.md` §3.

The manifest is the completion sentinel: its presence means every other file in
the set folder is final, and it is written only after the author's own PREFLIGHT
passes. These checks are deliberately redundant with that PREFLIGHT — theirs runs
on the authoring side, ours runs here, and neither should be trusted alone.

Checks are numbered M-1…M-14 to match the spec, so a failure names the same thing
in both documents.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any

from .checks import Finding, Severity

SUPPORTED_SCHEMA = {"1.0"}

#: `CHARACTERISTICS_SPEC.md` §5. A limit equal to these needs no justification;
#: anything else does, which is what makes an intentional bump legible.
DEFAULT_TL_S = 1.0
DEFAULT_ML_MB = 256
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

#: `CHARACTERISTICS_SPEC.md` §4, frozen at v1.0. Covers the **algorithmic** axis only.
#: ElectiCode's category field also carries curricular labels (`academy exam`), but
#: those are platform state, not authored metadata — a manifest tag outside this set
#: means the author drifted, which is what M-9 is for.
VOCABULARY = frozenset({
    "implementation", "math", "brute force", "greedy",
    "observation", "arrays", "strings", "data structures",
    "bitmasks", "hashing", "sortings", "binary search",
    "ternary search", "two pointers", "sliding window", "prefix sums",
    "dp", "divide and conquer", "meet-in-the-middle", "constructive algorithms",
    "simulation", "combinatorics", "number theory", "probabilities",
    "games", "graphs", "trees", "dfs and similar",
    "shortest paths", "dsu", "graph matchings", "flows",
    "interactive", "geometry", "matrices", "string suffix structures",
    "expression parsing", "fft", "2-sat", "chinese remainder theorem",
    "schedules", "backtracking",
})

_BINARY_MAGIC = (b"\x7fELF", b"MZ", b"\xfe\xed\xfa", b"\xcf\xfa\xed\xfe")
_FORBIDDEN_NAMES = ("__MACOSX", ".DS_Store", ".git/", ".pyc")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load(set_dir: Path) -> dict[str, Any]:
    return json.loads((Path(set_dir) / "MANIFEST.json").read_text(encoding="utf-8"))


def validate(set_dir: str | Path, *, extra_tags: set[str] | None = None) -> list[Finding]:
    """Run M-1…M-14 over a set folder. Returns findings; empty means clean.

    `extra_tags` widens the M-9 vocabulary for a set that deliberately carries
    labels outside the frozen list.
    """
    set_dir = Path(set_dir)
    out: list[Finding] = []

    def err(check: str, msg: str, slug: str | None = None) -> None:
        out.append(Finding(check, Severity.ERROR, msg, slug))

    def warn(check: str, msg: str, slug: str | None = None) -> None:
        out.append(Finding(check, Severity.WARN, msg, slug))

    mf = set_dir / "MANIFEST.json"
    if not mf.is_file():
        err("M-0", "no MANIFEST.json — the set is not finished, do not ingest")
        return out
    try:
        m = json.loads(mf.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        err("M-0", f"MANIFEST.json is not valid JSON: {e}")
        return out

    # M-1 — an unknown schema is a halt, not a best-effort read.
    version = str(m.get("schema_version", ""))
    if version not in SUPPORTED_SCHEMA:
        err("M-1", f"unsupported schema_version {version!r}; this build understands {sorted(SUPPORTED_SCHEMA)}")
        return out

    st = m.get("set", {})
    problems: list[dict[str, Any]] = m.get("problems", [])

    # M-4
    if st.get("problem_count") != len(problems):
        err("M-4", f"set.problem_count={st.get('problem_count')} but {len(problems)} problems listed")

    # M-14 — a partial re-delivery is an update, never a fresh import.
    partial = st.get("delivery") == "partial"
    if partial:
        warn("M-14", "delivery=partial — route to the update path, not fresh import")

    # M-5
    seen: set[str] = set()
    for p in problems:
        slug = p.get("slug", "")
        if slug in seen:
            err("M-5", "duplicate slug", slug)
        seen.add(slug)
        if not SLUG_RE.match(slug):
            err("M-5", f"slug does not match {SLUG_RE.pattern}", slug)
        if slug.endswith("-tests"):
            err("M-5", "slug ends in -tests, which is reserved for the tests archive", slug)

    # M-2 / M-11 / M-12
    referenced: set[str] = set()
    for p in problems:
        slug = p.get("slug", "")
        for key, check in (("archive", "M-2"), ("tests_archive", "M-11")):
            spec = p.get(key)
            if spec is None:
                if key == "archive" and not partial:
                    err("M-2", "archive is null in a full delivery", slug)
                continue
            name = spec.get("filename", "")
            referenced.add(name)
            path = set_dir / name
            if not path.is_file():
                err(check, f"{key} {name!r} is missing from the set folder", slug)
                continue
            if (actual := path.stat().st_size) != spec.get("bytes"):
                # Stop here. A wrong-length file will also fail its checksum and may
                # not open as a zip, but neither adds information — and reporting them
                # would drown the one signal that distinguishes a copy still in flight
                # from a file that is genuinely corrupt.
                err(check, f"{name}: {actual} bytes on disk, manifest says {spec.get('bytes')}", slug)
                continue
            if (digest := _sha256(path)) != spec.get("sha256"):
                err(check, f"{name}: sha256 {digest[:12]}… does not match the manifest", slug)
                continue
            if key == "archive":
                out.extend(_inspect_archive(path, slug))

    # M-3 — an orphan zip means the manifest does not describe the folder.
    for zpath in sorted(set_dir.glob("*.zip")):
        if zpath.name not in referenced:
            err("M-3", f"{zpath.name} is on disk but not referenced by any manifest entry")

    # M-6
    ch = m.get("characteristics", {})
    cpath = set_dir / ch.get("filename", "characteristics.md")
    if not cpath.is_file():
        err("M-6", f"{cpath.name} is missing")
    elif (digest := _sha256(cpath)) != ch.get("sha256"):
        err("M-6", f"{cpath.name}: sha256 {digest[:12]}… does not match the manifest")

    # M-9
    allowed = VOCABULARY | (extra_tags or set())
    for p in problems:
        for tag in p.get("tags", []):
            if tag not in allowed:
                err("M-9", f"tag {tag!r} is outside the closed vocabulary — it would be "
                           "created silently in the tag store", p.get("slug"))

    # M-10
    for p in problems:
        subs = p.get("subtasks") or []
        if not subs:
            continue
        slug = p.get("slug")
        for s in subs:
            if s.get("id") == 0 and s.get("points") != 0:
                err("M-10", f"subtask 0 carries {s.get('points')} points; samples must be worth 0", slug)
        total = sum(s.get("points", 0) for s in subs if s.get("id") != 0)
        if total != 100:
            err("M-10", f"non-sample subtask points sum to {total}, not 100", slug)

    # M-15 / M-16 — the limits the author justified, against the measurement
    # they justified them with.
    #
    # Both fields are in the schema and neither was checked. That is the whole
    # reason this pair exists: a limit is the one authored value that fails
    # nothing when it is wrong. It passes import, build, verify, upload and
    # audit, and surfaces weeks later as a TLE on a correct submission.
    for p in problems:
        lim = p.get("limits") or {}
        slug = p.get("slug")
        tl, ml = lim.get("time_limit_s"), lim.get("memory_limit_mb")
        measured = lim.get("measured_worst_s")

        # M-15 — a deviation from the default without a stated reason is
        # indistinguishable from a typo, which is exactly why the spec requires
        # the rationale rather than merely inviting it.
        non_default = (tl is not None and float(tl) != DEFAULT_TL_S) or \
                      (ml is not None and int(ml) != DEFAULT_ML_MB)
        if non_default and not (lim.get("limits_rationale") or "").strip():
            err("M-15", f"TL {tl}s / ML {ml}MB departs from the default "
                        f"({DEFAULT_TL_S:g}s / {DEFAULT_ML_MB}MB) with no limits_rationale — "
                        f"an intentional bump and a typo look identical without one", slug)

        if measured is None or tl is None:
            continue
        measured, tl = float(measured), float(tl)

        # M-16 — the reference solution has to fit inside its own limit, and by
        # a margin. `CHARACTERISTICS_SPEC` §5's worked example is 0.81s under a
        # 2s limit: 2.5x. Below 2x the problem is one slower judge away from
        # failing its own intended solution.
        if measured >= tl:
            err("M-16", f"the reference solution's measured worst case ({measured:g}s) is not "
                        f"inside its own time limit ({tl:g}s) — the intended solution TLEs. "
                        f"This is the author's own machine; the platform's is a different one, "
                        f"so it can only be worse", slug)
        elif measured * 2 > tl:
            warn("M-16", f"measured worst case {measured:g}s against a {tl:g}s limit is only "
                         f"{tl / measured:.1f}x margin; the spec's own example targets 2.5x, and "
                         f"a correct solution this close fails on a slower judge day", slug)

    # M-13
    pf = m.get("preflight", {})
    if pf.get("status") != "pass" or pf.get("checks_failed") != 0:
        err("M-13", f"preflight is {pf.get('status')!r} with {pf.get('checks_failed')} failures")
    if waivers := pf.get("waivers"):
        warn("M-13", f"preflight carries {len(waivers)} waiver(s) — needs human acknowledgement: {waivers}")

    return out


def _inspect_archive(path: Path, slug: str) -> list[Finding]:
    """M-12 — open the archive and check its shape without extracting it."""
    out: list[Finding] = []
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            roots = {n.split("/", 1)[0] for n in names if n.strip("/")}
            if roots != {slug}:
                out.append(Finding("M-12", Severity.ERROR,
                                   f"expected exactly one root folder {slug!r}, found {sorted(roots)}", slug))
            for n in names:
                if n.startswith("/") or ".." in Path(n).parts:
                    out.append(Finding("M-12", Severity.ERROR, f"unsafe path {n!r}", slug))
                    continue
                if any(bad in n for bad in _FORBIDDEN_NAMES) or n.endswith(".zip"):
                    out.append(Finding("M-12", Severity.ERROR, f"forbidden entry {n!r}", slug))
                base = n.rsplit("/", 1)[-1]
                if base and (base != base.lower() or " " in base or not base.isascii()):
                    out.append(Finding("M-12", Severity.ERROR,
                                       f"entry name must be lowercase ASCII without spaces: {n!r}", slug))
                if n.endswith("/"):
                    continue
                with z.open(n) as f:
                    head = f.read(4)
                if head.startswith(_BINARY_MAGIC):
                    out.append(Finding("M-12", Severity.ERROR, f"compiled binary in archive: {n!r}", slug))
    except zipfile.BadZipFile as e:
        out.append(Finding("M-12", Severity.ERROR, f"{path.name} is not a readable zip: {e}", slug))
    return out
