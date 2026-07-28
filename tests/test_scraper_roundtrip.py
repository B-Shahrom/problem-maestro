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
