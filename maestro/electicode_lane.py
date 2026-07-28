"""Stages 6-8 — upload the batch, prove it landed, chore it, audit it.

Where the Polygon lane fans out per problem, this half is **batch-shaped and
strictly serial**. The uploader takes one parent folder, the chore runner takes
one characteristics file, and all of them drive the same browser profile: two
concurrent Playwright sessions on one `session_state.json` is a race over the
platform's own UI state. `step()` therefore runs exactly one stage per call and
returns, so progress is durable between stages rather than only at the end.

Three decisions are worth stating up front, because none of them is obvious from
the tool surfaces alone.

**Preview always precedes apply.** The upload modal is the only place that knows
which problems already exist on the platform, and `exists` decides whether stage
7 replaces a problem's categories or appends to them. It cannot be derived from
the manifest, from Polygon, or from a catalog scrape taken before the upload.

**Stage 6.5 exists because nothing else proves the join.** Upload reports success
when the modal closes. It does not report *what the platform named the things it
created*. Every later stage addresses problems by slug, so a slug that drifted —
or a folder the platform quietly skipped — would let chores and audit run
cleanly against the wrong rows. Reconcile reads the catalog back and refuses to
continue on a mismatch.

**Chores run once per exists-group.** `batch.py --tags-mode` is a whole-run
setting, so a batch mixing fresh and pre-existing problems cannot express both in
one invocation. The lane splits the characteristics file instead.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import characteristics as char
from . import divisions as div
from . import manifest
from .checks import Severity, errors
from .model import (BlockReason, Problem, ProblemStage, ProblemStatus, RunStage,
                    RunStatus)
from .preflight import divisions_landed, limits_landed
from .scraper import Detected, Outcome, Progress, Result, ScraperClient
from .store import Store

#: Tries at a stage whose failure the Scraper called operational. Low on purpose:
#: these stages drive a browser, and a fault that survives three attempts is a
#: platform or session problem that another attempt will not fix.
RETRY_CAP = 3

#: Lines from one tool invocation that reach the event log before it stops
#: relaying them. Playwright can be very loud on stderr, and an event table that
#: is 95% browser warnings is one nobody reads — but truncating silently would
#: reproduce the failure this whole change is about, so the cut is announced.
LOG_CAP = 300


class Reporter:
    """Relays a running tool's output into the run's event log, as it happens.

    Before this, a stage that took twenty minutes wrote nothing until it
    finished. "Something is stuck uploading" was therefore not a hard question —
    it was an unanswerable one, because the only observation available was that
    no result had appeared yet, which is identical to working normally.

    Three kinds of line arrive and they are not equally interesting:

    * the tool's own `--json` events, which are the actual progress and are
      reformatted to one readable line each;
    * `heartbeat`, which is Maestro noticing silence. This is the one that
      answers the question, so it is logged at `warn` — it is the difference
      between "slow" and "not responding";
    * everything else, which is browser noise, kept at `debug` and capped.
    """

    def __init__(self, store: Store, run_id: int, label: str) -> None:
        self.store = store
        self.run_id = run_id
        self.label = label
        self._n = 0
        self._lock = threading.Lock()

    def __call__(self, p: Progress) -> None:
        with self._lock:
            self._n += 1
            n = self._n
        if n > LOG_CAP:
            if n == LOG_CAP + 1:
                self.store.log(self.run_id, "warn",
                               f"{self.label}: further output suppressed after "
                               f"{LOG_CAP} lines — the full stream is still captured "
                               f"and reported when the stage ends")
            return

        if p.stream == "heartbeat":
            self.store.log(self.run_id, "warn", f"{self.label}: {p.line}")
            return
        if (pretty := _event_line(p.line)) is not None:
            self.store.log(self.run_id, "info", f"{self.label}: {pretty}")
            return
        self.store.log(self.run_id, "debug" if p.stream == "stdout" else "warn",
                       f"{self.label}: {p.line[:300]}")


def _event_line(line: str) -> str | None:
    """One `--json` event as a sentence, or `None` if this is not one.

    The tools emit NDJSON on stdout: `{"event": "start", "total": N}` then one
    `item` per unit of work. Rendering them rather than dumping the raw JSON is
    what makes the log answer "where is it" at a glance.
    """
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        e = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(e, dict) or "event" not in e:
        return None

    kind = e.get("event")
    if kind == "start":
        total = e.get("total")
        warns = e.get("warnings") or []
        head = f"starting — {total} step(s)" if total is not None else "starting"
        return head + (f"; {len(warns)} warning(s): {warns[0]}" if warns else "")
    if kind == "item":
        what = (e.get("id") or "?").strip()
        if "ok" in e:
            mark = "ok" if e.get("ok") else "FAILED"
            detail = f" — {e['error']}" if not e.get("ok") and e.get("error") else ""
            return f"{what}: {mark}{detail}"
        # The upload modal's detection rows carry `exists` instead of `ok`.
        if "exists" in e:
            return f"detected {what} ({'exists' if e.get('exists') else 'new'})"
        return f"{what}"
    if kind in ("end", "done", "summary"):
        return f"{kind}: " + ", ".join(f"{k}={v}" for k, v in e.items() if k != "event")
    return f"{kind}: " + ", ".join(f"{k}={v}" for k, v in e.items() if k != "event")


@dataclass(slots=True)
class LaneReport:
    stage: RunStage | None = None
    """The stage this pass attempted."""
    ran: list[str] = field(default_factory=list)
    advanced: list[str] = field(default_factory=list)
    blocked: BlockReason | None = None
    failed: str | None = None
    run_advanced_to: RunStage | None = None

    @property
    def idle(self) -> bool:
        return not (self.advanced or self.run_advanced_to)


@dataclass(slots=True)
class ChoreGroup:
    """One `batch.py run` invocation: a slug set and the tag mode it needs."""

    name: str
    tags_mode: str
    slugs: list[str]


#: `batch run` reports each stage's `item` event under the plan's display *name*,
#: while `--only`/`--skip` address stages by *key*. Mapping one to the other is
#: what lets a failed chain resume, so it is explicit rather than derived: an
#: unrecognised name yields no key, and a stage Maestro cannot name it also will
#: not skip. That is the safe direction — the cost is re-running a chain from the
#: top, and the alternative is skipping a stage that never ran.
_STAGE_KEYS = {
    "fixmdx": "fixmdx",           # the display name carries its scope: "fixmdx (all)"
    "translate": "translate",
    "metadata": "metadata",
    "custom": "custom",
    "division": "division",
    "list add": "list-add",
    "list reorder": "list-reorder",
}

#: Which stages survive being run twice (CLI contract §7). `metadata` is the one
#: that depends on how it was invoked: `--tags-mode reset` re-fills a fixed value
#: and converges, while `add` appends through a helper that never de-dupes.
_REPLAYABLE = {
    "fixmdx": True,        # compares against the current text and skips the save
    "translate": True,     # re-translates and re-saves; nothing accumulates
    "metadata": None,      # → depends on tags_mode
    "custom": False,       # --category-prepend appends without de-duping
    "division": True,      # declarative: set to exactly this set, verify, done
    "list-add": False,     # de-dupes slugs against row titles, so almost never
    "list-reorder": True,  # a completed reorder plans no moves
}


def stage_key(name: str) -> str | None:
    for prefix, key in _STAGE_KEYS.items():
        if name == prefix or name.startswith(prefix + " ("):
            return key
    return None


def stage_progress(evts: list[dict] | None) -> tuple[list[str], str | None]:
    """`(stage keys that completed, the key of the one that failed)`.

    With `--stop-on-error` the run halts at the first failure, so the stream is a
    prefix of the plan and the last event is the failure. A stage whose name does
    not map is reported as neither — it is not skipped on the retry, and it is not
    treated as a known-safe failure either.
    """
    done: list[str] = []
    failed: str | None = None
    for e in evts or []:
        if e.get("event") != "item":
            continue
        key = stage_key((e.get("id") or "").strip())
        if e.get("ok"):
            if key:
                done.append(key)
        elif failed is None:
            failed = key
    return done, failed


def stage_retryable(failed: str | None, tags_mode: str) -> bool:
    """Whether re-running the stage that failed is safe.

    Unknown (`None`) is not retryable: it means the failure could not be
    attributed to a stage, so nothing is known about replaying it.
    """
    if failed is None:
        return False
    replayable = _REPLAYABLE.get(failed)
    if replayable is None:
        return failed == "metadata" and tags_mode != "add"
    return replayable


def _did_nothing(evts: list[dict] | None) -> str | None:
    """Read a `batch run --json` stream for a success that applied nothing.

    Two shapes of silent no-op, both exiting 0:

    * **an empty plan** — every step was skipped, so the batch reports success
      having touched nothing (`batch.py:411-414`);
    * **dropped tags** — `build_plan` discards *all* tags when the tag-line count
      disagrees with the row count, warns, and carries on (`batch.py:195-197`).

    Maestro's precheck catches the second before a run starts, and `render()`
    guarantees alignment in the files it writes. This reads the tool's own report
    anyway: the two guarantees are Maestro's model of `batch.py`, and this is
    `batch.py` saying what it actually did.
    """
    start = next((e for e in (evts or []) if e.get("event") == "start"), None)
    if start is None:
        return None  # no --json support, or the stream was lost — not a finding
    if not start.get("total"):
        return ("produced no steps at all — nothing was applied, and the tool "
                "still exited 0")
    if dropped := [w for w in (start.get("warnings") or []) if "tags skipped" in w]:
        return f"ran with its tags silently dropped: {dropped[0]}"
    return None


def chore_groups(problems: list[Problem]) -> list[ChoreGroup]:
    """Split the batch by whether each problem existed before the upload.

    `--category` overwrites and `--category-add` appends, so the two populations
    need opposite treatment: a fresh problem's field is empty and should be set
    outright, while a pre-existing one may carry curricular labels (`academy
    exam`) that the authored tags must not erase.

    `existed_before_upload is None` groups with the pre-existing ones. Appending
    to a fresh problem costs nothing; overwriting an established one destroys
    data, so the ambiguous case takes the side whose worst outcome is recoverable.
    """
    fresh = [p.slug for p in problems if p.existed_before_upload is False]
    known = [p.slug for p in problems if p.existed_before_upload is not False]
    out = []
    if fresh:
        out.append(ChoreGroup("fresh", "reset", fresh))
    if known:
        out.append(ChoreGroup("existing", "add", known))
    return out


class ElectiCodeLane:
    def __init__(
        self,
        store: Store,
        client: ScraperClient,
        work_dir: str | Path,
        *,
        apply: bool = False,
        divisions: str = "",
        targets: str = "",
        list_url: str = "",
        fixmdx: str = "subtasks",
    ) -> None:
        self.store = store
        self.client = client
        self.work_dir = Path(work_dir)
        self.apply = apply
        self.divisions = divisions
        self.targets = targets
        self.list_url = list_url
        self.fixmdx = fixmdx

    # ------------------------------------------------------------ scaffolding

    def run_dir(self, run_id: int) -> Path:
        return self.work_dir / str(run_id)

    def upload_dir(self, run_id: int) -> Path:
        """Must match `PolygonLane.upload_dir` — this is the handoff between halves."""
        return self.run_dir(run_id) / "upload"

    def divisions_for(self, run) -> str:
        """The divisions this batch asks for.

        The run's own choice wins, and `""` is a choice: an operator who unticked
        everything asked for none, not for whatever the install happens to be
        configured with. Only `None` — never chosen — inherits the default.
        """
        return self.divisions if run.divisions is None else run.divisions

    def _needs_paged_scrape(self, run) -> bool:
        """Whether stage 8 has to page the table instead of reading the catalog.

        The catalog is one page load against ~40, so the paged scrape is worth
        avoiding — but it is not a strictly better source. It carries a subset of
        the columns, and `division_access` is not among them.

        Difficulty and category, which are what the chores set, *are* in the
        catalog. So the answer turns on one thing: whether this run granted
        division access and therefore has a division claim to verify. If it did
        not, there is nothing the paged scrape would add and forty page loads buy
        nothing.

        Getting this wrong in the cheap direction is the dangerous one — a
        catalog-sourced scrape has no `division_access` on any row, which is
        indistinguishable from every problem having been granted none. `report
        audit --char` skips its division check in exactly that state, so a run
        that silently used the catalog would pass an audit that never looked.
        """
        return bool(self.divisions_for(run))

    def _say(self, run_id: int, label: str) -> Reporter:
        """A live relay from one tool invocation into this run's event log.

        One per call rather than one per lane: the cap is per invocation, so a
        loud upload cannot use up the budget a later chore chain needs.
        """
        return Reporter(self.store, run_id, label)

    def artefact(self, run_id: int, name: str) -> Path:
        """Everything a stage produced, kept per run so a failure is inspectable."""
        d = self.run_dir(run_id) / "electicode"
        d.mkdir(parents=True, exist_ok=True)
        return d / name

    def _stages_done(self, run_id: int, group: ChoreGroup,
                     add: list[str] | None = None) -> list[str]:
        """Read, or extend, the stages this group has already completed.

        Kept on disk rather than in the job store because it is scoped to one
        group's chore chain and is as much an operator artefact as orchestrator
        state — when a run stops mid-chain, this file is the answer to "how far
        did it get".
        """
        path = self.artefact(run_id, f"chores-{group.name}-done.json")
        try:
            done = [k for k in json.loads(path.read_text(encoding="utf-8")) if isinstance(k, str)]
        except (OSError, ValueError, TypeError):
            done = []
        if add is not None:
            done = list(dict.fromkeys(done + add))
            path.write_text(json.dumps(done), encoding="utf-8")
        return done

    @staticmethod
    def _char_path(set_dir: str | Path) -> Path:
        """The authored characteristics file, whose name the manifest may override.

        The default is used when the manifest is unreadable rather than raising:
        by the time this runs the set has passed ingest, so a manifest that has
        since gone missing is a different failure than the one this stage reports,
        and the missing-file check below gives the clearer message.
        """
        set_dir = Path(set_dir)
        try:
            name = (manifest.load(set_dir).get("characteristics") or {}).get("filename")
        except (OSError, ValueError, AttributeError):
            name = None
        return set_dir / (name or "characteristics.md")

    # -------------------------------------------------------------- dispatch

    _STAGES = (RunStage.UPLOAD, RunStage.RECONCILE, RunStage.CHORES, RunStage.AUDIT)

    def step(self, run_id: int) -> LaneReport:
        report = LaneReport()
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"no run {run_id}")
        if run.stage not in self._STAGES:
            return report
        if run.status in (RunStatus.FAILED, RunStatus.BLOCKED):
            # A stopped run stays stopped until something clears it. Without this
            # the retry cap does not hold: `_setback` gives up by marking the run
            # FAILED, and a scheduler that only looked at the stage would call
            # straight back in and start the attempts over — on `_upload`, that
            # means submitting the batch again past the budget meant to stop it.
            return report
        report.stage = run.stage

        if not self._preflight(run, report):
            return report

        handler = {
            RunStage.UPLOAD: self._upload,
            RunStage.RECONCILE: self._reconcile,
            RunStage.CHORES: self._chores,
            RunStage.AUDIT: self._audit,
        }[run.stage]
        handler(run_id, report)
        return report

    def _preflight(self, run, report: LaneReport) -> bool:
        """Auth check, then the apply gate. Both park the run rather than failing it.

        The session check runs for all four stages. Only `report.py` is browserless,
        and the stage that calls it opens with a catalog scrape that is not.

        The gate opens for a scheduler configured with `apply` **or** for a run an
        operator approved individually. Approval is per-run because a global flag
        cannot be granted to one batch: without it, releasing a blocked run would
        mean releasing every other run at the same time, which is not what anyone
        looking at one blocked batch is asking for.
        """
        if not self._session_ok(run.id, report):
            return False
        if not (self.apply or run.approved) and run.stage in (RunStage.UPLOAD, RunStage.CHORES):
            # The two mutating stages. Reconcile and audit only read, so a
            # preview-mode run still gets to prove what a real one would find.
            self.store.block(run.id, BlockReason.AWAITING_APPROVAL,
                             f"stage {run.stage} would write to ElectiCode — "
                             f"approve this run to proceed")
            report.blocked = BlockReason.AWAITING_APPROVAL
            return False
        return True

    def _session_ok(self, run_id: int, report: LaneReport) -> bool:
        r = self.client.session(progress=self._say(run_id, "session"))
        report.ran.append("session")
        if r.ok:
            return True
        if r.outcome is Outcome.BLOCKED:
            self.store.block(run_id, BlockReason.SESSION_EXPIRED,
                             f"ElectiCode session unusable: {r.reason}")
            report.blocked = BlockReason.SESSION_EXPIRED
            return False
        return self._setback(run_id, r, report, "session check")

    # ------------------------------------------------------------ stage 6

    def _upload(self, run_id: int, report: LaneReport) -> None:
        problems = self.store.active(run_id)
        folder = self.upload_dir(run_id)
        if not folder.is_dir():
            self._fail(run_id, report, f"nothing to upload: {folder} does not exist")
            return

        preview = self.client.upload(folder, self.artefact(run_id, "upload-preview.json"),
                                     progress=self._say(run_id, "upload preview"))
        report.ran.append("upload:preview")
        if not preview.ok:
            self._setback(run_id, preview, report, "upload preview")
            return

        rows: list[Detected] = preview.data or []
        if problem := self._identity_error(run_id, problems, rows):
            # Refusing here is the whole point of reading the preview: past this
            # line the platform is being told to overwrite a named problem, and
            # `--apply` is not reversible by re-running anything.
            self._fail(run_id, report, problem)
            return

        # Write-once. A retry after an apply that failed *late* — the platform
        # created the problems but the modal never closed — sees its own work as
        # EXISTS, and recording that would answer the wrong question. The field
        # asks whether a problem predated this run, and only the first preview
        # knows.
        by_id = {r.id: r for r in rows}
        for p in problems:
            if p.existed_before_upload is None:
                self.store.set_problem(run_id, p.slug,
                                       existed_before_upload=by_id[p.slug].exists)

        applied = self.client.upload(folder, self.artefact(run_id, "upload.json"),
                                     apply=True, only=[p.slug for p in problems],
                                     progress=self._say(run_id, "upload"))
        report.ran.append("upload:apply")
        if not applied.ok:
            self._setback(run_id, applied, report, "upload")
            return

        for p in problems:
            self.store.set_problem(run_id, p.slug, stage=ProblemStage.UPLOADED,
                                   status=ProblemStatus.OK)
            report.advanced.append(p.slug)
        n_exists = sum(1 for r in rows if r.exists)
        self.store.log(run_id, "info",
                       f"uploaded {len(problems)} problem(s); {n_exists} overwrote an "
                       f"existing entry")
        self._advance(run_id, RunStage.RECONCILE, report)

    def _identity_error(self, run_id: int, problems: list[Problem],
                        rows: list[Detected]) -> str | None:
        """Refuse the upload unless every row is the problem Maestro meant.

        The check that only exists here is the last one: an EXISTS row whose
        `overwrite_name` is not the title Maestro expects for that slug is a
        collision with unrelated content, and the platform will replace it without
        complaint. `--only` cannot catch that — it matches on the Problem ID,
        which is precisely what agrees while the content does not.
        """
        if not rows:
            return ("the upload modal reported no detected problems — refusing to "
                    "submit a batch whose contents could not be read back")
        expected = {p.slug: p for p in problems}
        seen = {r.id for r in rows}
        if missing := sorted(set(expected) - seen):
            return (f"the platform did not detect {len(missing)} problem folder(s): "
                    f"{', '.join(missing)}")
        if extra := sorted(seen - set(expected)):
            # Not fatal, because the apply pass passes `--only` and these rows are
            # unticked. Still worth saying: the upload folder holds something this
            # run did not put there, which means state and disk disagree.
            self.store.log(run_id, "warn",
                           f"the upload folder holds {len(extra)} problem(s) this run does "
                           f"not own; they will not be ticked: {', '.join(extra)}")
        for r in rows:
            if r.id not in expected:
                continue
            want = expected[r.id].title
            if r.exists and r.overwrite_name and want and r.overwrite_name != want:
                return (f"{r.id}: would overwrite {r.overwrite_name!r}, but this run's "
                        f"problem is titled {want!r} — refusing a possible collision")
        return None

    # ---------------------------------------------------------- stage 6.5

    def _reconcile(self, run_id: int, report: LaneReport) -> None:
        # Reconcile only asks "does this slug exist on the platform" — a question
        # the one-page catalog answers as well as forty pages of table do.
        r = self.client.scrape(self.artefact(run_id, "catalog.json"), from_catalog=True,
                               progress=self._say(run_id, "catalog scrape (1 page load)"))
        report.ran.append("scrape")
        if not r.ok:
            self._setback(run_id, r, report, "catalog scrape")
            return

        rows = {row.get("s3_id"): row for row in (r.data or []) if row.get("s3_id")}
        problems = self.store.active(run_id)
        missing = [p.slug for p in problems if p.slug not in rows]
        if missing:
            # A slug that is not in the catalog after a successful upload means the
            # identity map broke somewhere Maestro cannot see. Choring it would
            # target nothing and audit would report it as an unrelated gap, so the
            # run stops here with the cause still legible.
            self._fail(run_id, report,
                       f"{len(missing)} uploaded problem(s) are absent from the "
                       f"ElectiCode catalog: {', '.join(missing)}")
            return

        renamed = [f"{p.slug} ({rows[p.slug].get('name')!r} on the platform, "
                   f"{p.title!r} in the manifest)"
                   for p in problems
                   if p.title and (rows[p.slug].get("name") or "").strip() != p.title]
        if renamed:
            # Not fatal: the platform is authoritative for display names and an
            # operator may have renamed one deliberately. It does invalidate the
            # audit's title column, so it goes in the log rather than passing
            # unremarked.
            self.store.log(run_id, "warn",
                           f"{len(renamed)} title(s) differ from the manifest: "
                           + "; ".join(renamed))

        for p in problems:
            self.store.set_problem(run_id, p.slug, stage=ProblemStage.RECONCILED,
                                   status=ProblemStatus.OK, electicode_slug=p.slug)
            report.advanced.append(p.slug)
        self.store.log(run_id, "info", f"{len(problems)} slug(s) confirmed on ElectiCode")
        self._advance(run_id, RunStage.CHORES, report)

    # ------------------------------------------------------------ stage 7

    def _chores(self, run_id: int, report: LaneReport) -> None:
        run = self.store.get_run(run_id)
        assert run is not None
        source = self._char_path(run.set_dir)
        if not source.is_file():
            self._fail(run_id, report, f"no {source.name} in {run.set_dir}")
            return
        parsed = char.parse(source.read_text(encoding="utf-8"))

        # Each group is marked done the moment it succeeds, so a retry resumes at
        # the group that failed. Re-running a finished one is not merely wasteful:
        # `batch.py` ends in `list_editor add`, and adding a problem to a contest
        # it is already in is not an operation that cancels out.
        problems = self.store.active(run_id)
        pending = [p for p in problems if p.stage is not ProblemStage.CHORED]
        for group in chore_groups(pending):
            path = self.artefact(run_id, f"characteristics-{group.name}.md")
            try:
                path.write_text(char.render(char.subset(parsed, group.slugs)),
                                encoding="utf-8")
            except ValueError as e:
                self._fail(run_id, report, str(e))
                return

            done = self._stages_done(run_id, group)
            r = self.client.chores(
                path, tags_mode=group.tags_mode, apply=True,
                divisions=self.divisions_for(run), targets=self.targets,
                list_url=self.list_url, fixmdx=self.fixmdx,
                skip=",".join(done),
                progress=self._say(run_id, f"chores/{group.name}"),
            )
            report.ran.append(f"chores:{group.name}")
            if not r.ok:
                # A chore chain resumes at the stage that failed rather than at the
                # top, because several of the stages cannot be replayed — a retry
                # from the top would re-append tags and re-add list rows that
                # already landed. Whether the *failed* stage itself may be retried
                # is a separate question, and the answer is often no.
                ran, failed = stage_progress(r.data)
                self._stages_done(run_id, group, done + ran)
                what = (f"chores for the {group.name} group "
                        f"({len(group.slugs)} problem(s))")
                self._setback(run_id, r, report,
                              f"{what} at stage {failed or 'unknown'}",
                              retry_safe=stage_retryable(failed, group.tags_mode))
                return
            if noop := _did_nothing(r.data):
                # `batch.py` exits 0 when its plan is empty and when it drops all
                # tags for a count mismatch. Both are the failure this pipeline is
                # built to catch: work that reports success and did not happen.
                self._fail(run_id, report, f"the {group.name} group {noop}")
                return

            for slug in group.slugs:
                self.store.set_problem(run_id, slug, stage=ProblemStage.CHORED,
                                       status=ProblemStatus.OK)
                report.advanced.append(slug)
            self.store.log(run_id, "info",
                           f"chored {len(group.slugs)} {group.name} problem(s) with "
                           f"--tags-mode {group.tags_mode}")

        self._advance(run_id, RunStage.AUDIT, report)

    # ------------------------------------------------------------ stage 8

    def _audit(self, run_id: int, report: LaneReport) -> None:
        run = self.store.get_run(run_id)
        assert run is not None
        wanted = self.divisions_for(run)
        paged = self._needs_paged_scrape(run)
        scrape = self.client.scrape(self.artefact(run_id, "catalog-after.json"),
                                    from_catalog=not paged,
                                    progress=self._say(run_id, "catalog scrape"
                                                       + ("" if paged else " (1 page load)")))
        report.ran.append("scrape")
        if not scrape.ok:
            self._setback(run_id, scrape, report, "post-chore scrape")
            return

        problems = self.store.active(run_id)
        parsed = char.parse(self._char_path(run.set_dir).read_text(encoding="utf-8"))
        # Audit only what this run actually put there. The full authored file may
        # name quarantined problems, and every one of them would be reported as
        # `not_scraped` — a real gap, but not one this run introduced or can fix.
        path = self.artefact(run_id, "characteristics-audited.md")
        path.write_text(char.render(char.subset(parsed, [p.slug for p in problems])),
                        encoding="utf-8")

        # Maestro's own checks, both for the same reason: `report audit --char`
        # does not compare limits at all, and it silently skips the division check
        # in exactly the state that most needs it (see `divisions_landed`).
        if self._limits_wrong(run_id, run.set_dir, scrape.data or [], report):
            return
        if self._divisions_wrong(run_id, wanted, scrape.data or [], problems, report):
            return

        r = self.client.audit(self.artefact(run_id, "catalog-after.json"), path,
                              self.artefact(run_id, "audit.json"),
                              divisions=wanted,
                              progress=self._say(run_id, "audit"))
        report.ran.append("audit")
        if r.outcome is Outcome.HALT and r.rc == 1:
            # `report.py audit` exits 1 on a gap or a mismatch. That is a finding
            # about the *batch*, not a bad invocation: the run is complete and
            # wrong, which a human has to see rather than a retry loop swallow.
            self.store.block(run_id, BlockReason.AWAITING_APPROVAL,
                             f"audit found gaps: {self._audit_summary(r)}")
            report.blocked = BlockReason.AWAITING_APPROVAL
            return
        if not r.ok:
            self._setback(run_id, r, report, "audit")
            return
        if self._audit_incomplete(run_id, wanted, r, report):
            return

        for p in problems:
            self.store.set_problem(run_id, p.slug, stage=ProblemStage.AUDITED,
                                   status=ProblemStatus.OK)
            report.advanced.append(p.slug)
        self.store.set_run(run_id, stage=RunStage.DONE, status=RunStatus.DONE)
        report.run_advanced_to = RunStage.DONE
        self.store.log(run_id, "info",
                       f"audit clean — {len(problems)} problem(s) match the characteristics")

    def _limits_wrong(self, run_id: int, set_dir: str, rows: list[dict],
                      report: LaneReport) -> bool:
        """Log the limits verdict; fail the run if any limit did not land.

        A wrong limit is not a gap this run can close afterwards. The values arrive
        with the imported package and no tool Maestro drives can set them, so the
        only fix available today is a corrected re-import. That makes it a failure
        rather than something to note and continue past.

        The platform itself *does* have a way to set both; it is simply not exposed
        by any Scraper command yet. When it is, this stops being terminal and
        becomes a repair — see the limits section of `FROM_MAESTRO.md` in the
        Scraper repo for the shape that was asked for.
        """
        try:
            m = manifest.load(Path(set_dir))
        except (OSError, ValueError):
            return False
        findings = limits_landed(m, rows)
        for f in findings:
            self.store.log(run_id, "error" if f.severity is Severity.ERROR else "warn",
                           f"{f.check}: {f.message}", slug=f.slug)
        if bad := errors(findings):
            self._fail(run_id, report,
                       f"{len(bad)} problem(s) carry the wrong limits on the platform; "
                       "nothing Maestro drives can set them, so only a corrected "
                       "re-import fixes it today")
            return True
        return False

    def _divisions_wrong(self, run_id: int, wanted: str, rows: list[dict],
                         problems: list[Problem], report: LaneReport) -> bool:
        """Log the division verdict; fail the run if any grant did not land.

        Unlike a wrong limit this *is* fixable in place — `division set` is
        idempotent and converges — but it still fails the run rather than warning,
        because a problem without division access is invisible to the students it
        was authored for, and a run that ends `done` is a run nobody looks at again.
        """
        findings = divisions_landed(wanted, rows, [p.slug for p in problems])
        for f in findings:
            self.store.log(run_id, "error" if f.severity is Severity.ERROR else "warn",
                           f"{f.check}: {f.message}", slug=f.slug)
        if bad := errors(findings):
            self._fail(run_id, report,
                       f"{len(bad)} problem(s) are missing the requested division access — "
                       "they would not be visible to the divisions this run targeted")
            return True
        return False

    def _audit_incomplete(self, run_id: int, wanted: str, r: Result,
                          report: LaneReport) -> bool:
        """Refuse to call a run done when the audit did not run every check.

        `report audit --char` exits **0** when it skips a check — correctly, since
        nothing it looked at was wrong. It reports the skip in `skipped` and lists
        what it did in `checks_run`, deliberately machine-visible rather than
        stderr-only, precisely so a caller cannot mistake one for the other.

        Reading it closes the last link in a chain that otherwise ends in silence:
        divisions requested → catalog-sourced scrape → no `division_access` on any
        row → division check skipped → audit exits 0 → run marked DONE, with the
        grant never verified. `_needs_paged_scrape` is meant to prevent that, but
        that is one boolean standing between an operator and a false pass, and the
        tool is willing to state the fact outright.
        """
        data = r.data if isinstance(r.data, dict) else {}
        want = {"difficulty", "tags"} | ({"division"} if wanted else set())

        if "checks_run" not in data:
            # An older tool that does not say. Not a block — it may well have run
            # everything — but it must not read as confirmation either.
            self.store.log(run_id, "warn",
                           "the audit did not report which checks it ran, so this pass "
                           "confirms only that nothing it looked at was wrong")
            return False

        missing = sorted(want - set(data.get("checks_run") or []))
        if not missing:
            return False
        skipped = ", ".join(data.get("skipped") or []) or "not reported"
        self.store.block(run_id, BlockReason.AWAITING_APPROVAL,
                         f"the audit passed but did not run every check — {', '.join(missing)} "
                         f"never ran (skipped: {skipped}). Exit 0 here means nothing it "
                         f"looked at was wrong, not that the batch is correct")
        report.blocked = BlockReason.AWAITING_APPROVAL
        return True

    @staticmethod
    def _audit_summary(r: Result) -> str:
        counts = (r.data or {}).get("counts") if isinstance(r.data, dict) else None
        if not counts:
            return r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "see audit.json"
        return ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in counts.items() if v)

    # ------------------------------------------------------------- outcomes

    def _advance(self, run_id: int, stage: RunStage, report: LaneReport) -> None:
        self.store.set_run(run_id, stage=stage, status=RunStatus.RUNNING)
        report.run_advanced_to = stage

    def _fail(self, run_id: int, report: LaneReport, message: str) -> None:
        self.store.set_run(run_id, status=RunStatus.FAILED, error=message)
        self.store.log(run_id, "error", message)
        report.failed = message

    def _setback(self, run_id: int, r: Result, report: LaneReport, what: str,
                 *, retry_safe: bool = True) -> bool:
        """Retry a transient fault, fail a permanent one, count either way.

        The counter lives on the run's problems rather than the run, because the
        store's retry budget is per-problem and these stages act on all of them at
        once. Any active problem's count answers "how many times has this stage
        been tried", since they advance together.

        `retry_safe=False` says the *operation* cannot be repeated even though the
        *fault* was transient — the Scraper's exit code describes the failure, not
        whether replaying the step is sound, and only the caller knows that.
        """
        detail = (r.stderr.strip().splitlines() or [""])[-1]
        if r.outcome is not Outcome.RETRY:
            self._fail(run_id, report, f"{what} failed: {r.reason}. {detail}".strip())
            return False
        if not retry_safe:
            self._fail(run_id, report,
                       f"{what} failed: {r.reason}. Not retried — the step is not "
                       f"idempotent, so a repeat would double-apply what already "
                       f"landed. {detail}".strip())
            return False

        active = self.store.active(run_id)
        tries = max((self.store.bump_attempt(run_id, p.slug) for p in active), default=RETRY_CAP)
        if tries >= RETRY_CAP:
            self._fail(run_id, report,
                       f"{what} failed {tries} time(s): {r.reason}. {detail}".strip())
            return False
        self.store.log(run_id, "warn",
                       f"{what}: {r.reason} — retrying (attempt {tries + 1}). {detail}".strip())
        return False
