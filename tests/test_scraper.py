import pytest

from pathlib import Path

from maestro.scraper import (Detected, Outcome, ScraperClient, _default_runner,
                             catalog, detected, events, interpret)


def test_general_exit_codes():
    assert interpret(0)[0] is Outcome.OK
    assert interpret(1)[0] is Outcome.HALT
    assert interpret(2)[0] is Outcome.RETRY


def test_session_reads_the_same_codes_differently():
    """`1` is a caller error everywhere else; for `session` it needs a human."""
    assert interpret(1)[0] is Outcome.HALT
    assert interpret(1, session=True)[0] is Outcome.BLOCKED
    assert interpret(3, session=True)[0] is Outcome.BLOCKED
    assert interpret(2, session=True)[0] is Outcome.RETRY


def test_an_undocumented_code_halts_rather_than_retrying():
    outcome, reason = interpret(7)
    assert outcome is Outcome.HALT
    assert "7" in reason


# --------------------------------------------------------------------- argv


def _recorder(rc=0, stdout="", stderr=""):
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        return rc, stdout, stderr

    return run, calls


def test_state_precedes_the_subcommand(tmp_path):
    """Every Scraper tool declares --state on the *parent* parser."""
    run, calls = _recorder()
    ScraperClient(tmp_path / "repo", tmp_path / "s.json", runner=run).scrape(tmp_path / "c.json")
    argv = calls[0]
    assert argv[argv.index("--state") + 2] == "problems"


def test_apply_is_never_inferred(tmp_path):
    run, calls = _recorder()
    c = ScraperClient(tmp_path / "repo", tmp_path / "s.json", runner=run)
    c.upload(tmp_path, tmp_path / "o.json")
    c.chores(tmp_path / "char.md", tags_mode="reset")
    assert not any("--apply" in argv for argv in calls)
    c.upload(tmp_path, tmp_path / "o.json", apply=True)
    assert "--apply" in calls[-1]


def test_output_paths_are_absolute(tmp_path, monkeypatch):
    """The Scraper redirects bare filenames into its own output/ folder."""
    run, calls = _recorder()
    monkeypatch.chdir(tmp_path)
    ScraperClient("repo", "s.json", runner=run).scrape("catalog.json")
    out = calls[0][calls[0].index("--output") + 1]
    assert Path(out).is_absolute()


def test_scrape_is_strict_by_default(tmp_path):
    """A short scrape must fail, not read as every slug having gone missing."""
    run, calls = _recorder()
    ScraperClient(tmp_path, tmp_path / "s.json", runner=run).scrape(tmp_path / "c.json")
    assert "--strict" in calls[0]


def test_a_timeout_is_operational():
    """A hung browser must map to RETRY, not escape as an exception."""
    import sys

    rc, _, err = _default_runner([sys.executable, "-c", "import time; time.sleep(30)"], 0.5)
    assert interpret(rc)[0] is Outcome.RETRY
    assert "timed out" in err


# ------------------------------------------------------------------ parsing


def test_catalog_accepts_both_shapes():
    rows = [{"s3_id": "a"}, {"s3_id": "b"}]
    assert catalog(rows) == rows
    assert catalog({"problems": rows}) == rows
    assert catalog(None) == []


PREVIEW_NDJSON = """\
{"event":"start","tool":"upload","op":"upload","total":3,"apply":false}
{"event":"item","tool":"upload","op":"upload","id":"a-slug","ok":true,"status":"new","exists":false,"selected":true}
{"event":"item","tool":"upload","op":"upload","id":"b-slug","ok":true,"status":"exists","exists":true,"overwrite_name":"Largest Gap","selected":true}
{"event":"item","tool":"upload","op":"upload","id":"c-slug","ok":true,"status":"new","exists":false,"selected":false}
{"event":"summary","tool":"upload","op":"upload","total":3,"ok":3,"failed":0,"exit":0,"preview":true}
"""


def test_detected_reads_the_json_events():
    """The preview is the only run that reports `exists`, and only stdout has it."""
    assert detected(PREVIEW_NDJSON) == [
        Detected("a-slug", False, "", True),
        Detected("b-slug", True, "Largest Gap", True),
        Detected("c-slug", False, "", False),
    ]


def test_detected_falls_back_to_the_output_file():
    """The file is written on the apply pass, where the early returns don't fire."""
    data = {"detected": {"problems": [
        {"id": "a-slug", "exists": True, "overwrite_name": "A Title", "selected": True},
    ]}}
    assert detected("", data) == [Detected("a-slug", True, "A Title", True)]


def test_detected_prefers_the_events_over_the_file():
    data = {"detected": {"problems": [{"id": "stale", "exists": False}]}}
    assert [d.id for d in detected(PREVIEW_NDJSON, data)] == ["a-slug", "b-slug", "c-slug"]


def test_detected_is_empty_without_either_source():
    """An empty read must stay empty — the lane refuses to submit on it."""
    assert detected("", None) == []
    assert detected("Folder: /x\n    a-slug\n") == []


def test_events_drop_unparseable_lines_rather_than_raising():
    stream = '{"event":"start","total":1}\nnot json\n{"event":"item","id":"a"}\n'
    assert [e.get("event") for e in events(stream)] == ["start", "item"]


# ------------------------------------------------------- outdated checkouts


USAGE_ERROR = (
    "usage: contest_scraper.py [-h] [--state STATE] {login,electicode,contest,list,scrape,dump} ...\n"
    "contest_scraper.py: error: argument command: invalid choice: 'session' "
    "(choose from 'login', 'electicode', 'contest', 'list', 'scrape', 'dump')\n"
)


def test_an_argparse_rejection_halts_instead_of_retrying(tmp_path):
    """argparse also exits 2 — retrying an unknown subcommand is pure waste."""
    def run(argv, timeout):
        return 2, "", USAGE_ERROR

    r = ScraperClient(tmp_path, tmp_path / "s.json", runner=run).session()
    assert r.outcome is Outcome.HALT
    assert "invalid choice: 'session'" in r.reason
    assert "older than the contract" in r.reason


def test_a_genuine_operational_failure_still_retries(tmp_path):
    def run(argv, timeout):
        return 2, "", "Timed out waiting for the platform to detect problems.\n"

    r = ScraperClient(tmp_path, tmp_path / "s.json", runner=run).scrape(tmp_path / "c.json")
    assert r.outcome is Outcome.RETRY


@pytest.mark.parametrize("line", [
    "tool.py: error: unrecognized arguments: --json",
    "tool.py: error: argument --skip: expected one argument",
    "tool.py: error: the following arguments are required: --char",
])
def test_every_argparse_shape_is_recognised(tmp_path, line):
    def run(argv, timeout):
        return 2, "", f"usage: tool.py …\n{line}\n"

    assert ScraperClient(tmp_path, tmp_path / "s.json",
                         runner=run).scrape(tmp_path / "c.json").outcome is Outcome.HALT
