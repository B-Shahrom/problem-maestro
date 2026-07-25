"""Stage 3-5 — drive each problem through import, build+verify, and download.

`step()` performs one non-blocking pass over a run and returns. It is meant to be
called on a timer: a Polygon build takes about a minute per problem, so anything
that blocked would spend the whole run asleep and lose its state on a restart.

**One job per problem.** The Middleman accepts several archives in a single job,
but Maestro submits one problem at a time. A job is the unit of retry, and a
shared job would make one bad archive's `STEP_FAILED` indistinguishable from its
siblings' — the retry cap would then punish problems that never failed. Per-slug
serialisation on the Middleman side makes many small jobs safe.

Shaping collapsed to almost nothing once the upload test showed ElectiCode accepts
Polygon's native layout: extract the package into a folder named for the slug and
drop the Windows binaries. No file moves, no renames.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .model import ProblemStage, ProblemStatus, RunStage, RunStatus
from .polygon import Action, PolygonClient, PolygonError, decide, problem_decisions
from .store import Store


@dataclass(slots=True)
class LaneReport:
    """What one pass did. Keys are slugs."""

    actions: dict[str, Action] = field(default_factory=dict)
    advanced: list[str] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    run_advanced_to: RunStage | None = None

    @property
    def idle(self) -> bool:
        """Nothing moved — safe to sleep longer before the next pass."""
        return not (self.advanced or self.quarantined or self.run_advanced_to)


class PolygonLane:
    def __init__(self, store: Store, client: PolygonClient, work_dir: str | Path) -> None:
        self.store = store
        self.client = client
        self.work_dir = Path(work_dir)

    def upload_dir(self, run_id: int) -> Path:
        """The parent folder handed to `problem_uploader --folder`.

        Only problems that reached SHAPED have a subdirectory here, so a
        quarantined problem is excluded by construction rather than by a filter
        someone has to remember to apply.
        """
        return self.work_dir / str(run_id) / "upload"

    def step(self, run_id: int) -> LaneReport:
        report = LaneReport()
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"no run {run_id}")
        if run.stage is not RunStage.POLYGON:
            return report
        set_dir = Path(run.set_dir)

        for p in self.store.active(run_id):
            if p.stage is ProblemStage.SHAPED and p.status is ProblemStatus.OK:
                continue
            try:
                action = self._advance(run_id, p, set_dir, report)
            except PolygonError as e:
                # A rejected submission is the Middleman refusing the request itself
                # (bad multipart, missing credentials) — not a per-problem content
                # error, so it stops the run rather than quarantining one problem.
                self.store.log(run_id, "error", f"import rejected: {e}", slug=p.slug)
                self.store.set_run(run_id, status=RunStatus.FAILED, error=str(e))
                return report
            report.actions[p.slug] = action

        self._maybe_finish(run_id, report)
        return report

    # ------------------------------------------------------------ per problem

    def _advance(self, run_id: int, p, set_dir: Path, report: LaneReport) -> Action:
        if p.stage is ProblemStage.BUILT:
            return self._download(run_id, p, report)
        if p.polygon_job_id is None:
            return self._submit(run_id, p, set_dir)
        return self._poll(run_id, p, report)

    def _submit(self, run_id: int, p, set_dir: Path) -> Action:
        archives = [str(set_dir / p.archive)]
        tests = set_dir / f"{p.slug}-tests.zip"
        if tests.is_file():
            # A tests archive is redundant with the main one by contract, but the
            # append path exists for partial re-deliveries where it is all there is.
            archives.append(str(tests))
        job = self.client.import_problem(archives)
        self.store.set_problem(run_id, p.slug, status=ProblemStatus.RUNNING,
                               polygon_job_id=job["jobId"])
        self.store.log(run_id, "info", f"submitted job {job['jobId']}", slug=p.slug)
        return Action.WAIT

    def _poll(self, run_id: int, p, report: LaneReport) -> Action:
        status, body = self.client.verify_status(p.polygon_job_id)
        decisions = problem_decisions(status, body, attempts={p.slug: p.attempts})
        # A lost job folds to a single "*" decision covering the whole response.
        d = decisions.get(p.slug) or decisions.get("*") or decide()

        if d.action in (Action.RESUBMIT, Action.RETRY):
            n = self.store.bump_attempt(run_id, p.slug)
            self.store.clear_job(run_id, p.slug)
            self.store.log(run_id, "warn", f"{d.reason} — resubmitting (attempt {n + 1})", slug=p.slug)
            return d.action

        if d.action is Action.HALT:
            self.store.quarantine(run_id, p.slug, d.reason)
            report.quarantined.append(p.slug)
            return d.action

        # The response carries two independent facts, and conflating them loses one:
        # the *import* block says how far the problem got, while the *verify* block
        # says what to do next. `decide` folds them into one action deliberately —
        # verify supersedes import, so a committed problem whose build is still
        # running reports WAIT. Stage has to come from the import block instead, or
        # such a problem would sit at PENDING until its build finished and a restart
        # would resubmit work that was already done.
        entry = self._entry(body, p.slug)
        self._record_ids(run_id, p.slug, entry)

        if d.action is Action.SUCCESS:      # VERIFY_READY, or IMPORTED_ALREADY_VERIFIED
            self.store.set_problem(run_id, p.slug, stage=ProblemStage.BUILT,
                                   status=ProblemStatus.OK)
            report.advanced.append(p.slug)
        elif p.stage is ProblemStage.PENDING and entry.get("problemId"):
            # Created and committed on Polygon; the build was requested and is running.
            self.store.set_problem(run_id, p.slug, stage=ProblemStage.IMPORTED,
                                   status=ProblemStatus.RUNNING)
            report.advanced.append(p.slug)
        return d.action

    @staticmethod
    def _entry(body: dict, slug: str) -> dict:
        return next((e for e in body.get("problems", []) if e.get("slug") == slug), {})

    def _record_ids(self, run_id: int, slug: str, entry: dict) -> None:
        """Persist the Polygon half of the identity map as soon as it exists."""
        if not entry:
            return
        verify = entry.get("verify") or {}
        self.store.set_problem(
            run_id, slug,
            polygon_problem_id=entry.get("problemId"),
            polygon_package_id=verify.get("packageId"),
        )

    def _download(self, run_id: int, p, report: LaneReport) -> Action:
        status, raw = self.client.download_package(p.polygon_job_id, p.polygon_problem_id)
        if status != 200:
            d = decide(http_status=status, detail=raw[:200].decode("utf-8", "replace"))
            if d.action is Action.HALT:
                self.store.quarantine(run_id, p.slug, d.reason)
                report.quarantined.append(p.slug)
            return d.action

        dest = self.upload_dir(run_id) / p.slug
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            z.extractall(dest)
        pruned = 0
        for exe in dest.rglob("*.exe"):
            exe.unlink()
            pruned += 1

        self.store.set_problem(run_id, p.slug, stage=ProblemStage.SHAPED, status=ProblemStatus.OK)
        self.store.log(run_id, "info",
                       f"package extracted to {dest.name}/ ({pruned} binaries pruned)", slug=p.slug)
        report.advanced.append(p.slug)
        return Action.SUCCESS

    # ------------------------------------------------------------- run-level

    def _maybe_finish(self, run_id: int, report: LaneReport) -> None:
        active = self.store.active(run_id)
        if not active:
            # Every problem quarantined — there is nothing to upload, so the batch
            # ends here rather than handing an empty folder to the uploader.
            self.store.set_run(run_id, status=RunStatus.FAILED,
                               error="every problem failed Polygon verification")
            self.store.log(run_id, "error", "no problems survived the Polygon stage")
            return
        if all(p.stage is ProblemStage.SHAPED and p.status is ProblemStatus.OK for p in active):
            self.store.set_run(run_id, stage=RunStage.UPLOAD)
            report.run_advanced_to = RunStage.UPLOAD
            self.store.log(run_id, "info", f"{len(active)} problem(s) ready to upload")
