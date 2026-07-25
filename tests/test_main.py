import json

import pytest

from maestro.__main__ import DEFAULTS, build, load_config, main
from maestro.model import BlockReason, ProblemSeed
from maestro.store import Store

from tests.conftest import SLUGS


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "db": str(tmp_path / "m.db"),
        "work_dir": str(tmp_path / "work"),
        "scraper_repo": str(tmp_path / "scraper"),
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
