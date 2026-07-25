import json

import pytest

from maestro.__main__ import DEFAULTS, build, load_config, main
from maestro.model import BlockReason, ProblemSeed
from maestro.store import Store

from tests.conftest import SLUGS


@pytest.fixture
def cfg(tmp_path):
    from maestro.__main__ import REQUIRED_TOOLS
    repo = tmp_path / "scraper"
    repo.mkdir()
    for tool in REQUIRED_TOOLS:
        (repo / tool).write_text("", encoding="utf-8")
    state = tmp_path / "session_state.json"
    state.write_text("{}", encoding="utf-8")

    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "db": str(tmp_path / "m.db"),
        "work_dir": str(tmp_path / "work"),
        "scraper_repo": str(repo),
        "scraper_state": str(state),
        "dashboard_port": 0,
    }), encoding="utf-8")
    return path


def test_defaults_fill_in(cfg):
    loaded = load_config(cfg)
    assert loaded["middleman_url"] == DEFAULTS["middleman_url"]
    assert loaded["apply"] is False


def test_an_unknown_key_is_refused_not_ignored(tmp_path):
    """A typo that silently kept the default is how `apply` ends up off."""
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"scraper_repo": "/x", "aply": True}), encoding="utf-8")
    with pytest.raises(SystemExit, match="aply"):
        load_config(path)


def test_writes_are_off_unless_asked_for(cfg):
    scheduler, store = build(load_config(cfg))
    try:
        assert scheduler.electicode.apply is False
    finally:
        store.close()


def test_a_missing_scraper_repo_is_refused(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="scraper_repo"):
        build(load_config(path))


def test_ingest_gets_the_middleman_parser(cfg):
    """So a set is checked against what will actually import, not just locally."""
    scheduler, store = build(load_config(cfg))
    try:
        assert scheduler.parser is not None
    finally:
        store.close()


def test_init_writes_a_config_and_refuses_to_clobber(tmp_path, capsys):
    path = tmp_path / "new.json"
    assert main(["--config", str(path), "init"]) == 0
    assert set(json.loads(path.read_text())) == set(DEFAULTS)
    with pytest.raises(SystemExit, match="not overwriting"):
        main(["--config", str(path), "init"])


def test_run_stops_after_max_ticks(cfg):
    assert main(["--config", str(cfg), "run", "--max-ticks", "1"]) == 0


def test_status_exits_non_zero_when_a_run_wants_a_human(cfg, capsys):
    loaded = load_config(cfg)
    with Store(loaded["db"]) as store:
        run_id = store.create_run("s", "/tmp/x",
                                  [ProblemSeed(slug=SLUGS[0], idx=1, title="T", archive="a.zip")])

    assert main(["--config", str(cfg), "status"]) == 0
    with Store(loaded["db"]) as store:
        store.block(run_id, BlockReason.SESSION_EXPIRED, "log in")
    assert main(["--config", str(cfg), "status"]) == 1

    out = capsys.readouterr().out
    assert "session_expired" in out and "[unapproved]" in out


def test_status_on_an_empty_store_is_clean(cfg, capsys):
    assert main(["--config", str(cfg), "status"]) == 0
    assert "no runs" in capsys.readouterr().out


# --------------------------------------------------------------- path checks


def _scraper(tmp_path, name="scraper"):
    from maestro.__main__ import REQUIRED_TOOLS
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    for tool in REQUIRED_TOOLS:
        (repo / tool).write_text("", encoding="utf-8")
    return repo


def _cfg(tmp_path, **over):
    from maestro.__main__ import DEFAULTS
    state = tmp_path / "session_state.json"
    state.write_text("{}", encoding="utf-8")
    return {**DEFAULTS, "scraper_repo": str(_scraper(tmp_path)),
            "scraper_state": str(state), "db": str(tmp_path / "m.db"), **over}


def test_a_good_config_has_no_problems(tmp_path):
    from maestro.__main__ import check_paths
    assert check_paths(_cfg(tmp_path)) == []


def test_pointing_at_the_output_folder_suggests_the_parent(tmp_path):
    """The actual mistake: `output/` is where the Scraper writes, not where it lives."""
    from maestro.__main__ import check_paths
    repo = _scraper(tmp_path)
    (repo / "output").mkdir()
    problems = check_paths(_cfg(tmp_path, scraper_repo=str(repo / "output")))
    assert len(problems) == 1
    assert "problem_uploader.py" in problems[0]
    assert f"Did you mean its parent? {repo}" in problems[0]


def test_a_wrong_repo_without_a_near_miss_just_lists_what_is_missing(tmp_path):
    from maestro.__main__ import check_paths
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    problems = check_paths(_cfg(tmp_path, scraper_repo=str(elsewhere)))
    assert "Did you mean" not in problems[0]
    assert "batch.py" in problems[0]


def test_a_missing_session_file_is_named(tmp_path):
    from maestro.__main__ import check_paths
    problems = check_paths(_cfg(tmp_path, scraper_state=str(tmp_path / "nope.json")))
    assert any("scraper_state" in p and "login" in p for p in problems)


def test_a_watch_dir_that_is_not_a_directory_is_named(tmp_path):
    """Otherwise the sweep silently finds nothing and reports nothing."""
    from maestro.__main__ import check_paths
    problems = check_paths(_cfg(tmp_path, watch_dir=str(tmp_path / "missing")))
    assert any("watch_dir" in p for p in problems)


def test_build_refuses_a_bad_config(tmp_path):
    from maestro.__main__ import build
    with pytest.raises(SystemExit, match="config problems"):
        build(_cfg(tmp_path, scraper_repo=str(tmp_path / "nowhere")))


def test_check_reports_the_sets_it_can_see(tmp_path, capsys):
    watch = tmp_path / "watch"
    (watch / "edu-arrays-20260725").mkdir(parents=True)
    path = tmp_path / "c.json"
    path.write_text(json.dumps(_cfg(tmp_path, watch_dir=str(watch))), encoding="utf-8")

    assert main(["--config", str(path), "check"]) == 0
    err = capsys.readouterr().err
    assert "config OK" in err and "preview only" in err
    assert "1 set folder(s): edu-arrays-20260725" in err


def test_check_exits_non_zero_on_a_bad_path(tmp_path, capsys):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(_cfg(tmp_path, scraper_repo=str(tmp_path / "nope"))),
                    encoding="utf-8")
    assert main(["--config", str(path), "check"]) == 1
