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
from maestro import settings as cfg
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
        s.set_setting(rid, "divisions", "Electi")
    with Store(tmp_path / "m.db") as s:
        assert s.get_run(rid).divisions == "Electi"


def test_choosing_none_is_not_the_same_as_never_choosing(store, run_id):
    store.set_setting(run_id, "divisions", "")
    assert store.get_run(run_id).divisions == ""
    store.set_setting(run_id, "divisions", None)
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
    with urllib.request.urlopen(f"{base}/api/settings", timeout=5) as r:
        served = {f["key"]: f["vocabulary"] for f in json.load(r)["fields"]}
    assert served["divisions"] == list(div.DIVISIONS)
    assert served["targets"] == list(cfg.LANGUAGES)
    assert served["list_url"] is None, "a URL has no vocabulary to offer"


def test_ticking_boxes_sets_the_batch_s_divisions(live):
    store, run_id, base = live
    out = _post(f"{base}/api/runs/{run_id}/settings",
                {"divisions": ["Division A+", "Electi"]})
    assert out["run"]["settings"]["divisions"] == "Electi, Division A+"   # canonical, modal order
    assert store.get_run(run_id).divisions == "Electi, Division A+"


def test_unticking_everything_means_none_not_the_default(live):
    store, run_id, base = live
    _post(f"{base}/api/runs/{run_id}/settings", {"divisions": []})
    assert store.get_run(run_id).divisions == ""


def test_an_explicit_null_restores_the_configured_default(live):
    store, run_id, base = live
    _post(f"{base}/api/runs/{run_id}/settings", {"divisions": ["Electi"]})
    _post(f"{base}/api/runs/{run_id}/settings", {"divisions": None})
    assert store.get_run(run_id).divisions is None


def test_an_unknown_name_is_refused_with_the_valid_list(live):
    store, run_id, base = live
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(f"{base}/api/runs/{run_id}/settings", {"divisions": ["Divison A"]})
    assert e.value.code == 400
    body = json.loads(e.value.read())["error"]
    assert "Divison A" in body and "Division A+" in body
    assert store.get_run(run_id).divisions is None, "nothing may be stored on a rejection"


def test_choosing_after_the_chores_ran_is_recorded_but_says_so(live):
    """It is not a lie to store it — but the grant already happened, and the
    operator has to know this only moves what the audit looks for."""
    store, run_id, base = live
    store.set_run(run_id, stage=RunStage.AUDIT)
    out = _post(f"{base}/api/runs/{run_id}/settings", {"divisions": ["Electi"]})
    assert "chores have already run" in out["note"]
    assert store.get_run(run_id).divisions == "Electi"
    assert out["run"]["settings_locked"] is True


# ------------------------------------------- the other two, same mechanism


def test_targets_is_a_closed_vocabulary_too():
    """`problem_editor translate` rejects an unknown code the same way."""
    values, unknown = cfg.normalise("targets", "RU, tg, klingon")
    assert values == ["ru", "tg"]
    assert unknown == ["klingon"]


def test_a_list_url_is_free_text_because_a_url_has_no_vocabulary():
    values, unknown = cfg.normalise("list_url", " https://www.electicode.com/x/manage ")
    assert values == ["https://www.electicode.com/x/manage"]
    assert unknown == []


def test_targets_render_in_the_vocabulary_s_order_not_the_operator_s():
    """A stable form is what lets the page tell a changed choice from a re-render."""
    assert cfg.render("targets", ["uz", "ru"]) == "ru, uz"


@pytest.mark.parametrize("key,configured,chosen,want", [
    ("targets", "ru,tg,uz", None, "ru,tg,uz"),
    ("targets", "ru,tg,uz", "", ""),
    ("targets", "", "ru", "ru"),
    ("list_url", "https://default", None, "https://default"),
    ("list_url", "https://default", "", ""),
    ("list_url", "", "https://this-batch", "https://this-batch"),
])
def test_all_three_share_one_inheritance_rule(key, configured, chosen, want):
    assert cfg.effective(key, chosen, configured) == want


def test_each_setting_describes_its_own_kind_of_empty(store, run_id):
    assert "not chosen" in cfg.describe("targets", None)
    assert "no statements will be translated" in cfg.describe("targets", "")
    assert "not be added to any list" in cfg.describe("list_url", "")


def test_one_control_saving_does_not_clear_the_others(live):
    """Partial updates: only the keys in the body are touched."""
    store, run_id, base = live
    _post(f"{base}/api/runs/{run_id}/settings", {"divisions": ["Electi"], "targets": ["ru"]})
    _post(f"{base}/api/runs/{run_id}/settings", {"list_url": "https://x/manage"})
    run = store.get_run(run_id)
    assert (run.divisions, run.targets, run.list_url) == ("Electi", "ru", "https://x/manage")


def test_an_unknown_setting_key_is_refused(live):
    """The key reaches a column name, and the caller is an HTTP handler."""
    store, run_id, base = live
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(f"{base}/api/runs/{run_id}/settings", {"approved": True})
    assert e.value.code == 400
    assert "not a per-batch setting" in json.loads(e.value.read())["error"]


def test_the_store_refuses_a_key_that_is_not_a_column(store, run_id):
    with pytest.raises(KeyError):
        store.set_setting(run_id, "set_name; DROP TABLE runs", "x")


def test_an_unknown_language_is_refused_with_the_valid_list(live):
    store, run_id, base = live
    with pytest.raises(urllib.error.HTTPError) as e:
        _post(f"{base}/api/runs/{run_id}/settings", {"targets": ["klingon"]})
    body = json.loads(e.value.read())["error"]
    assert "klingon" in body and "uz" in body
    assert store.get_run(run_id).targets is None


def test_the_lane_sends_each_batch_s_own_choice_to_the_chore_runner(tmp_path):
    """The end of the chain: what actually reaches `batch.py run`."""
    from maestro.electicode_lane import ElectiCodeLane
    from maestro.scraper import ScraperClient

    calls: list[list[str]] = []
    store = Store(tmp_path / "m.db")
    client = ScraperClient(tmp_path, tmp_path / "s.json",
                           runner=lambda a, t, p=None: (calls.append(a), (0, "", ""))[1])
    lane = ElectiCodeLane(store, client, tmp_path / "runs",
                          divisions="Electi", targets="ru,tg,uz", list_url="https://default")
    try:
        run = Run(id=1, set_name="s", set_dir="/x", stage=RunStage.CHORES,
                  status=RunStatus.RUNNING,
                  divisions="Tier 1", targets="", list_url=None)
        assert lane.setting_for(run, "divisions") == "Tier 1"      # overridden
        assert lane.setting_for(run, "targets") == ""              # chosen none
        assert lane.setting_for(run, "list_url") == "https://default"   # inherited
    finally:
        store.close()
