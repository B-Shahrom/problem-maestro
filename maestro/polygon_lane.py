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

from . import manifest
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
        limits = self._limits(run_id, set_dir, p.slug)
        job = self.client.import_problem(
            archives,
            time_limit_s=limits.get("time_limit_s"),
            memory_limit_mb=limits.get("memory_limit_mb"),
        )
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
        if reason := self._limits_applied(run_id, p, entry):
            # Caught here rather than at stage 8. Both are real checks — this one
            # reads what the Middleman sent, the audit reads what the platform
            # ended up with — but failing at import saves a build, a download, an
            # upload and a chore chain that would all have to be redone anyway.
            self.store.quarantine(run_id, p.slug, reason)
            report.quarantined.append(p.slug)
            return Action.HALT

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

    def _limits_applied(self, run_id: int, p, entry: dict) -> str | None:
        """Did `problem.updateInfo` get the limits Maestro sent?

        `appliedTimeLimit`/`appliedMemoryLimit` are what the Middleman actually
        passed, recorded only when that call returned OK — so they confirm the
        send took rather than re-reading Polygon. `null` on a tests-only pack,
        which never calls `updateInfo` at all, and absent on an older Middleman;
        neither is a finding.
        """
        run = self.store.get_run(run_id)
        if run is None:
            return None
        want = self._limits(run_id, Path(run.set_dir), p.slug)
        for label, sent, key, scale in (
            ("time limit", want.get("time_limit_s"), "appliedTimeLimit", 1000),
            ("memory limit", want.get("memory_limit_mb"), "appliedMemoryLimit", 1),
        ):
            got = entry.get(key)
            if sent is None or got is None:
                continue
            if int(round(float(sent) * scale)) != int(got):
                return (f"{label}: Maestro sent {int(round(float(sent) * scale))} but the "
                        f"import applied {int(got)} — the package would carry the wrong one")

        # Where the applied value *came from*, not just what it was. The Middleman
        # can now take limits from a form field, an uploaded manifest, or its own
        # default, and reports which — precisely so a fallback cannot be mistaken
        # for an explicit value.
        #
        # Maestro always sends both form fields when the manifest has them, so
        # anything but `form` there means the send did not arrive and the right
        # number was reached by luck. That is worth catching while it is still
        # true, because the luck runs out the first time the manifest and the
        # form field disagree.
        source = entry.get("limitsSource")
        sent_both = want.get("time_limit_s") is not None and want.get("memory_limit_mb") is not None
        if source and sent_both and source != "form":
            return (f"the import took its limits from {source!r}, not from the fields "
                    f"Maestro sent — the values happen to match, but the send did not "
                    f"take, so the next set where they differ would import wrong")
        return None

    def _limits(self, run_id: int, set_dir: Path, slug: str) -> dict:
        """This problem's authored limits, from the manifest.

        Read from the manifest rather than carried on `Problem` because they are
        input data, not run state: the manifest is authoritative and immutable for
        the life of a run, and copying them into the store would create a second
        place for them to be wrong.

        An absent entry falls back to the Middleman's default, which is the exact
        behaviour this exists to prevent — so it is logged rather than passed over.
        The manifest schema requires `limits`, so reaching this means the set got
        past a validator that should have caught it.
        """
        try:
            m = manifest.load(set_dir)
        except (OSError, ValueError) as e:
            self.store.log(run_id, "warn",
                           f"could not read the manifest for limits ({e}); the import will "
                           "apply the Middleman's default", slug=slug)
            return {}
        entry = next((p for p in m.get("problems") or [] if p.get("slug") == slug), {})
        if limits := entry.get("limits") or {}:
            return limits
        self.store.log(run_id, "warn",
                       "no limits in the manifest; the import will apply the Middleman's "
                       "default, which overwrites whatever the package declares", slug=slug)
        return {}

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
