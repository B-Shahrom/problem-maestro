"""Stage 1→2 — turn a folder on disk into a run.

`MANIFEST.json` is the completion sentinel: the author writes it last, via an
atomic rename, and only after their own PREFLIGHT passes. So its presence is the
signal to look.

Presence alone is not enough here, though. The author's guarantee holds inside
*their* set folder; it says nothing about how the folder reaches Maestro. Today
that path is a human downloading archives out of a chat and dropping them into a
watched directory, in whatever order, over several minutes. The manifest can
easily land first.

That is survivable because completeness is validated **by content, not by arrival
order** — every archive is checked against the size and checksum the manifest
records. What it does require is telling two failures apart:

* **incomplete** — an archive is missing, or shorter than the manifest says. A copy
  is probably still running. Look again later.
* **invalid** — the schema is unknown, slugs are malformed, the author's preflight
  failed, or a file is the right size with the wrong checksum. Waiting will not fix
  any of those. Report once and stop.

Blurring the two gives either a watcher that retries a permanently broken set
forever, or one that rejects a set because it looked while a copy was in flight.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .characteristics import precheck
from .checks import Finding, Severity, errors
from .manifest import validate
from .model import ProblemSeed, RunStage, RunStatus
from .preflight import compare
from .store import Store

#: `PolygonClient.parse` — archives in, a `/api/parse` response out. Taken as a
#: callable rather than a client so ingest depends on the *question*, not on a
#: configured HTTP client it would otherwise have to be handed everywhere.
Parser = Callable[[list[Path]], dict[str, Any]]

#: Findings that mean "the folder is still filling up" rather than "this set is wrong".
_INCOMPLETE_MARKERS = ("is missing from the set folder", "is missing", "bytes on disk")


class Verdict(StrEnum):
    READY = "ready"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"
    ALREADY_INGESTED = "already_ingested"


@dataclass(frozen=True, slots=True)
class Inspection:
    set_dir: Path
    verdict: Verdict
    findings: list[Finding]
    set_name: str = ""
    run_id: int | None = None

    checked: frozenset[str] = frozenset()
    """Which check families actually ran — see `feedback.FAMILIES`.

    The findings alone cannot answer this. Two of the three families are gated
    behind a clean manifest and the third behind a reachable importer, so "no
    P-* findings" means either *the importer agreed* or *the importer was never
    asked*, and those are opposite facts. An empty set is the safe default: a
    caller that forgets to populate it under-claims rather than over-claims.
    """

    @property
    def retryable(self) -> bool:
        return self.verdict is Verdict.INCOMPLETE


def _classify(findings: list[Finding]) -> Verdict:
    errs = errors(findings)
    if not errs:
        return Verdict.READY
    # A single permanent error is decisive, even alongside transient ones: a set with
    # a failed preflight does not become valid once the last archive finishes copying.
    if all(any(mark in f.message for mark in _INCOMPLETE_MARKERS) for f in errs):
        return Verdict.INCOMPLETE
    return Verdict.INVALID


def inspect(set_dir: str | Path, store: Store | None = None, *,
            extra_tags: set[str] | None = None, parser: Parser | None = None) -> Inspection:
    """Validate one candidate set folder without touching it.

    `parser` is the Middleman's `/api/parse` dry run. When given, the manifest is
    also checked against what will *actually* import — see `preflight`. It is
    optional so that ingest keeps working with the Middleman down: a set that
    cannot be pre-flighted is still validated locally, it just loses one gate.
    """
    set_dir = Path(set_dir)
    mf = set_dir / "MANIFEST.json"
    if not mf.is_file():
        return Inspection(set_dir, Verdict.INCOMPLETE,
                          [Finding("M-0", Severity.ERROR, "no MANIFEST.json yet")])

    try:
        name = json.loads(mf.read_text(encoding="utf-8")).get("set", {}).get("name", "")
    except json.JSONDecodeError:
        # Mid-write is possible despite the atomic rename if the copy tool is not atomic.
        return Inspection(set_dir, Verdict.INCOMPLETE,
                          [Finding("M-0", Severity.ERROR, "MANIFEST.json is not parseable yet")])

    if store is not None and name:
        if any(r.set_name == name for r in store.list_runs()):
            return Inspection(set_dir, Verdict.ALREADY_INGESTED, [], name)

    findings = validate(set_dir, extra_tags=extra_tags)
    verdict = _classify(findings)
    ran = {"manifest"}
    # The characteristics pre-check needs a trustworthy manifest to compare against,
    # so it only runs once the manifest itself is clean.
    if verdict is Verdict.READY:
        m = json.loads(mf.read_text(encoding="utf-8"))
        findings += precheck(set_dir / "characteristics.md", m)
        ran.add("characteristics")
        if parser is not None:
            pf, reached = _preflight(set_dir, m, parser)
            findings += pf
            if reached:
                ran.add("importer")
        verdict = _classify(findings)
    return Inspection(set_dir, verdict, findings, name, checked=frozenset(ran))


def _preflight(set_dir: Path, manifest: dict, parser: Parser) -> tuple[list[Finding], bool]:
    """Ask the Middleman what these archives would import as, and compare.

    A parser that is unreachable produces no findings rather than an INVALID
    verdict: the set may be perfectly good and the service merely down, and
    failing a delivery for that would be Maestro breaking its own gate.

    The second return value is whether the importer actually answered, because
    an empty finding list is produced by both agreement and unreachability, and
    only the caller can say which of those it is reporting.
    """
    archives = [set_dir / (p.get("archive") or {}).get("filename", "")
                for p in manifest.get("problems") or []]
    archives = [a for a in archives if a.is_file()]
    if not archives:
        return [], False
    try:
        parsed = parser(archives)
    except Exception as e:  # noqa: BLE001 — any transport failure, not just ours
        return [Finding("P-0", Severity.WARN,
                        f"could not pre-flight against the importer ({e}); the manifest "
                        f"was validated locally only")], False
    return compare(manifest, parsed), True


def candidates(watch_dir: str | Path) -> Iterator[Path]:
    """Immediate subdirectories of the watched folder, in a stable order."""
    watch_dir = Path(watch_dir)
    if not watch_dir.is_dir():
        return
    yield from sorted(p for p in watch_dir.iterdir() if p.is_dir())


def ingest(set_dir: str | Path, store: Store, *, extra_tags: set[str] | None = None,
           parser: Parser | None = None) -> Inspection:
    """Validate and, if clean, register a run.

    Nothing is moved or modified. The set folder stays exactly as delivered — it is
    the input to the Polygon lane, and a corrected re-delivery arrives as a new
    folder with an `-rN` suffix rather than an edit to this one.
    """
    result = inspect(set_dir, store, extra_tags=extra_tags, parser=parser)
    if result.verdict is not Verdict.READY:
        return result

    m = json.loads((Path(set_dir) / "MANIFEST.json").read_text(encoding="utf-8"))
    seeds = [
        ProblemSeed(
            slug=p["slug"],
            idx=p.get("idx", i + 1),
            title=p.get("title", ""),
            archive=(p.get("archive") or {}).get("filename", ""),
        )
        for i, p in enumerate(m.get("problems", []))
    ]
    run_id = store.create_run(m["set"]["name"], set_dir, seeds)
    # A registered run *is* a completed ingest — the stage's whole job was to
    # decide whether this folder is worth working, and it said yes. Leaving it at
    # INGEST would park it where no lane looks, so nothing would ever pick it up.
    store.set_run(run_id, stage=RunStage.POLYGON, status=RunStatus.RUNNING)
    store.log(run_id, "info", f"ingested {len(seeds)} problem(s) from {Path(set_dir).name}")
    for f in result.findings:  # warnings survive ingest and belong in the record
        store.log(run_id, "warn", str(f), slug=f.slug)
    return Inspection(Path(set_dir), Verdict.READY, result.findings, m["set"]["name"], run_id,
                      checked=result.checked)


def scan(watch_dir: str | Path, store: Store, *, extra_tags: set[str] | None = None,
         parser: Parser | None = None) -> list[Inspection]:
    """One sweep of the watched folder. Safe to call on a timer."""
    return [ingest(d, store, extra_tags=extra_tags, parser=parser)
            for d in candidates(watch_dir)]
