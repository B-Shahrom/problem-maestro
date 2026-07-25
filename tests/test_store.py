from pathlib import Path

import pytest

from maestro.model import (
    BlockReason, ProblemSeed, ProblemStage, ProblemStatus, RunStage, RunStatus,
)
from maestro.store import Store

SEEDS = [
    ProblemSeed(slug="edu-arrays-running-max", idx=1, title="Running Maximum",
                archive="edu-arrays-running-max.zip"),
    ProblemSeed(slug="edu-arrays-largest-gap", idx=2, title="Largest Gap",
                archive="edu-arrays-largest-gap.zip"),
    ProblemSeed(slug="edu-sorting-podium-order", idx=3, title="Podium Order",
                archive="edu-sorting-podium-order.zip"),
]


@pytest.fixture
def store(tmp_path: Path) -> Store:
    with Store(tmp_path / "maestro.db") as s:
        yield s


@pytest.fixture
def run(store: Store) -> int:
    return store.create_run("edu-arrays-20260725", "/sets/edu-arrays-20260725", SEEDS)


def test_create_run_seeds_problems_in_order(store, run):
    r = store.get_run(run)
    assert r.stage is RunStage.INGEST and r.status is RunStatus.PENDING
    assert [p.slug for p in r.problems] == [s.slug for s in SEEDS]
    assert all(p.stage is ProblemStage.PENDING for p in r.problems)


def test_run_names_are_unique(store, run):
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        store.create_run("edu-arrays-20260725", "/elsewhere", SEEDS)


def test_empty_run_rejected(store):
    with pytest.raises(ValueError):
        store.create_run("empty-20260725", "/sets/empty", [])


def test_identity_map_persists(store, run):
    store.set_problem(run, "edu-arrays-largest-gap",
                      polygon_problem_id=563392, polygon_package_id=1425709,
                      electicode_slug="edu-arrays-largest-gap")
    p = next(p for p in store.problems(run) if p.slug == "edu-arrays-largest-gap")
    assert (p.polygon_problem_id, p.polygon_package_id) == (563392, 1425709)
    assert p.electicode_slug == "edu-arrays-largest-gap"


def test_existed_before_upload_is_tristate(store, run):
    """Unknown must stay distinguishable from False — it drives reset-vs-add."""
    p = store.problems(run)[0]
    assert p.existed_before_upload is None
    store.set_problem(run, p.slug, existed_before_upload=True)
    assert store.problems(run)[0].existed_before_upload is True


def test_quarantine_excludes_from_batch_but_keeps_siblings(store, run):
    store.quarantine(run, "edu-arrays-largest-gap", "verify FAILED: checker rejected test 12")
    active = store.active(run)
    assert [p.slug for p in active] == ["edu-arrays-running-max", "edu-sorting-podium-order"]
    dead = next(p for p in store.problems(run) if p.slug == "edu-arrays-largest-gap")
    assert dead.status is ProblemStatus.QUARANTINED
    assert "test 12" in dead.error


def test_ready_for_follows_previous_stage(store, run):
    assert len(store.ready_for(run, ProblemStage.IMPORTED)) == 3
    store.set_problem(run, "edu-arrays-running-max",
                      stage=ProblemStage.IMPORTED, status=ProblemStatus.OK)
    assert [p.slug for p in store.ready_for(run, ProblemStage.IMPORTED)] == [
        "edu-arrays-largest-gap", "edu-sorting-podium-order"]
    assert [p.slug for p in store.ready_for(run, ProblemStage.BUILT)] == [
        "edu-arrays-running-max"]


def test_ready_for_skips_quarantined(store, run):
    store.quarantine(run, "edu-arrays-largest-gap", "verify failed")
    assert "edu-arrays-largest-gap" not in [
        p.slug for p in store.ready_for(run, ProblemStage.IMPORTED)]


def test_block_records_reason_and_clears_on_resume(store, run):
    store.block(run, BlockReason.SESSION_EXPIRED, "ElectiCode session expired; log in on the host")
    r = store.get_run(run)
    assert r.status is RunStatus.BLOCKED and r.block_reason is BlockReason.SESSION_EXPIRED
    store.set_run(run, status=RunStatus.RUNNING)
    r = store.get_run(run)
    assert r.status is RunStatus.RUNNING and r.block_reason is None


def test_events_tail_by_cursor(store, run):
    first = store.log(run, "info", "one")
    store.log(run, "info", "two", slug="edu-arrays-running-max")
    tail = store.events(run, after_id=first)
    assert [e["message"] for e in tail] == ["two"]
    assert tail[0]["slug"] == "edu-arrays-running-max"


def test_resume_point_reports_outstanding_polygon_work(store, run):
    store.set_run(run, stage=RunStage.POLYGON, status=RunStatus.RUNNING)
    for slug in ("edu-arrays-running-max", "edu-arrays-largest-gap"):
        store.set_problem(run, slug, stage=ProblemStage.SHAPED, status=ProblemStatus.OK)
    stage, pending = store.resume_point(run)
    assert stage is RunStage.POLYGON
    assert [p.slug for p in pending] == ["edu-sorting-podium-order"]


def test_resume_point_outside_polygon_has_no_per_problem_work(store, run):
    store.set_run(run, stage=RunStage.CHORES)
    stage, pending = store.resume_point(run)
    assert stage is RunStage.CHORES and pending == []


def test_state_survives_reopen(tmp_path):
    db = tmp_path / "maestro.db"
    with Store(db) as s:
        rid = s.create_run("edu-arrays-20260725", "/sets/x", SEEDS)
        s.set_run(rid, stage=RunStage.POLYGON, status=RunStatus.RUNNING)
        s.quarantine(rid, "edu-arrays-largest-gap", "verify failed")
    with Store(db) as s:
        r = s.get_run(rid)
        assert r.stage is RunStage.POLYGON
        assert len(s.active(rid)) == 2


def test_unknown_problem_raises(store, run):
    with pytest.raises(KeyError):
        store.set_problem(run, "not-a-slug", status=ProblemStatus.OK)


def test_the_store_survives_concurrent_writers(tmp_path):
    """The scheduler writes from its loop thread and its ElectiCode worker.

    Without a lock, two `BEGIN IMMEDIATE`s on one connection raise "cannot start a
    transaction within a transaction" — and `bump_attempt` reads back a row it
    just wrote, so an interleaving would also let one thread return the other's
    count.
    """
    import threading

    store = Store(tmp_path / "m.db")
    try:
        run_id = store.create_run("s", "/tmp/x",
                                  [ProblemSeed(slug="a", idx=1, title="A", archive="a.zip")])
        errors: list[Exception] = []
        counts: list[int] = []
        barrier = threading.Barrier(8)

        def hammer(n):
            try:
                barrier.wait(5)
                for i in range(25):
                    store.log(run_id, "info", f"{n}:{i}")
                    counts.append(store.bump_attempt(run_id, "a"))
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

        assert not errors, errors
        assert len(store.events(run_id, limit=1000)) == 200
        assert store.problems(run_id)[0].attempts == 200
        assert sorted(counts) == list(range(1, 201)), "a count was returned twice"
    finally:
        store.close()
