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

from dataclasses import dataclass, field
from pathlib import Path

from . import characteristics as char
from . import manifest
from .model import (BlockReason, Problem, ProblemStage, ProblemStatus, RunStage,
                    RunStatus)
from .scraper import Detected, Outcome, Result, ScraperClient
from .store import Store

#: Tries at a stage whose failure the Scraper called operational. Low on purpose:
#: these stages drive a browser, and a fault that survives three attempts is a
#: platform or session problem that another attempt will not fix.
RETRY_CAP = 3


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

    @property
    def retry_safe(self) -> bool:
        """Whether re-running this group after a partial failure is harmless.

        `--tags-mode reset` fills the category field with a fixed value, so a
        repeat converges. `--tags-mode add` appends through `_append_value`, which
        joins with `", "` and never de-dupes — a problem that was saved before the
        step failed comes back as `"arrays, arrays"`. There is no way to retry
        only the unsaved ones either: `batch run --json` reports one event per
        *stage*, not per problem, and it does not forward its sub-tools' streams.

        So this group fails to a human instead of retrying. Silent tag
        duplication on the problems that already worked is worse than a stop.
        """
        return self.tags_mode != "add"


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

    def artefact(self, run_id: int, name: str) -> Path:
        """Everything a stage produced, kept per run so a failure is inspectable."""
        d = self.run_dir(run_id) / "electicode"
        d.mkdir(parents=True, exist_ok=True)
        return d / name

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

        if not self._preflight(run_id, run.stage, report):
            return report

        handler = {
            RunStage.UPLOAD: self._upload,
            RunStage.RECONCILE: self._reconcile,
            RunStage.CHORES: self._chores,
            RunStage.AUDIT: self._audit,
        }[run.stage]
        handler(run_id, report)
        return report

    def _preflight(self, run_id: int, stage: RunStage, report: LaneReport) -> bool:
        """Auth check, then the apply gate. Both park the run rather than failing it.

        The session check runs for all four stages. Only `report.py` is browserless,
        and the stage that calls it opens with a catalog scrape that is not.
        """
        if not self._session_ok(run_id, report):
            return False
        if not self.apply and stage in (RunStage.UPLOAD, RunStage.CHORES):
            # The two mutating stages. Reconcile and audit only read, so a
            # preview-mode run still gets to prove what a real one would find.
            self.store.block(run_id, BlockReason.AWAITING_APPROVAL,
                             f"stage {stage} would write to ElectiCode — "
                             f"re-run with apply=True to proceed")
            report.blocked = BlockReason.AWAITING_APPROVAL
            return False
        return True

    def _session_ok(self, run_id: int, report: LaneReport) -> bool:
        r = self.client.session()
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

        preview = self.client.upload(folder, self.artefact(run_id, "upload-preview.json"))
        report.ran.append("upload:preview")
        if not preview.ok:
            self._setback(run_id, preview, report, "upload preview")
            return

        rows: list[Detected] = preview.data or []
        if problem := self._identity_error(problems, rows):
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

        applied = self.client.upload(folder, self.artefact(run_id, "upload.json"), apply=True)
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

    def _identity_error(self, problems: list[Problem], rows: list[Detected]) -> str | None:
        """Refuse the upload unless every row is the problem Maestro meant.

        Three ways this goes wrong, all silent if unchecked: the platform detects
        fewer folders than were handed to it; a row's Problem ID is not a slug
        this run owns; or an EXISTS row would overwrite a problem whose current
        title is not the one Maestro expects for that slug — a genuine collision
        with unrelated content, which the platform will happily replace.
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
            return (f"the platform detected {len(extra)} problem(s) this run does not "
                    f"own: {', '.join(extra)}")
        for r in rows:
            if not r.selected:
                return f"{r.id}: the modal left this row unticked, so it would not upload"
            want = expected[r.id].title
            if r.exists and r.overwrite_name and want and r.overwrite_name != want:
                return (f"{r.id}: would overwrite {r.overwrite_name!r}, but this run's "
                        f"problem is titled {want!r} — refusing a possible collision")
        return None

    # ---------------------------------------------------------- stage 6.5

    def _reconcile(self, run_id: int, report: LaneReport) -> None:
        r = self.client.scrape(self.artefact(run_id, "catalog.json"))
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

            r = self.client.chores(
                path, tags_mode=group.tags_mode, apply=True,
                divisions=self.divisions, targets=self.targets,
                list_url=self.list_url, fixmdx=self.fixmdx,
            )
            report.ran.append(f"chores:{group.name}")
            if not r.ok:
                # `list add` is the other non-idempotent step: its de-dup compares
                # the tokens it was handed (slugs) against the list's row *titles*,
                # which almost never match, so a retry re-adds.
                self._setback(run_id, r, report,
                              f"chores for the {group.name} group ({len(group.slugs)} problem(s))",
                              retry_safe=group.retry_safe and not self.list_url)
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
        scrape = self.client.scrape(self.artefact(run_id, "catalog-after.json"))
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

        r = self.client.audit(self.artefact(run_id, "catalog-after.json"), path,
                              self.artefact(run_id, "audit.json"),
                              divisions=self.divisions)
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

        for p in problems:
            self.store.set_problem(run_id, p.slug, stage=ProblemStage.AUDITED,
                                   status=ProblemStatus.OK)
            report.advanced.append(p.slug)
        self.store.set_run(run_id, stage=RunStage.DONE, status=RunStatus.DONE)
        report.run_advanced_to = RunStage.DONE
        self.store.log(run_id, "info",
                       f"audit clean — {len(problems)} problem(s) match the characteristics")

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
