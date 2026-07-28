"""Saying what is happening while it happens, and saying how a tool died.

Both are answers to one report from the field: an upload appeared to hang, was
force-killed, and the only trace was `upload failed: unexpected exit code
3221225786`. That number is not an exit code — it is `STATUS_CONTROL_C_EXIT`,
Windows for "someone pressed Ctrl-C" — and the twenty silent minutes before it
were what made killing it look reasonable.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from maestro import scraper
from maestro.electicode_lane import LOG_CAP, Reporter, _event_line
from maestro.model import ProblemSeed
from maestro.scraper import Outcome, Progress, interpret, killed, run_streamed
from maestro.store import Store

PY = sys.executable


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        yield s


@pytest.fixture
def run_id(store):
    return store.create_run("set", "/tmp/x",
                            [ProblemSeed(slug="a-one", idx=1, title="T", archive="a.zip")])


# ------------------------------------------------------- decoding the death


def test_the_reported_exit_code_is_recognised_as_ctrl_c():
    """The exact number from the field report."""
    how = killed(3221225786)
    assert how and "Ctrl-C" in how and "STATUS_CONTROL_C_EXIT" in how


def test_a_killed_process_is_not_reported_as_a_tool_failure():
    """It never reached its own exit path, so its code says nothing about the request."""
    outcome, reason = interpret(3221225786)
    assert outcome is Outcome.RETRY
    assert "did not fail" in reason
    assert "unexpected exit code" not in reason


@pytest.mark.parametrize("rc,marker", [
    (-2, "SIGINT"),
    (-9, "SIGKILL"),
    (-11, "SIGSEGV"),
    (137, "SIGKILL"),
    (143, "SIGTERM"),
    (0xC0000005, "access violation"),
    (0xC0000142, "DLL"),
])
def test_every_encoding_of_a_kill_is_decoded(rc, marker):
    """Popen reports a signal negative, a shell reports 128+n, Windows an NTSTATUS."""
    how = killed(rc)
    assert how and marker in how


@pytest.mark.parametrize("rc", [0, 1, 2, 3])
def test_an_ordinary_exit_code_is_left_alone(rc):
    assert killed(rc) is None


def test_a_genuinely_unknown_code_still_halts():
    """Kill-decoding must not swallow the "running against a version I don't know" case."""
    assert interpret(42)[0] is Outcome.HALT
    assert "unexpected exit code 42" in interpret(42)[1]


# --------------------------------------------------------------- streaming


def test_output_arrives_while_the_tool_is_still_running():
    """The whole point. Capturing and returning at the end is what made a
    twenty-minute stage indistinguishable from a hung one."""
    seen: list[Progress] = []
    script = ("import sys, time\n"
              "print('one', flush=True)\n"
              "time.sleep(0.6)\n"
              "print('two', flush=True)\n")
    started = time.monotonic()
    rc, out, _ = run_streamed([PY, "-c", script], 30, seen.append)

    assert rc == 0
    assert [p.line for p in seen if p.stream == "stdout"] == ["one", "two"]
    # The first line must have been reported well before the process ended.
    first = next(p for p in seen if p.line == "one")
    assert first.elapsed < (time.monotonic() - started) - 0.3


def test_the_full_output_is_still_returned_whole():
    """Every existing caller parses the returned text; streaming must not cost that."""
    rc, out, err = run_streamed(
        [PY, "-c", "import sys; print('a'); print('b'); print('e', file=sys.stderr)"], 30)
    assert rc == 0
    assert out.splitlines() == ["a", "b"]
    assert err.strip() == "e"


def test_silence_is_reported_as_silence():
    """"Slow" and "not responding" are identical from outside without this."""
    seen: list[Progress] = []
    run_streamed([PY, "-c", "import time; time.sleep(1.2)"], 30, seen.append,
                 quiet_after=0.2)
    beats = [p for p in seen if p.stream == "heartbeat"]
    assert beats, "a tool that said nothing for a second reported nothing at all"
    assert "no output at all yet" in beats[0].line
    # One per quiet period, not one per poll — the waiter wakes several times
    # inside each period and must not report on every wake.
    assert len(beats) <= round(1.2 / 0.2) + 1


def test_a_heartbeat_quotes_the_last_thing_it_heard():
    seen: list[Progress] = []
    run_streamed([PY, "-c", "print('working on a-one', flush=True)\n"
                            "import time; time.sleep(1.2)"], 30, seen.append, quiet_after=0.2)
    beat = next(p for p in seen if p.stream == "heartbeat")
    assert "a-one" in beat.line


def test_a_timeout_kills_the_tool_and_says_so():
    rc, _, err = run_streamed([PY, "-c", "import time; time.sleep(60)"], 0.5)
    assert interpret(rc)[0] is Outcome.RETRY
    assert "timed out" in err and "killed" in err


def test_a_tool_that_cannot_start_is_reported_not_raised():
    rc, _, err = run_streamed(["/definitely/not/a/program"], 5)
    assert rc == 2
    assert "could not start" in err


def test_a_failing_progress_callback_does_not_kill_the_run():
    """Reporting is a convenience; the stage it reports on is not."""
    def boom(_p):
        raise RuntimeError("the log is on fire")

    rc, out, _ = run_streamed([PY, "-c", "print('fine')"], 30, boom)
    assert rc == 0 and out.strip() == "fine"


def test_the_child_is_not_in_maestro_s_signal_group():
    """Ctrl-C to a console reaches the whole group — which is how the browser died.

    Verified structurally rather than by sending a real signal: the test runner
    is in that same group, so proving it the direct way would kill the suite.
    """
    opts = scraper._detached()
    if sys.platform == "win32":
        assert opts["creationflags"] & 0x00000200  # CREATE_NEW_PROCESS_GROUP
    else:
        assert opts["start_new_session"] is True


def test_a_detached_child_really_is_in_its_own_group():
    """The POSIX half of the above, checked for real."""
    if sys.platform == "win32":
        pytest.skip("process groups are checked structurally on Windows")
    import os

    rc, out, _ = run_streamed([PY, "-c", "import os; print(os.getpgrp())"], 30)
    assert rc == 0
    assert int(out.strip()) != os.getpgrp()


# ---------------------------------------------------------------- reporting


@pytest.mark.parametrize("line,want", [
    ('{"event": "start", "total": 12}', "starting — 12 step(s)"),
    ('{"event": "item", "id": "metadata", "ok": true}', "metadata: ok"),
    ('{"event": "item", "id": "division", "ok": false, "error": "timeout"}',
     "division: FAILED — timeout"),
    ('{"event": "item", "id": "a-one", "exists": true}', "detected a-one (exists)"),
    ('{"event": "item", "id": "a-two", "exists": false}', "detected a-two (new)"),
])
def test_json_events_become_readable_lines(line, want):
    assert _event_line(line) == want


@pytest.mark.parametrize("line", [
    "Playwright warning: something",
    "",
    "{not json",
    '{"no": "event key"}',
])
def test_non_events_are_left_for_the_raw_path(line):
    assert _event_line(line) is None


def test_progress_reaches_the_run_s_event_log(store, run_id):
    say = Reporter(store, run_id, "upload")
    say(Progress("stdout", '{"event": "item", "id": "a-one", "exists": false}', 1.0))
    (row,) = store.events(run_id)
    assert row["message"] == "upload: detected a-one (new)"
    assert row["level"] == "info"


def test_a_heartbeat_is_logged_loudly(store, run_id):
    """It is the one line that answers "is it stuck", so it must not read as chatter."""
    say = Reporter(store, run_id, "chores/fresh")
    say(Progress("heartbeat", "still running after 300s, but nothing for 120s", 300.0))
    (row,) = store.events(run_id)
    assert row["level"] == "warn"
    assert "still running after 300s" in row["message"]


def test_browser_noise_is_kept_quiet(store, run_id):
    say = Reporter(store, run_id, "upload")
    say(Progress("stdout", "chromium: some warning", 1.0))
    say(Progress("stderr", "a real complaint", 1.0))
    levels = [r["level"] for r in store.events(run_id)]
    assert levels == ["debug", "warn"]


def test_a_very_loud_tool_is_capped_but_says_that_it_was(store, run_id):
    """Truncating silently would reproduce the failure this module exists to fix."""
    say = Reporter(store, run_id, "upload")
    for i in range(LOG_CAP + 50):
        say(Progress("stdout", f"line {i}", float(i)))
    rows = store.events(run_id, limit=LOG_CAP + 100)
    assert len(rows) == LOG_CAP + 1
    assert "further output suppressed" in rows[-1]["message"]
    assert "still captured" in rows[-1]["message"]


def test_the_lane_reports_under_the_stage_that_is_running(tmp_path):
    """The label is what makes the log answer "which stage", not just "something"."""
    from maestro.electicode_lane import ElectiCodeLane
    from maestro.scraper import ScraperClient

    with Store(tmp_path / "m.db") as store:
        rid = store.create_run("s", "/tmp/x",
                               [ProblemSeed(slug="a-one", idx=1, title="T", archive="a.zip")])
        lane = ElectiCodeLane(store, ScraperClient(tmp_path, tmp_path / "s.json"),
                              tmp_path / "runs")
        lane._say(rid, "upload")(Progress("stdout", "hello", 1.0))
        assert store.events(rid)[0]["message"].startswith("upload: ")


# ------------------------------------------------------------- forgetting


def test_deleting_a_run_takes_its_problems_and_events_with_it(store):
    rid = store.create_run("doomed", "/tmp/x",
                           [ProblemSeed(slug="a-one", idx=1, title="T", archive="a.zip")])
    store.log(rid, "info", "something happened")
    assert store.delete_run(rid).set_name == "doomed"
    assert store.get_run(rid) is None
    assert store.problems(rid) == []
    assert store.events(rid) == []


def test_deleting_a_run_frees_its_set_name_for_a_clean_retry(store):
    """The reason the delete exists. `set_name` is UNIQUE and ingest returns
    ALREADY_INGESTED for a name it has seen, so a wedged run owns its folder."""
    seeds = [ProblemSeed(slug="a-one", idx=1, title="T", archive="a.zip")]
    rid = store.create_run("edu-arrays", "/tmp/x", seeds)
    store.delete_run(rid)
    assert store.create_run("edu-arrays", "/tmp/x", seeds) != rid


def test_deleting_a_run_that_is_not_there_says_so_rather_than_raising(store):
    assert store.delete_run(9999) is None


def test_the_dashboard_refuses_to_delete_a_run_mid_upload(tmp_path):
    """A browser uploading for run 12 does not stop because run 12 was deleted."""
    import json
    import urllib.error
    import urllib.request

    from maestro.dashboard import GUARD_HEADER, Dashboard

    class Busy:
        current_electicode = None

    busy = Busy()
    with Store(tmp_path / "m.db") as store:
        rid = store.create_run("live", "/tmp/x",
                               [ProblemSeed(slug="a-one", idx=1, title="T", archive="a.zip")])
        busy.current_electicode = rid
        with Dashboard(store, port=0, scheduler=busy) as dash:
            url = f"http://127.0.0.1:{dash.port}/api/runs/{rid}/forget"
            req = urllib.request.Request(url, method="POST", headers={GUARD_HEADER: "1"})
            with pytest.raises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=5)
            assert e.value.code == 409
            assert "uploaded right now" in json.loads(e.value.read())["error"]
            assert store.get_run(rid) is not None

            # …and permits it once the worker has moved on.
            busy.current_electicode = None
            with urllib.request.urlopen(req, timeout=5) as r:
                assert json.load(r)["deleted"] == rid
            assert store.get_run(rid) is None


def test_a_timeout_never_signals_maestro_s_own_process_group(monkeypatch):
    """The guard on `_kill_tree`, which is the difference between losing a
    browser and losing Maestro.

    Found by mutation: with `_detached` neutered, the child shares the runner's
    group and the timeout path took the whole test session down with it.
    """
    if sys.platform == "win32":
        pytest.skip("process groups are POSIX")
    monkeypatch.setattr(scraper, "_detached", dict)
    rc, _, err = run_streamed([PY, "-c", "import time; time.sleep(60)"], 0.5)
    assert "timed out" in err  # we are still alive to assert it
    assert rc == 2


# ------------------------------------------------------------- the actual page


def test_no_data_is_ever_written_into_an_html_attribute():
    """The bug that shipped: the delete button did nothing.

    `JSON.stringify(run.set_name)` was interpolated into `onclick="..."`. It
    emits double quotes, so the attribute closed on itself — the button rendered
    perfectly and its handler did not parse. The endpoint test passed the whole
    time, because the endpoint was never the problem.

    The fix is structural, so the check is too: handlers are delegated from
    `data-act`, and an `onclick` anywhere in the page means someone has started
    putting values back into markup.
    """
    import re

    from maestro.dashboard import PAGE

    # Prose about the bug is allowed; markup that reintroduces it is not.
    code = re.sub(r"^\s*//.*$", "", PAGE, flags=re.M)
    assert not re.search(r"\bon[a-z]+\s*=", code), (
        "inline handlers are how a set name closed an attribute and killed the "
        "button; bind from data-act instead")


def test_every_action_the_page_offers_is_one_the_server_accepts():
    """A button naming an action the router rejects is a button that does nothing."""
    import re

    from maestro.dashboard import PAGE

    offered = set(re.findall(r'data-act="([a-z]+)"', PAGE))
    assert offered == {"select", "approve", "resume", "forget"}
    # `select` is client-side; the rest are POST routes.
    assert offered - {"select"} == {"approve", "resume", "forget"}


def test_the_delete_button_is_rendered_for_a_selected_run():
    from maestro.dashboard import PAGE

    assert 'data-act="forget"' in PAGE
    assert "delete run" in PAGE


def test_the_page_escapes_quotes_too():
    """It did not before — which is what let a value break out of an attribute."""
    from maestro.dashboard import PAGE

    esc = next(l for l in PAGE.splitlines() if "const esc" in l)
    assert '"' in esc.split("replace")[1][:20] or "&quot;" in PAGE


# --------------------------------------------------- not scraping 41 pages


def _lane(tmp_path, divisions=""):
    from maestro.electicode_lane import ElectiCodeLane
    from maestro.scraper import ScraperClient

    calls: list[list[str]] = []

    def runner(argv, timeout, progress=None):
        calls.append(argv)
        return 0, "", ""

    store = Store(tmp_path / "m.db")
    client = ScraperClient(tmp_path / "repo", tmp_path / "s.json", runner=runner)
    return ElectiCodeLane(store, client, tmp_path / "runs", divisions=divisions), calls, store


def test_reconcile_reads_the_catalog_in_one_page_load(tmp_path):
    """It only asks whether a slug exists, which the catalog answers."""
    lane, calls, store = _lane(tmp_path)
    try:
        lane.client.scrape(tmp_path / "c.json", from_catalog=True)
        assert "--from-catalog" in calls[0]
    finally:
        store.close()


def test_the_audit_pages_the_table_only_when_divisions_are_at_stake(tmp_path):
    """The catalog carries no `division_access`, so a run that granted some has
    to page. A run that granted none gains nothing from 41 page loads."""
    with_div, _, s1 = _lane(tmp_path / "a", divisions="Electi")
    without, _, s2 = _lane(tmp_path / "b")
    try:
        assert with_div._needs_paged_scrape() is True
        assert without._needs_paged_scrape() is False
    finally:
        s1.close()
        s2.close()


def test_report_py_is_not_sent_a_state_it_cannot_parse(tmp_path):
    """`report.py` has no session — argparse matched the path against `command`
    and stage 8 could never succeed."""
    lane, calls, store = _lane(tmp_path)
    try:
        lane.client.audit(tmp_path / "s.json", tmp_path / "c.md", tmp_path / "o.json")
        assert "--state" not in calls[0], calls[0]
        assert calls[0][2] == "audit", "the subcommand must come first for report.py"
    finally:
        store.close()


def test_the_browser_tools_still_get_their_state(tmp_path):
    lane, calls, store = _lane(tmp_path)
    try:
        lane.client.scrape(tmp_path / "c.json")
        assert calls[0][2:4] == ["--state", str(tmp_path / "s.json")]
    finally:
        store.close()
