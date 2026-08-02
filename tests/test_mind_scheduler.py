"""The mind inside the loop: when it is asked, and what it is allowed to do.

`test_mind.py` covers the reading. This covers the seam, which is where the
expensive mistakes live — a diagnosis per tick on a run parked for a week, a
reading acted on that the operator never permitted, or a mind that quietly stops
being consulted at all.
"""

from __future__ import annotations

import pytest

from maestro.mind import Action, Mind, TransportError
from maestro.model import BlockReason, ProblemSeed, RunStage, RunStatus
from maestro.scheduler import Scheduler
from maestro.store import Store

from tests.test_scheduler import FakeLane


def answer(action="escalate", confidence="high"):
    return {"summary": "stopped at chores", "cause": "division set exited 1",
            "action": action, "reason": "because", "confidence": confidence,
            "checks": [], "unknowns": []}


class Counting:
    """A transport that counts calls, so "asked once" is a testable claim."""

    def __init__(self, *answers, raises=None):
        self.answers = list(answers) or [answer()]
        self.raises = raises
        self.calls = 0

    def __call__(self, system, user):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.answers[min(self.calls - 1, len(self.answers) - 1)]


@pytest.fixture
def wired(tmp_path):
    """A scheduler with one failed run and a mind that can be pointed at it."""
    store = Store(tmp_path / "m.db")
    run_id = store.create_run("edu-arrays", str(tmp_path / "set"),
                              [ProblemSeed(slug="edu-arrays-max", idx=1,
                                           title="T", archive="a.zip")])
    store.set_run(run_id, stage=RunStage.CHORES, status=RunStatus.FAILED)

    def build(transport, allowed=None):
        s = Scheduler(store, FakeLane(), FakeLane(),
                      mind=Mind(transport, allowed=allowed or set()),
                      work_dir=tmp_path / "runs")
        return s

    yield build, store, run_id
    store.close()


def minds(store, run_id):
    return [r["message"] for r in store.events(run_id) if r["level"] == "mind"]


# ------------------------------------------------------------------- when


def test_a_stopped_run_is_read_and_the_reading_lands_in_its_log(wired):
    build, store, run_id = wired
    sched = build(Counting())
    report = sched.tick()

    assert [r for r, _ in report.readings] == [run_id]
    assert any("division set exited 1" in m for m in minds(store, run_id))


def test_a_running_run_is_not_read_at_all(wired):
    """Nothing to explain yet, and a request per run per tick for the answer
    "it is working" is the cost this bound exists for."""
    build, store, run_id = wired
    store.set_run(run_id, status=RunStatus.RUNNING)
    t = Counting()
    build(t).tick()
    assert t.calls == 0


def test_an_unchanged_parked_run_is_read_once_however_many_ticks_pass(wired):
    """A parked run is parked for hours and the tick comes round every minute."""
    build, store, run_id = wired
    t = Counting()
    sched = build(t)
    for _ in range(5):
        sched.tick()
    assert t.calls == 1


def test_a_new_line_in_the_log_earns_a_fresh_reading(wired):
    """The state it was read at is gone — something happened to this run."""
    build, store, run_id = wired
    t = Counting()
    sched = build(t)
    sched.tick()
    store.log(run_id, "error", "and now this")
    sched.tick()
    assert t.calls == 2


def test_a_run_that_clears_and_stops_again_is_read_again(wired):
    """The second stop is a different question, and the stamp has to forget."""
    build, store, run_id = wired
    t = Counting()
    sched = build(t)
    sched.tick()
    store.set_run(run_id, status=RunStatus.RUNNING)
    sched.tick()
    store.set_run(run_id, status=RunStatus.FAILED)
    sched.tick()
    assert t.calls == 2


def test_a_mind_that_is_off_costs_the_tick_nothing_and_writes_nothing(wired):
    build, store, run_id = wired
    sched = Scheduler(store, FakeLane(), FakeLane())
    report = sched.tick()
    assert report.readings == []
    assert minds(store, run_id) == []


# ------------------------------------------------------------------- what


def test_nothing_is_acted_on_without_an_allowlist(wired):
    build, store, run_id = wired
    sched = build(Counting(answer("resume")))
    report = sched.tick()

    assert report.acted == []
    assert store.get_run(run_id).status is RunStatus.FAILED
    assert any("not acting" in r["message"] for r in store.events(run_id))


def test_an_allowed_resume_puts_the_run_back_in_the_queue(wired):
    build, store, run_id = wired
    sched = build(Counting(answer("resume")), allowed={Action.RESUME})
    report = sched.tick()

    assert report.acted == [(run_id, "resume")]
    assert store.get_run(run_id).status is RunStatus.RUNNING
    assert any("acted: resume" in m for m in minds(store, run_id))


def test_a_medium_confidence_resume_is_not_taken_even_when_allowed(wired):
    build, store, run_id = wired
    sched = build(Counting(answer("resume", "medium")), allowed={Action.RESUME})
    sched.tick()
    assert store.get_run(run_id).status is RunStatus.FAILED


def test_approve_is_only_taken_when_approve_is_the_allowed_action(wired):
    """The allowlist is per action, not a single "autonomous" switch — an
    operator who trusts `resume` has said nothing about writing to ElectiCode."""
    build, store, run_id = wired
    store.block(run_id, BlockReason.AWAITING_APPROVAL, "gate")
    sched = build(Counting(answer("approve")), allowed={Action.RESUME})
    sched.tick()
    assert not store.get_run(run_id).approved

    sched = build(Counting(answer("approve")), allowed={Action.APPROVE})
    sched.tick()
    assert store.get_run(run_id).approved


def test_wait_changes_nothing_but_is_still_recorded(wired):
    """"I looked and there is nothing to do" is a reading an operator needs; it
    is the one that means the silence is deliberate."""
    build, store, run_id = wired
    sched = build(Counting(answer("wait")), allowed={Action.WAIT})
    report = sched.tick()

    assert report.acted == []
    assert store.get_run(run_id).status is RunStatus.FAILED
    assert minds(store, run_id)


def test_a_tick_that_only_read_something_is_still_idle(wired):
    """Readings happen on stopped runs, and a stopped run is not the loop
    working. Acting is different, and says so."""
    build, store, run_id = wired
    assert build(Counting()).tick().idle
    assert not build(Counting(answer("resume")), allowed={Action.RESUME}).tick().idle


# --------------------------------------------------------- when it cannot answer


def test_an_unreachable_mind_warns_in_the_run_s_own_log(wired):
    """The failure mode this prevents: a stopped run that is simply never read,
    which from the log looks exactly like one the mind found nothing wrong with."""
    build, store, run_id = wired
    sched = build(Counting(raises=TransportError("connection reset")))
    report = sched.tick()

    warnings = [r["message"] for r in store.events(run_id) if r["level"] == "warn"]
    assert any("connection reset" in w for w in warnings)
    assert minds(store, run_id) == []
    assert report.readings and "no reading" in report.readings[0][1]


def test_a_transport_that_explodes_does_not_take_the_tick_with_it(wired):
    build, store, run_id = wired
    sched = build(Counting(raises=RuntimeError("boom")))
    report = sched.tick()          # must not raise
    assert report.readings


def test_an_unavailable_reading_is_retried_on_the_next_change_not_every_tick(wired):
    """A dead API must not turn into one request per parked run per tick.

    The warning it writes is itself a new event, so the stamp moves — which is
    correct (something was said) and self-limiting (the next tick's stamp
    matches).
    """
    build, store, run_id = wired
    t = Counting(raises=TransportError("down"))
    sched = build(t)
    for _ in range(5):
        sched.tick()
    assert t.calls == 1
