import itertools
import threading
import time
from pathlib import Path

import pytest

from maestro.model import BlockReason, ProblemSeed, RunStage, RunStatus
from maestro.scheduler import Scheduler
from maestro.store import Store

from tests.conftest import SLUGS


class FakeLane:
    """Records which runs it was stepped with, and can be made slow or angry."""

    def __init__(self, advance_to=None):
        self.calls: list[int] = []
        self.advance_to = advance_to
        self.raises: Exception | None = None
        self.gate: threading.Event | None = None
        self.entered = threading.Event()

    def step(self, run_id):
        self.calls.append(run_id)
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(5)
        if self.raises is not None:
            raise self.raises
        return Report(self.advance_to)


class Report:
    def __init__(self, advance_to=None):
        self.advanced = []
        self.quarantined = []
        self.run_advanced_to = advance_to
        self.blocked = None
        self.failed = None


@pytest.fixture
def sched(tmp_path):
    store = Store(tmp_path / "m.db")
    polygon, electi = FakeLane(), FakeLane()
    s = Scheduler(store, polygon, electi, busy_interval=0.01, idle_interval=0.01)
    yield s, store, polygon, electi
    s.stop(timeout=5)
    store.close()


_n = itertools.count()


def make_run(store, stage, *, status=RunStatus.RUNNING, name=None):
    """`set_name` is UNIQUE, so every run needs a distinct one."""
    name = name or f"set-{next(_n)}"
    run_id = store.create_run(name, "/tmp/x",
                              [ProblemSeed(slug=SLUGS[0], idx=1, title="T", archive="a.zip")])
    store.set_run(run_id, stage=stage, status=status)
    return run_id


# ------------------------------------------------------------------ routing


def test_a_polygon_run_goes_to_the_polygon_lane(sched):
    s, store, polygon, electi = sched
    run_id = make_run(store, RunStage.POLYGON)
    s.tick()
    assert polygon.calls == [run_id]
    assert electi.calls == []


def test_an_upload_run_goes_to_the_electicode_worker(sched):
    s, store, polygon, electi = sched
    run_id = make_run(store, RunStage.UPLOAD)
    s.tick()
    s.stop(timeout=5)
    assert electi.calls == [run_id]
    assert polygon.calls == []


@pytest.mark.parametrize("stage", [RunStage.RECONCILE, RunStage.CHORES, RunStage.AUDIT])
def test_every_electicode_stage_routes_to_the_worker(sched, stage):
    s, store, polygon, electi = sched
    run_id = make_run(store, stage)
    s.tick()
    s.stop(timeout=5)
    assert electi.calls == [run_id]


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.BLOCKED, RunStatus.DONE])
def test_a_stopped_run_is_never_stepped(sched, status):
    """FAILED and BLOCKED are terminal until something external clears them."""
    s, store, polygon, electi = sched
    make_run(store, RunStage.POLYGON, status=status)
    make_run(store, RunStage.UPLOAD, status=status)
    s.tick()
    s.stop(timeout=5)
    assert polygon.calls == [] and electi.calls == []


def test_a_done_run_is_left_alone(sched):
    s, store, polygon, electi = sched
    make_run(store, RunStage.DONE, status=RunStatus.RUNNING)
    s.tick()
    assert polygon.calls == [] and electi.calls == []


# -------------------------------------------------------------- concurrency


def test_polygon_runs_advance_together(sched):
    """The Polygon half fans out — no reason to serialise it."""
    s, store, polygon, electi = sched
    ids = [make_run(store, RunStage.POLYGON, name=f"set{i}") for i in range(3)]
    s.tick()
    assert polygon.calls == ids


def test_only_one_electicode_run_at_a_time(sched):
    """Two writers on one ElectiCode account corrupt each other's UI state."""
    s, store, polygon, electi = sched
    electi.gate = threading.Event()
    first = make_run(store, RunStage.UPLOAD, name="a")
    make_run(store, RunStage.CHORES, name="b")

    s.tick()
    assert electi.entered.wait(5)
    report = s.tick()                      # while the first is still in flight

    assert electi.calls == [first]
    assert report.electicode_busy is True
    assert report.electicode is None
    electi.gate.set()


def test_a_run_in_flight_keeps_priority_until_it_leaves(sched):
    """Oldest-first, and "oldest" doesn't yield just because a tick elapsed."""
    s, store, polygon, electi = sched
    first = make_run(store, RunStage.UPLOAD, name="a")
    make_run(store, RunStage.CHORES, name="b")

    for _ in range(3):
        s.tick()
        s.stop(timeout=5)
        s._stop.clear()
    assert set(electi.calls) == {first}, "a newer run jumped the queue"


def test_the_worker_picks_up_the_next_run_once_the_first_finishes(sched):
    s, store, polygon, electi = sched
    first = make_run(store, RunStage.UPLOAD, name="a")
    second = make_run(store, RunStage.CHORES, name="b")

    s.tick()
    s.stop(timeout=5)
    s._stop.clear()
    store.set_run(first, stage=RunStage.DONE, status=RunStatus.DONE)

    s.tick()
    s.stop(timeout=5)
    assert electi.calls == [first, second]


