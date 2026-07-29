import json

import pytest

from maestro.__main__ import (DEFAULTS, build, check_paths, check_warnings,
                              load_config, main)
from maestro.model import BlockReason, ProblemSeed
from maestro.store import Store

from tests.conftest import SLUGS


def _scraper(tmp_path, name="scraper", *, capable=True):
    """A stub checkout. `capable=False` mimics one predating Phase 2.

    The declarations are written the way the real tools write them — the
    `add_parser(` call split across lines — because that shape is exactly what a
    naive substring check gets wrong, and a stub that avoided it would let the
    check pass here while failing against the real repo.
    """
    tools = {
        "contest_scraper.py": ['sub.add_parser(\n        "session", help="…")'],
        "problem_uploader.py": ['sub.add_parser("upload")', 'pu.add_argument("--only")',
                                'pu.add_argument("--json", action="store_true")'],
        "problem_scraper.py": ['sub.add_parser("problems")', 'ps.add_argument("--from-catalog")'],
        "batch.py": ['sub.add_parser("run")', 'pr.add_argument("--tags-mode")',
                     'pr.add_argument("--skip")', 'pr.add_argument("--json")', '{"key": "limits"}'],
        "report.py": ['sub.add_parser(\n        "audit")', 'pa.add_argument("--char")'],
    }
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    for tool, decls in tools.items():
        repo.joinpath(tool).write_text("\n".join(decls) if capable else "", encoding="utf-8")
    return repo


@pytest.fixture
def cfg(tmp_path):
    repo = _scraper(tmp_path)
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


# ------------------------------------------------------- outdated checkouts


def test_a_checkout_predating_phase_2_is_named(tmp_path):
    """The real failure: `session` landed in priority 4, and an older checkout
    rejects it mid-run with argparse's own error rather than at startup."""
    from maestro.__main__ import check_capabilities
    problems = check_capabilities(_cfg(tmp_path, scraper_repo=str(
        _scraper(tmp_path, "old", capable=False))))
    joined = "\n".join(problems)
    assert "`session` subcommand" in joined and "priority 4" in joined
    assert "older than the contract" in joined
    assert any("git -C" in p and "pull" in p for p in problems)


def test_a_current_checkout_has_no_capability_problems(tmp_path):
    from maestro.__main__ import check_capabilities
    assert check_capabilities(_cfg(tmp_path)) == []


def test_check_fails_on_an_outdated_checkout(tmp_path, capsys):
    path = tmp_path / "c.json"
    path.write_text(json.dumps(_cfg(tmp_path, scraper_repo=str(
        _scraper(tmp_path, "old", capable=False)))), encoding="utf-8")
    assert main(["--config", str(path), "check"]) == 1
    assert "older than the contract" in capsys.readouterr().err


def test_a_missing_tool_is_not_also_a_capability_complaint(tmp_path):
    """One cause, one message — check_paths already reports the missing file."""
    from maestro.__main__ import check_capabilities
    assert check_capabilities(_cfg(tmp_path, scraper_repo=str(tmp_path / "nowhere"))) == []


def test_run_refuses_to_start_against_an_outdated_checkout(tmp_path):
    """`run` goes through build(), so the check has to be there and not only in `check`."""
    from maestro.__main__ import build
    with pytest.raises(SystemExit, match="older than the contract"):
        build(_cfg(tmp_path, scraper_repo=str(_scraper(tmp_path, "old", capable=False))))


def test_run_does_not_replay_a_run_s_whole_history_on_every_start(cfg, capsys):
    """A durable log replayed from zero reprints every historical line at each
    start — so a failure diagnosed and fixed weeks ago reappears looking current,
    and gets debugged again. That is what happened with a stale session error.
    """
    loaded = load_config(cfg)
    with Store(loaded["db"]) as store:
        run_id = store.create_run("old", "/tmp/x",
                                  [ProblemSeed(slug=SLUGS[0], idx=1, title="T", archive="a.zip")])
        store.log(run_id, "error", "session check failed 3 time(s) LONG AGO")

    assert main(["--config", str(cfg), "run", "--max-ticks", "1"]) == 0
    err = capsys.readouterr().err
    assert "LONG AGO" not in err, "history was replayed as if it were happening now"
    assert "earlier event(s) not shown" in err, "and the omission must be stated"


def test_run_echoes_events_logged_after_it_started(cfg, capsys):
    """The cursor starts at the tail — it must not skip everything forever."""
    loaded = load_config(cfg)
    with Store(loaded["db"]) as store:
        run_id = store.create_run("live", "/tmp/x",
                                  [ProblemSeed(slug=SLUGS[0], idx=1, title="T", archive="a.zip")])
        store.log(run_id, "info", "before the start")

    sched, store = build(loaded, check=False)
    try:
        cursors: dict[int, int] = {}
        import maestro.__main__ as m

        seen: list[str] = []
        # The same seed-then-follow logic `cmd_run` uses, driven directly so the
        # second pass has something new to find.
        def tail():
            if run_id not in cursors:
                last = store.last_event(run_id)
                cursors[run_id] = last["id"] if last else 0
                return
            for r in store.events(run_id, after_id=cursors[run_id]):
                cursors[run_id] = r["id"]
                seen.append(r["message"])

        tail()                                            # seeds past the history
        store.log(run_id, "info", "NEW LINE AFTER START")
        tail()
        assert seen == ["NEW LINE AFTER START"]
    finally:
        store.close()


def test_an_empty_divisions_config_is_a_warning_not_a_refusal(cfg):
    """A batch with no divisions is ordinary; it must still start.

    But the division step vanishes from the chore plan entirely when unset, with
    nothing anywhere saying a step was dropped — so it has to be said here.
    """
    loaded = load_config(cfg)
    assert loaded["divisions"] == ""
    assert check_paths(loaded) == [], "an empty divisions must not block startup"
    assert any("divisions" in w for w in check_warnings(loaded))
    assert main(["--config", str(cfg), "run", "--max-ticks", "1"]) == 0
