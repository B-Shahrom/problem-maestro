import json
import urllib.error
import urllib.request

import pytest

from maestro.dashboard import GUARD_HEADER, Dashboard
from maestro.model import BlockReason, ProblemSeed, RunStage, RunStatus
from maestro.store import Store

from tests.conftest import SLUGS


@pytest.fixture
def dash(tmp_path):
    store = Store(tmp_path / "m.db")
    run_id = store.create_run(
        "edu-arrays-20260725", "/tmp/set",
        [ProblemSeed(slug=s, idx=i, title=s.title(), archive=f"{s}.zip")
         for i, s in enumerate(SLUGS, 1)])
    with Dashboard(store, port=0) as d:
        yield d, store, run_id
    store.close()


def get(dash, path):
    d = dash[0]
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{d.port}{path}", timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def post(dash, path, *, guard=True):
    d = dash[0]
    req = urllib.request.Request(f"http://127.0.0.1:{d.port}{path}", method="POST", data=b"")
    if guard:
        req.add_header(GUARD_HEADER, "1")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --------------------------------------------------------------------- reads


def test_the_page_is_served(dash):
    d = dash[0]
    with urllib.request.urlopen(f"http://127.0.0.1:{d.port}/", timeout=5) as r:
        body = r.read().decode()
    assert r.status == 200 and "<title>Maestro</title>" in body


def test_runs_are_listed_with_their_counts(dash):
    _, store, run_id = dash
    status, body = get(dash, "/api/runs")
    assert status == 200
    run = body["runs"][0]
    assert run["id"] == run_id
    assert run["problems"] == 2
    assert run["by_status"] == {"pending": 2}


def test_a_blocked_run_shows_why(dash):
    _, store, run_id = dash
    store.block(run_id, BlockReason.SESSION_EXPIRED, "log in on the host")
    _, body = get(dash, "/api/runs")
    assert body["runs"][0]["status"] == "blocked"
    assert body["runs"][0]["block_reason"] == "session_expired"


def test_detail_carries_the_problems(dash):
    _, store, run_id = dash
    status, body = get(dash, f"/api/runs/{run_id}")
    assert status == 200
    assert [p["slug"] for p in body["run"]["problems"]] == SLUGS


def test_events_are_cursor_tailed(dash):
    """The tail is what makes a 3s poll cheap instead of a full re-read."""
    _, store, run_id = dash
    store.log(run_id, "info", "one")
    store.log(run_id, "warn", "two", slug=SLUGS[0])

    _, first = get(dash, f"/api/runs/{run_id}/events")
    assert [e["message"] for e in first["events"]] == ["one", "two"]

    _, again = get(dash, f"/api/runs/{run_id}/events?after={first['cursor']}")
    assert again["events"] == []
    assert again["cursor"] == first["cursor"]

    store.log(run_id, "error", "three")
    _, third = get(dash, f"/api/runs/{run_id}/events?after={first['cursor']}")
    assert [e["message"] for e in third["events"]] == ["three"]


def test_an_unknown_run_is_404(dash):
    assert get(dash, "/api/runs/999")[0] == 404


def test_a_bad_run_id_is_400(dash):
    assert get(dash, "/api/runs/not-a-number")[0] == 400


def test_an_unknown_path_is_404(dash):
    assert get(dash, "/api/nope")[0] == 404


# ------------------------------------------------------------------ actions


def test_approve_opens_the_gate_for_one_run(dash):
    _, store, run_id = dash
    status, body = post(dash, f"/api/runs/{run_id}/approve")
    assert status == 200 and body["run"]["approved"] is True
    assert store.get_run(run_id).approved is True


def test_approving_a_blocked_run_also_resumes_it(dash):
    """Otherwise the operator has said what they want and nothing happens."""
    _, store, run_id = dash
    store.block(run_id, BlockReason.AWAITING_APPROVAL, "needs approval")
    post(dash, f"/api/runs/{run_id}/approve")
    run = store.get_run(run_id)
    assert run.status is RunStatus.RUNNING
    assert run.block_reason is None
    assert run.approved is True


def test_resume_clears_a_failed_run(dash):
    _, store, run_id = dash
    store.set_run(run_id, status=RunStatus.FAILED, error="browser died")
    post(dash, f"/api/runs/{run_id}/resume")
    assert store.get_run(run_id).status is RunStatus.RUNNING


def test_resume_does_not_touch_a_running_run(dash):
    _, store, run_id = dash
    store.set_run(run_id, stage=RunStage.POLYGON, status=RunStatus.RUNNING)
    post(dash, f"/api/runs/{run_id}/resume")
    assert store.get_run(run_id).status is RunStatus.RUNNING
    assert not [e for e in store.events(run_id) if "resumed" in e["message"]]


def test_both_actions_are_recorded_in_the_log(dash):
    """A state a human changed must be as visible as one the pipeline changed."""
    _, store, run_id = dash
    store.set_run(run_id, status=RunStatus.FAILED, error="x")
    post(dash, f"/api/runs/{run_id}/resume")
    post(dash, f"/api/runs/{run_id}/approve")
    messages = [e["message"] for e in store.events(run_id)]
    assert any("resumed by the operator" in m for m in messages)
    assert any("approved by the operator" in m for m in messages)


def test_an_unknown_action_is_404(dash):
    _, store, run_id = dash
    assert post(dash, f"/api/runs/{run_id}/delete")[0] == 404


def test_an_action_on_a_missing_run_is_404(dash):
    assert post(dash, "/api/runs/999/approve")[0] == 404


# ---------------------------------------------------------------- csrf guard


def test_a_post_without_the_guard_header_is_refused(dash):
    """A cross-origin form POST cannot set it, so this closes that hole."""
    _, store, run_id = dash
    status, body = post(dash, f"/api/runs/{run_id}/approve", guard=False)
    assert status == 403 and GUARD_HEADER in body["error"]
    assert store.get_run(run_id).approved is False


def test_reads_do_not_need_the_guard(dash):
    assert get(dash, "/api/runs")[0] == 200


# ----------------------------------------------------------------- binding


def test_it_binds_to_localhost_by_default(tmp_path):
    """It has no authentication of its own; the VPN is the access control."""
    store = Store(tmp_path / "m.db")
    with Dashboard(store, port=0) as d:
        assert d._server.server_address[0] == "127.0.0.1"
    store.close()