def test_the_tick_does_not_block_on_electicode(sched):
    """A batch run takes tens of minutes; a tick that waited would stall everything."""
    s, store, polygon, electi = sched
    electi.gate = threading.Event()
    make_run(store, RunStage.CHORES)
    make_run(store, RunStage.POLYGON, name="polygon-set")

    start = time.monotonic()
    s.tick()
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, "tick waited for the ElectiCode stage"
    assert polygon.calls, "the Polygon run was starved behind ElectiCode"
    electi.gate.set()


def test_a_run_that_finishes_polygon_starts_electicode_the_same_tick(sched):
    s, store, polygon, electi = sched
    run_id = make_run(store, RunStage.POLYGON)
    polygon.advance_to = RunStage.UPLOAD

    def advance(rid):
        polygon.calls.append(rid)
        store.set_run(rid, stage=RunStage.UPLOAD)
        return Report(RunStage.UPLOAD)

    polygon.step = advance
    s.tick()
    s.stop(timeout=5)
    assert electi.calls == [run_id]


# ------------------------------------------------------------------ results


def test_worker_results_land_on_a_later_tick(sched):
    """They cannot mutate the report of the tick that started them."""
    s, store, polygon, electi = sched
    run_id = make_run(store, RunStage.UPLOAD)
    electi.advance_to = RunStage.RECONCILE

    first = s.tick()
    assert first.advanced == []           # the worker hasn't finished yet
    s.stop(timeout=5)
    s._stop.clear()

    second = s.tick()
    assert f"{run_id}→{RunStage.RECONCILE}" in second.advanced


def test_a_lane_that_raises_fails_the_run(sched):
    s, store, polygon, electi = sched
    run_id = make_run(store, RunStage.POLYGON)
    polygon.raises = RuntimeError("boom")

    report = s.tick()
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "RuntimeError: boom" in run.error
    assert any("boom" in e for e in report.errors)


def test_a_raising_worker_fails_the_run_rather_than_vanishing(sched):
    """Nobody joins that thread; an escaping exception would be invisible."""
    s, store, polygon, electi = sched
    run_id = make_run(store, RunStage.CHORES)
    electi.raises = RuntimeError("browser died")

    s.tick()
    s.stop(timeout=5)
    assert store.get_run(run_id).status is RunStatus.FAILED

    s._stop.clear()
    report = s.tick()
    assert any("browser died" in e for e in report.errors)


def test_a_failed_run_is_not_retried_on_the_next_tick(sched):
    s, store, polygon, electi = sched
    make_run(store, RunStage.POLYGON)
    polygon.raises = RuntimeError("boom")
    s.tick()
    s.tick()
    assert len(polygon.calls) == 1


# -------------------------------------------------------------- the loop


def test_run_forever_stops_when_asked(sched):
    s, store, polygon, electi = sched
    make_run(store, RunStage.POLYGON)
    t = threading.Thread(target=s.run_forever, daemon=True)
    t.start()
    time.sleep(0.2)
    s.stop(timeout=5)
    t.join(5)
    assert not t.is_alive(), "stop() did not interrupt the wait"


def test_max_ticks_bounds_the_loop(sched):
    s, store, polygon, electi = sched
    make_run(store, RunStage.POLYGON)
    s.run_forever(max_ticks=3)
    assert len(polygon.calls) == 3


def test_an_idle_tick_reports_itself_as_idle(sched):
    s, store, polygon, electi = sched
    assert s.tick().idle is True
    make_run(store, RunStage.POLYGON)
    polygon.advance_to = RunStage.UPLOAD
    assert s.tick().idle is False


# ------------------------------------------------------------------ ingest


def test_ingest_picks_up_a_dropped_set(tmp_path, set_dir):
    watch = tmp_path / "watch"
    watch.mkdir()
    target = watch / set_dir.name
    import shutil
    shutil.copytree(set_dir, target)

    store = Store(tmp_path / "m.db")
    polygon, electi = FakeLane(), FakeLane()
    s = Scheduler(store, polygon, electi, watch_dir=watch)
    try:
        report = s.tick()
        assert report.ingested == [set_dir.name]
        assert len(store.list_runs()) == 1
        s.tick()                       # a second sweep must not re-ingest
        assert len(store.list_runs()) == 1
    finally:
        s.stop(timeout=5)
        store.close()


def test_an_unreadable_watch_folder_does_not_kill_the_loop(sched, monkeypatch):
    s, store, polygon, electi = sched
    s.watch_dir = Path("/nonexistent/watch")
    run_id = make_run(store, RunStage.POLYGON)

    def boom(*a, **kw):
        raise OSError("network mount went away")

    monkeypatch.setattr("maestro.scheduler.scan", boom)
    report = s.tick()
    assert any("watch folder unreadable" in e for e in report.errors)
    assert polygon.calls == [run_id], "runs in flight must be unaffected"
