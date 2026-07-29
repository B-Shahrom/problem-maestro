"""Maestro's command lines, fed to the Scraper's own argument parsers.

The third round-trip module, and it exists for the same reason as the other two:
Maestro's risk is concentrated in its *model* of the tools it drives, and a test
that only checks Maestro against itself cannot see that model being wrong.

It was wrong. `_run` put `--state` on every invocation because five of the six
tools take it. `report.py` does not — it is a pure file-to-file transform over an
existing scrape, with no session to load. argparse then consumed the unknown flag
and matched the *path* against the `command` positional:

    report.py: error: argument command: invalid choice:
      'C:\\...\\session_state.json' (choose from 'audit')

which reads exactly like a stale checkout. Stage 8 could never have succeeded,
and no test in this repo could tell, because every one of them checked the argv
Maestro builds against the argv Maestro expects to build.

Skips when no checkout is present. Set `MAESTRO_SCRAPER_REPO` to point at one.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from maestro.scraper import STATEFUL_TOOLS, ScraperClient

REPO = Path(os.environ.get("MAESTRO_SCRAPER_REPO", "/workspace/platform-scraper"))
pytestmark = pytest.mark.skipif(
    not (REPO / "report.py").is_file(),
    reason=f"no Scraper checkout at {REPO}; set MAESTRO_SCRAPER_REPO")

#: Every tool Maestro invokes, and the call it makes on `ScraperClient`.
CALLS = {
    "contest_scraper.py": lambda c: c.session(),
    "problem_uploader.py": lambda c: c.upload("/f", "/o", apply=True, only=["a-one"]),
    "problem_scraper.py": lambda c: c.scrape("/o", from_catalog=True),
    "report.py": lambda c: c.audit("/s.json", "/c.md", "/o.json", divisions="Electi"),
    "batch.py": lambda c: c.chores("/c.md", tags_mode="reset", apply=True,
                                   divisions="Electi", targets="ru", skip="fixmdx"),
}


def _parser(tool: str):
    """The tool's real `build_parser()`, imported without running it.

    The checkout goes on `sys.path` first: the tools import siblings (`paths`,
    `problem_scraper`), and without it every case here skips on a
    ModuleNotFoundError — a test that never runs while reporting as fine, which
    is the exact shape of bug it was written to catch.
    """
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location(f"_sc_{tool[:-3]}", REPO / tool)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as e:
        # Only a *third-party* dependency earns a skip — playwright and friends
        # are not this repo's to install. A sibling that failed to resolve means
        # the path setup above is wrong, and must fail loudly rather than skip.
        if (e.name or "") in {t[:-3] for t in CALLS} or (REPO / f"{e.name}.py").is_file():
            raise
        pytest.skip(f"{tool} needs {e.name}, which is not installed here")
    if not hasattr(module, "build_parser"):
        pytest.skip(f"{tool} has no build_parser()")
    return module.build_parser()


def _argv_for(tool: str) -> list[str]:
    """The arguments Maestro passes, minus the interpreter and the script path."""
    recorded: list[list[str]] = []

    def runner(argv, timeout, progress=None):
        recorded.append(argv)
        return 0, "", ""

    CALLS[tool](ScraperClient(REPO, REPO / "session_state.json", runner=runner))
    assert recorded, f"no invocation recorded for {tool}"
    return recorded[0][2:]


@pytest.mark.parametrize("tool", sorted(CALLS))
def test_the_real_parser_accepts_what_maestro_sends(tool, capsys):
    """The whole point. Every flag, against the parser that will judge it.

    `parse_args` calls `sys.exit(2)` on a bad line, so a rejection surfaces as
    SystemExit with argparse's own message — which is precisely the failure the
    operator saw at stage 8.
    """
    parser = _parser(tool)
    argv = _argv_for(tool)
    try:
        parser.parse_args(argv)
    except SystemExit:
        err = capsys.readouterr().err.strip().splitlines()
        pytest.fail(f"{tool} rejects Maestro's command line:\n"
                    f"    argv: {' '.join(argv)}\n"
                    f"    {err[-1] if err else '(no message)'}")


@pytest.mark.parametrize("tool", sorted(CALLS))
def test_state_is_sent_to_exactly_the_tools_that_take_it(tool):
    """`STATEFUL_TOOLS` is Maestro's belief; the parser is the fact."""
    parser = _parser(tool)
    takes_state = any("--state" in (a.option_strings or []) for a in parser._actions)
    assert takes_state == (tool in STATEFUL_TOOLS), (
        f"{tool} {'takes' if takes_state else 'does not take'} --state, but Maestro "
        f"{'sends' if tool in STATEFUL_TOOLS else 'omits'} it")
    assert ("--state" in _argv_for(tool)) == takes_state


def test_the_catalog_shortcut_is_really_available():
    """Stage 6.5 now depends on it, so its absence must fail here, not live."""
    parser = _parser("problem_scraper.py")
    argv = _argv_for("problem_scraper.py")
    assert "--from-catalog" in argv
    parser.parse_args(argv)  # raises SystemExit if the checkout predates T2


# --------------------------------------------------------------- stage 8

"""Stage 8 has never once succeeded live — the `--state` bug stopped it before
it ever ran. So it is exercised here against the real `report.py`, on exactly the
files Maestro writes, rather than waiting for the next live run to find whatever
is behind it."""

import json
import subprocess

from maestro import characteristics as char


def _catalog(rows: list[dict], source: str = "paged") -> dict:
    """A scrape file in `problem_scraper --format json`'s shape."""
    return {"source": source, "count": len(rows), "expected_total": len(rows),
            "problems": rows}


