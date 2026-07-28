"""Division access as a per-batch choice, from a closed list.

It was a config value, which made it a property of the installation when it is
really a property of the delivery. It is also what produced a run with no
divisions at all: the default was never changed, and the chore step simply
vanished from the plan with nothing anywhere saying a step had been dropped.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from maestro import divisions as div
from maestro.dashboard import GUARD_HEADER, Dashboard
from maestro.model import ProblemSeed, Run, RunStage, RunStatus
from maestro.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        yield s


@pytest.fixture
def run_id(store):
    return store.create_run("edu-arrays", "/tmp/x",
                            [ProblemSeed(slug="a-one", idx=1, title="T", archive="a.zip")])


# ------------------------------------------------------------- vocabulary


def test_the_nine_names_are_offered_in_the_platform_s_own_order():
    assert div.DIVISIONS == ("Tier 3", "Tier 2", "Tier 1", "Electi",
                             "Division D", "Division C", "Division B",
                             "Division A", "Division A+")


@pytest.mark.parametrize("given,want", [
    ("electi", ["Electi"]),
    ("ELECTI, division a+", ["Electi", "Division A+"]),
    ("  Tier 1  ", ["Tier 1"]),
    ("Electi, Electi", ["Electi"]),
    ("", []),
    (None, []),
])
def test_names_are_matched_case_insensitively_and_de_duplicated(given, want):
    names, unknown = div.normalise(given)
    assert (names, unknown) == (want, [])


def test_an_unknown_name_is_returned_rather_than_dropped():
    """`division set` rejects it with exit 1 — at the END of the chore chain,
    after fixmdx and metadata have already run and been paid for."""
    names, unknown = div.normalise("Electi, Divison A")   # sic
    assert names == ["Electi"]
    assert unknown == ["Divison A"]


def test_the_stored_form_is_canonical_and_in_modal_order():
    names, _ = div.normalise("division a+, electi, tier 3")
    assert div.render(names) == "Tier 3, Electi, Division A+"


def test_never_chosen_and_chosen_none_read_differently():
    """The whole point of the nullable column."""
    assert "default" in div.describe(None)
    assert "no division access" in div.describe("")
    assert div.describe("Electi") == "Electi"


# ------------------------------------------------------------ persistence


def test_a_run_starts_out_inheriting_the_configured_default(store, run_id):
    assert store.get_run(run_id).divisions is None


def test_a_choice_survives_a_reopen(tmp_path):
    with Store(tmp_path / "m.db") as s:
        rid = s.create_run("x", "/tmp/x",
                           [ProblemSeed(slug="a", idx=1, title="T", archive="a.zip")])
        s.set_divisions(rid, "Electi")
    with Store(tmp_path / "m.db") as s:
        assert s.get_run(rid).divisions == "Electi"


def test_choosing_none_is_not_the_same_as_never_choosing(store, run_id):
    store.set_divisions(run_id, "")
    assert store.get_run(run_id).divisions == ""
    store.set_divisions(run_id, None)
    assert store.get_run(run_id).divisions is None


def test_an_older_database_gains_the_column_without_losing_its_runs(tmp_path):
    """The store outlives its schema — that is the point of it being durable."""
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE runs (id INTEGER PRIMARY KEY AUTOINCREMENT, set_name TEXT NOT NULL UNIQUE,
            set_dir TEXT NOT NULL, stage TEXT NOT NULL, status TEXT NOT NULL,
            block_reason TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        INSERT INTO runs (set_name, set_dir, stage, status, created_at, updated_at)
            VALUES ('legacy', '/tmp/x', 'polygon', 'running', 'then', 'then');
    """)
    db.close()

    with Store(path) as s:
        (run,) = s.list_runs()
        assert run.set_name == "legacy"
        assert run.divisions is None, "a pre-existing run must not adopt the config silently"


# ------------------------------------------------------------- the lane


def _lane(tmp_path, configured=""):
    from maestro.electicode_lane import ElectiCodeLane
    from maestro.scraper import ScraperClient

    store = Store(tmp_path / "m.db")
    client = ScraperClient(tmp_path, tmp_path / "s.json",
                           runner=lambda a, t, p=None: (0, "", ""))
    return ElectiCodeLane(store, client, tmp_path / "runs", divisions=configured), store


def _run(divisions):
    return Run(id=1, set_name="s", set_dir="/x", stage=RunStage.CHORES,
               status=RunStatus.RUNNING, divisions=divisions)


@pytest.mark.parametrize("configured,chosen,effective", [
    ("Electi", None, "Electi"),          # never chosen → inherit
    ("Electi", "Tier 1", "Tier 1"),      # chosen → override
    ("Electi", "", ""),                  # chosen none → NOT the default
    ("", "Division A", "Division A"),    # chosen where the install has none
    ("", None, ""),
])
def test_the_run_s_own_choice_beats_the_install_s(tmp_path, configured, chosen, effective):
    lane, store = _lane(tmp_path, configured)
    try:
        assert lane.divisions_for(_run(chosen)) == effective
    finally:
        store.close()


# --------------------------------------------------------- the checklist


@pytest.fixture
def live(store, run_id):
    with Dashboard(store, port=0) as dash:
        yield store, run_id, f"http://127.0.0.1:{dash.port}"


def _post(url, body):
    req = urllib.request.Request(
        url, method="POST", data=json.dumps(body).encode(),
        headers={GUARD_HEADER: "1", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def test_the_page_is_served_the_same_vocabulary_the_server_validates(live):
    """A checklist offering a name the validator rejects is a trap."""
    _, _, base = live
    with urllib.request.urlopen(f"{base}/api/divisions", timeout=5) as r:
        assert json.load(r)["divisions"] == list(div.DIVISIONS)


def test_ticking_boxes_sets_the_batch_s_divisions(live):
    store, run_id, base = live
    out = _post(f"{base}/api/runs/{run_id}/divisions",
                {"divisions": ["Division A+", "Electi"]})
    assert out["run"]["divisions"] == "Electi, Division A+"   # canonical, modal order
    assert store.get_run(run_id).divisions == "Electi, Division A+"


def test_unticking_everything_means_none_not_the_default(live):
    store, run_id, base = live
    _post(f"{base}/api/runs/{run_id}/divisions", {"divisions": []})
    assert store.get_run(run_id).divisions == ""


def test_an_explicit_null_restores_the_configured_default(live):
    store, run_id, base = live
    _post(f"{base}/api/runs/{run_id}/divisions", {"divisions": ["Electi"]})
    _post(f"{base}/api/runs/{run_id}/divisions", {"divisions": None})
    assert store.get_run(run_id).divisions is None


def test_an_unknown_name_is_refused_with_the_valid_list(live):
    store, run_id, base = live
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(f"{base}/api/runs/{run_id}/divisions", {"divisions": ["Divison A"]})
    assert e.value.code == 400
    body = json.loads(e.value.read())["error"]
    assert "Divison A" in body and "Division A+" in body
    assert store.get_run(run_id).divisions is None, "nothing may be stored on a rejection"


def test_choosing_after_the_chores_ran_is_recorded_but_says_so(live):
    """It is not a lie to store it — but the grant already happened, and the
    operator has to know this only moves what the audit looks for."""
    store, run_id, base = live
    store.set_run(run_id, stage=RunStage.AUDIT)
    out = _post(f"{base}/api/runs/{run_id}/divisions", {"divisions": ["Electi"]})
    assert "chores have already run" in out["note"]
    assert store.get_run(run_id).divisions == "Electi"
    assert out["run"]["divisions_locked"] is True
