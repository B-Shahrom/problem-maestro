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
from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .characteristics import precheck
from .checks import Finding, Severity, errors
from .manifest import validate
from .model import ProblemSeed
from .store import Store

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


def inspect(set_dir: str | Path, store: Store | None = None, *, extra_tags: set[str] | None = None) -> Inspection:
    """Validate one candidate set folder without touching it."""
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
    # The characteristics pre-check needs a trustworthy manifest to compare against,
    # so it only runs once the manifest itself is clean.
    if verdict is Verdict.READY:
        findings += precheck(set_dir / "characteristics.md", json.loads(mf.read_text(encoding="utf-8")))
        verdict = _classify(findings)
    return Inspection(set_dir, verdict, findings, name)


def candidates(watch_dir: str | Path) -> Iterator[Path]:
    """Immediate subdirectories of the watched folder, in a stable order."""
    watch_dir = Path(watch_dir)
    if not watch_dir.is_dir():
        return
    yield from sorted(p for p in watch_dir.iterdir() if p.is_dir())


def ingest(set_dir: str | Path, store: Store, *, extra_tags: set[str] | None = None) -> Inspection:
    """Validate and, if clean, register a run.

    Nothing is moved or modified. The set folder stays exactly as delivered — it is
    the input to the Polygon lane, and a corrected re-delivery arrives as a new
    folder with an `-rN` suffix rather than an edit to this one.
    """
    result = inspect(set_dir, store, extra_tags=extra_tags)
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
    store.log(run_id, "info", f"ingested {len(seeds)} problem(s) from {Path(set_dir).name}")
    for f in result.findings:  # warnings survive ingest and belong in the record
        store.log(run_id, "warn", str(f), slug=f.slug)
    return Inspection(Path(set_dir), Verdict.READY, result.findings, m["set"]["name"], run_id)


def scan(watch_dir: str | Path, store: Store, *, extra_tags: set[str] | None = None) -> list[Inspection]:
    """One sweep of the watched folder. Safe to call on a timer."""
    return [ingest(d, store, extra_tags=extra_tags) for d in candidates(watch_dir)]