def _run_audit(tmp_path, rows, char_md, *, divisions="", source="paged"):
    """Invoke the real report.py the way Maestro does, and read what it wrote."""
    scrape = tmp_path / "catalog-after.json"
    scrape.write_text(json.dumps(_catalog(rows, source)), encoding="utf-8")
    chars = tmp_path / "characteristics-audited.md"
    chars.write_text(char_md, encoding="utf-8")
    out = tmp_path / "audit.json"

    argv = [sys.executable, str(REPO / "report.py"), "audit",
            "--input", str(scrape), "--char", str(chars),
            "--format", "json", "--output", str(out)]
    if divisions:
        argv += ["--divisions", divisions]
    p = subprocess.run(argv, capture_output=True, text=True, cwd=REPO, timeout=120)
    data = json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
    return p.returncode, data, p.stderr


def _authored(tmp_path):
    """A characteristics file rendered by Maestro, and the scrape that matches it."""
    from tests.conftest import CHAR

    parsed = char.parse(CHAR)
    md = char.render(char.subset(parsed, [r.slug for r in parsed.rows]))
    rows = [{"s3_id": r.slug, "name": r.title, "difficulty": r.group.title(),
             "category": ", ".join(t.strip() for t in parsed.tags[i].split(",")),
             "division_access": "Electi", "submissions": "0"}
            for i, r in enumerate(parsed.rows)]
    return md, rows


def test_stage_8_passes_on_a_batch_that_landed_correctly(tmp_path):
    """The path no live run has reached. If this is wrong, so is the pipeline's end."""
    md, rows = _authored(tmp_path)
    rc, data, err = _run_audit(tmp_path, rows, md, divisions="Electi")
    assert rc == 0, f"a correct batch was audited as wrong:\n{json.dumps(data, indent=2)}\n{err}"
    assert data["issues"] == {k: [] for k in data["issues"]}
    assert "division" in data["checks_run"], "the division check must actually have run"


def test_stage_8_catches_a_difficulty_that_did_not_land(tmp_path):
    md, rows = _authored(tmp_path)
    rows[0]["difficulty"] = "Hard"
    rc, data, _ = _run_audit(tmp_path, rows, md)
    assert rc == 1
    assert data["issues"]["difficulty_mismatch"][0]["s3_id"] == rows[0]["s3_id"]


def test_stage_8_catches_a_tag_that_did_not_land(tmp_path):
    md, rows = _authored(tmp_path)
    rows[0]["category"] = "academy exam"
    rc, data, _ = _run_audit(tmp_path, rows, md)
    assert rc == 1
    assert data["issues"]["tags_missing"]


def test_stage_8_catches_a_missing_division_on_a_paged_scrape(tmp_path):
    """All-empty division access on a PAGED scrape is what a failed division step
    looks like. It must fail loudly, not be explained away."""
    md, rows = _authored(tmp_path)
    for r in rows:
        r["division_access"] = ""
    rc, data, _ = _run_audit(tmp_path, rows, md, divisions="Electi")
    assert rc == 1
    assert data["issues"]["division_missing"]
    assert data["skipped"] == []


def test_a_catalog_sourced_audit_reports_the_division_check_as_not_run(tmp_path):
    """Exit 0 here does NOT mean the divisions landed. Maestro must read `skipped`."""
    md, rows = _authored(tmp_path)
    for r in rows:
        r.pop("division_access")
    rc, data, err = _run_audit(tmp_path, rows, md, divisions="Electi", source="catalog")
    assert rc == 0, "the tool passes — which is exactly why the skip must be read"
    assert data["skipped"] == ["division"]
    assert "division" not in data["checks_run"]


def test_maestro_offers_exactly_the_divisions_the_scraper_knows():
    """A name in the checklist that `division set` rejects is a chore chain that
    dies at its last step — after fixmdx and metadata have run and been paid for.

    Pinned against the Scraper's own constant rather than a copy of it, because
    the checklist is only safe while the two lists are the same list.
    """
    from maestro import divisions as div

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import division_access

    assert list(div.DIVISIONS) == list(division_access.DIVISIONS)


def test_maestro_canonicalises_names_the_same_way_the_scraper_does():
    """Case-folding that disagreed would send a name the tool then rejects."""
    from maestro import divisions as div

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import division_access

    for probe in ("electi", "ELECTI", " Division A+ ", "tier 3", "Divison A"):
        mine, my_unknown = div.normalise(probe)
        theirs, their_unknown = division_access._normalize_divisions([probe])
        assert mine == theirs, probe
        assert bool(my_unknown) == bool(their_unknown), probe


def test_maestro_knows_every_stage_the_chore_runner_can_plan():
    """`_STAGE_KEYS` is Maestro's model of `batch run`'s plan; `STAGE_KEYS` is the
    plan. A key Maestro cannot name is never skipped on a resume — safe, but it
    means a chore chain restarts from the top forever, and silently.

    Pinned rather than copied: `limits` arrived with T7 and this is what says so.
    """
    from maestro.electicode_lane import _REPLAYABLE, _STAGE_KEYS

    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    import batch

    theirs = set(batch.STAGE_KEYS)
    assert set(_STAGE_KEYS.values()) == theirs
    assert set(_REPLAYABLE) == theirs, (
        "every stage needs a replay verdict — an unrecorded one defaults to "
        "not-retryable, which stops a run that could have continued")


def test_the_limits_stage_is_really_declarative():
    """Maestro marks it replayable, which is only safe if a repeat converges."""
    from maestro.electicode_lane import _REPLAYABLE

    assert _REPLAYABLE["limits"] is True
    source = (REPO / "problem_editor.py").read_text(encoding="utf-8")
    assert "--time-limit" in source and "--memory-limit" in source
