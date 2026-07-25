"""The loop that drives runs — one tick at a time, resumable, single-process.

Everything below this is a state machine over durable storage; this is the only
part that decides *when* to move. Three constraints shape it, and they conflict:

**The halves have opposite concurrency rules.** The Polygon lane is non-blocking
and fans out — several problems can be importing, building and downloading at
once, and a tick over ten problems returns in milliseconds. The ElectiCode lane
is the reverse: one subprocess driving one browser against one account, where two
concurrent writers corrupt each other's UI state. So a tick may advance many runs
through Polygon but at most one through ElectiCode.

**Nothing may block the loop.** A `batch run` takes tens of minutes. If a tick
waited for it, every other run would stall behind it and a restart would lose the
lot. The ElectiCode lane therefore runs in a worker thread, and the tick that
started it returns immediately; subsequent ticks see it is busy and skip.

**A stopped run stays stopped.** FAILED and BLOCKED are terminal until something
external clears them — a human logs in, approves an apply gate, or fixes a set.
The scheduler never clears them itself, because "retry it and see" is precisely
what the per-stage idempotency rules exist to prevent.

The loop owns no domain logic. It asks each lane to take one step and records
what happened.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .electicode_lane import ElectiCodeLane
from .checks import Severity
from .ingest import Verdict, scan
from .model import RunStage, RunStatus
from .polygon_lane import PolygonLane
from .store import Store

#: Seconds between ticks when something moved, and when nothing did. A Polygon
#: build takes about a minute, so polling faster mostly costs requests; sleeping
#: much longer makes a 25-problem batch's total latency the sum of its naps.
BUSY_INTERVAL = 5.0
IDLE_INTERVAL = 30.0

def _why(result) -> str:
    """The most useful single line from an inspection.

    Errors first: a set with three warnings and one error is rejected for the
    error, and leading with a warning would point at the wrong thing.
    """
    ranked = sorted(result.findings, key=lambda f: f.severity is not Severity.ERROR)
    if not ranked:
        return "no findings recorded"
    first = ranked[0]
    extra = f" (+{len(ranked) - 1} more)" if len(ranked) > 1 else ""
    return f"{first.check}: {first.message}{extra}"


_POLYGON_STAGES = (RunStage.POLYGON,)
_ELECTICODE_STAGES = (RunStage.UPLOAD, RunStage.RECONCILE, RunStage.CHORES, RunStage.AUDIT)


@dataclass
class TickReport:
    ingested: list[str] = field(default_factory=list)
    polygon: list[int] = field(default_factory=list)
    electicode: int | None = None
    """The run handed to the ElectiCode worker this tick, if any."""
    electicode_busy: bool = False
    advanced: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str, str]] = field(default_factory=list)
    """`(folder, verdict, why)` for every candidate the sweep did not ingest.

    Repeats each tick while the cause persists — an operator watching the log
    needs the current state, not a one-off they may have missed.
    """

    @property
    def idle(self) -> bool:
        return not (self.ingested or self.advanced or self.electicode is not None)


class Scheduler:
    def __init__(
        self,
        store: Store,
        polygon: PolygonLane,
        electicode: ElectiCodeLane,
        *,
        watch_dir: str | Path | None = None,
        extra_tags: set[str] | None = None,
        parser=None,
        busy_interval: float = BUSY_INTERVAL,
        idle_interval: float = IDLE_INTERVAL,
    ) -> None:
        self.store = store
        self.polygon = polygon
        self.electicode = electicode
        self.watch_dir = Path(watch_dir) if watch_dir else None
        self.extra_tags = extra_tags
        self.parser = parser
        self.busy_interval = busy_interval
        self.idle_interval = idle_interval
        self._worker: threading.Thread | None = None
        self._results: queue.SimpleQueue = queue.SimpleQueue()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ tick

    def tick(self) -> TickReport:
        """One pass. Never blocks on ElectiCode; safe to call on a timer."""
        report = TickReport()
        self._ingest(report)

        for run_id in self._live(_POLYGON_STAGES):
            self._step_polygon(run_id, report)

        # Re-read: a Polygon run that finished this tick is eligible for ElectiCode
        # immediately, and making it wait a whole interval for no reason would add
        # up over a batch.
        self._pump_electicode(self._live(_ELECTICODE_STAGES), report)
        return report

    def _live(self, stages: tuple[RunStage, ...]) -> list[int]:
        """Run ids at these stages that are still moving, **oldest first**.

        Order is the fairness rule: `_pump_electicode` takes the head of this
        list, so oldest-first is what stops a long-running batch from being
        starved by newer arrivals. `list_runs()` is newest-first for the
        dashboard, which is the opposite of what a queue wants.
        """
        return sorted(r.id for r in self.store.list_runs()
                      if r.status in (RunStatus.PENDING, RunStatus.RUNNING)
                      and r.stage in stages)

    def _ingest(self, report: TickReport) -> None:
        """Sweep the watch folder, and say what happened to everything in it.

        A candidate that is not ingested must be reported, not dropped. Silence
        here reads to an operator as "Maestro didn't see my folder" when the truth
        is almost always "it saw it and rejected it for a stated reason" — and the
        reason is the one thing they need.

        `ALREADY_INGESTED` is the exception: every previously-seen folder returns
        it on every tick forever, so reporting it would bury everything else.
        """
        if self.watch_dir is None:
            return
        try:
            for result in scan(self.watch_dir, self.store,
                               extra_tags=self.extra_tags, parser=self.parser):
                if result.verdict is Verdict.READY and result.run_id:
                    report.ingested.append(result.set_dir.name)
                elif result.verdict is not Verdict.ALREADY_INGESTED:
                    report.rejected.append(
                        (result.set_dir.name, str(result.verdict), _why(result)))
        except OSError as e:
            # The watch folder is often a network mount; a blip must not kill the
            # loop, and every run already in flight is unaffected by it.
            report.errors.append(f"watch folder unreadable: {e}")

    def _step_polygon(self, run_id: int, report: TickReport) -> None:
        try:
            r = self.polygon.step(run_id)
        except Exception as e:  # noqa: BLE001
            report.errors.append(self._crash(run_id, "polygon", e))
            return
        report.polygon.append(run_id)
        report.advanced += [f"{run_id}:{s}" for s in r.advanced]
        if r.run_advanced_to:
            report.advanced.append(f"{run_id}→{r.run_advanced_to}")

    # ----------------------------------------------------------- the worker

    def _pump_electicode(self, waiting: list[int], report: TickReport) -> None:
        """Report what the worker finished, then hand it the next run if it's free.

        Strictly one at a time — see the module docstring. `waiting` is in run
        order, so a batch that entered the ElectiCode half first finishes first
        rather than being starved by newer arrivals.
        """
        self._drain(report)
        if self._worker is not None and self._worker.is_alive():
            report.electicode_busy = True
            return
        self._worker = None
        if not waiting:
            return

        run_id = waiting[0]
        report.electicode = run_id
        self._worker = threading.Thread(
            target=self._run_electicode, args=(run_id,),
            name=f"electicode-{run_id}", daemon=True,
        )
        self._worker.start()

    def _drain(self, report: TickReport) -> None:
        """Move finished worker results into this tick's report.

        The worker cannot write to the report of the tick that started it: that
        tick returned long ago and its caller may already have read it. So results
        cross threads through a queue and land on whichever tick collects them —
        late by up to one interval, but never a mutation of something already
        handed out.
        """
        while True:
            try:
                advanced, error = self._results.get_nowait()
            except queue.Empty:
                return
            report.advanced += advanced
            if error:
                report.errors.append(error)

    def _run_electicode(self, run_id: int) -> None:
        """One ElectiCode stage, off the loop's thread.

        Exceptions are caught rather than raised: nobody joins this thread, so an
        escaping exception would vanish as a stack trace on stderr while the run
        sat at its stage looking healthy.
        """
        advanced: list[str] = []
        error: str | None = None
        try:
            r = self.electicode.step(run_id)
            if r.run_advanced_to:
                advanced.append(f"{run_id}→{r.run_advanced_to}")
            if r.blocked:
                advanced.append(f"{run_id} blocked: {r.blocked}")
            if r.failed:
                error = f"{run_id}: {r.failed}"
        except Exception as e:  # noqa: BLE001
            error = self._crash(run_id, "electicode", e)
        self._results.put((advanced, error))

    def _crash(self, run_id: int, where: str, e: Exception) -> str:
        """A lane raised. Fail the run rather than let the loop retry it forever.

        A lane is meant to fold its expected failures into a status; reaching here
        means an unanticipated one, and those do not become anticipated by being
        retried on a five-second timer.
        """
        message = f"{where} lane raised {type(e).__name__}: {e}"
        try:
            self.store.log(run_id, "error", message)
            self.store.set_run(run_id, status=RunStatus.FAILED, error=message)
        except Exception:  # noqa: BLE001 — the store itself is failing; nothing left to do
            pass
        return f"{run_id}: {message}"

    # -------------------------------------------------------------- running

    def run_forever(self, *, max_ticks: int | None = None, on_tick=None) -> None:
        """Tick until stopped. `max_ticks` bounds it for tests and one-shot runs.

        `on_tick` receives each `TickReport`. The loop itself prints nothing —
        what is worth showing depends on whether a human or a service manager is
        watching, and that is the caller's business.
        """
        ticks = 0
        while not self._stop.is_set():
            report = self.tick()
            if on_tick is not None:
                on_tick(report)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                return
            # Wait on the event rather than sleeping, so stop() is immediate
            # instead of taking up to a full interval.
            self._stop.wait(self.idle_interval if report.idle else self.busy_interval)

    def stop(self, *, timeout: float = 30.0) -> None:
        """Stop ticking and let an in-flight ElectiCode stage finish if it can.

        The worker is a daemon, so it dies with the process regardless. The join
        is a courtesy: a stage killed mid-`batch run` leaves the platform in a
        state only the audit will explain.
        """
        self._stop.set()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout)

    @property
    def busy(self) -> bool:
        return self._worker is not None and self._worker.is_alive()


def build(
    db: str | Path,
    work_dir: str | Path,
    *,
    middleman,
    scraper,
    watch_dir: str | Path | None = None,
    apply: bool = False,
    divisions: str = "",
    targets: str = "",
    list_url: str = "",
) -> tuple[Scheduler, Store]:
    """Wire a scheduler from a config. Returns it with the store it owns.

    `apply` defaults to False: a scheduler that starts writing to a live platform
    the moment it is constructed is not a default anyone should get by omission.
    """
    store = Store(db)
    polygon = PolygonLane(store, middleman, work_dir)
    electicode = ElectiCodeLane(store, scraper, work_dir, apply=apply,
                                divisions=divisions, targets=targets, list_url=list_url)
    sched = Scheduler(store, polygon, electicode, watch_dir=watch_dir,
                      parser=middleman.parse)
    return sched, store
