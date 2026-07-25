"""The Platform Scraper's CLIs, as a typed surface.

Every ElectiCode operation is a **subprocess**, not a library call. The Scraper
drives a real browser through Playwright and keeps its authentication in
`session_state.json`; importing it would drag that whole runtime into Maestro's
process and couple the two repos' dependencies. A subprocess boundary also means
a crashed browser kills a child, not the orchestrator.

Two things make that boundary usable rather than merely tolerable:

**Exit codes carry the decision.** The Scraper's codes are uniform across its
mutating tools — `0` success-or-preview, `1` validation, `2` operational — so
`interpret()` maps a return code to what Maestro should *do* without parsing any
human text. The one overload is `contest_scraper session`, which is a diagnosis
rather than an action and reads its codes differently; that is the only place a
caller has to say which tool it ran.

**Nothing mutates without `apply=True`.** Every method that can write takes the
flag explicitly and defaults to preview. `--apply` is never inferred.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

ELECTICODE_BASE = "https://www.electicode.com"

#: `(argv, timeout_seconds) -> (returncode, stdout, stderr)`. Injected so the
#: lane can be tested without a browser, a session, or the Scraper checkout.
Runner = Callable[[list[str], float], tuple[int, str, str]]


class Outcome(StrEnum):
    """What Maestro does about a return code."""

    OK = "ok"
    RETRY = "retry"      # transient: the page didn't load, the browser hung
    HALT = "halt"        # a retry cannot fix it — bad arguments, bad input file
    BLOCKED = "blocked"  # a human must act on the host, then the run resumes


_GENERAL = {
    0: (Outcome.OK, "success or preview"),
    1: (Outcome.HALT, "validation error"),
    2: (Outcome.RETRY, "operational failure"),
}

# `session` overloads the same numbers. It reports on the world rather than
# acting on it, so `1` (no session file) is not a caller mistake — it is the same
# "log in on the host" state as `3`, reached by a different route.
_SESSION = {
    0: (Outcome.OK, "session valid"),
    1: (Outcome.BLOCKED, "no saved session file — log in on the host"),
    2: (Outcome.RETRY, "the admin probe page could not be loaded"),
    3: (Outcome.BLOCKED, "session expired — log in on the host"),
}


def interpret(rc: int, *, session: bool = False) -> tuple[Outcome, str]:
    """Map a Scraper exit code to an action.

    An unknown code halts rather than retrying: the tools document three values
    (four for `session`), so a fourth means Maestro is running against a version
    it was not built for, and guessing would be worse than stopping.
    """
    table = _SESSION if session else _GENERAL
    return table.get(rc, (Outcome.HALT, f"unexpected exit code {rc}"))


#: argparse's own rejections. It exits `2`, the same code the Scraper uses for an
#: operational failure, so the text is the only thing that tells them apart.
_USAGE_MARKERS = ("invalid choice:", "unrecognized arguments:", "error: argument",
                  "the following arguments are required")


def _usage_error(stderr: str) -> str | None:
    """The argparse complaint in `stderr`, if that is what this was.

    Nearly always means the Scraper checkout predates a capability Maestro was
    built against, so the message says so — the alternative is an operator
    reading "operational failure" about a tool that is working exactly as its
    version intends.
    """
    for line in reversed((stderr or "").splitlines()):
        if any(m in line for m in _USAGE_MARKERS):
            return (f"the Scraper rejected the command line — {line.strip()}. "
                    "This checkout is probably older than the contract Maestro "
                    "was built against; update it and re-run `maestro check`")
    return None


@dataclass(slots=True)
class Result:
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    outcome: Outcome
    reason: str
    data: Any = None
    """Parsed payload, when the call had one to give (session JSON, audit JSON)."""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.OK

    @property
    def tool(self) -> str:
        return Path(self.argv[1]).name if len(self.argv) > 1 else "?"

    def __str__(self) -> str:
        return f"{self.tool} exited {self.rc} ({self.reason})"


@dataclass(slots=True)
class Detected:
    """One row of the upload modal's detection table.

    `exists` is the load-bearing field: it decides whether stage 7 *replaces* this
    problem's categories or *appends* to them. `problem_editor assign --category`
    calls `fill()`, so applying authored tags to a problem that already carried
    curricular ones would silently destroy them.
    """

    id: str
    exists: bool = False
    overwrite_name: str = ""
    selected: bool = True


def _default_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as e:
        # A hung browser is operational, not a bad request: report it as the
        # Scraper's own "operational failure" code so the retry policy applies.
        out = (e.stdout or b"") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 2, out if isinstance(out, str) else out.decode("utf-8", "replace"), \
            f"timed out after {timeout:.0f}s"
    return p.returncode, p.stdout, p.stderr


class ScraperClient:
    """One method per Scraper invocation Maestro needs.

    Paths handed to `--output` are always absolute: the Scraper's `resolve_output`
    redirects bare filenames into its own `output/` folder, which would scatter a
    run's artefacts across the Scraper checkout instead of the run directory.
    """

    def __init__(
        self,
        repo: str | Path,
        state: str | Path,
        *,
        base: str = ELECTICODE_BASE,
        runner: Runner | None = None,
        python: str | None = None,
        timeout: float = 1800.0,
    ) -> None:
        self.repo = Path(repo)
        self.state = Path(state)
        self.base = base
        self._run_argv: Runner = runner or _default_runner
        self.python = python or sys.executable or "python3"
        self.timeout = timeout

    # ------------------------------------------------------------- plumbing

    def _run(self, tool: str, sub: str, *args: str,
             session: bool = False, timeout: float | None = None) -> Result:
        argv = [self.python, str(self.repo / tool), "--state", str(self.state), sub, *args]
        rc, out, err = self._run_argv(argv, timeout or self.timeout)
        outcome, reason = interpret(rc, session=session)
        if outcome is Outcome.RETRY and (usage := _usage_error(err)):
            # argparse also exits 2, which collides with the Scraper's own
            # "operational failure". Retrying an unknown subcommand or flag is
            # pure waste, and worse, it buries the real cause under three
            # identical failures before reporting a timeout-shaped error.
            outcome, reason = Outcome.HALT, usage
        return Result(argv=argv, rc=rc, stdout=out, stderr=err, outcome=outcome, reason=reason)

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # -------------------------------------------------------------- session

    def session(self) -> Result:
        """Non-mutating auth check. Run before anything that needs a browser.

        Cheap relative to what it guards: without it, an expired session surfaces
        as a mid-batch upload failure whose cause is a timed-out selector.
        """
        r = self._run("contest_scraper.py", "session", "--base", self.base, "--json",
                      session=True, timeout=180.0)
        try:
            r.data = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            r.data = None
        return r

    # --------------------------------------------------------------- upload

    def upload(self, folder: str | Path, output: str | Path, *, apply: bool = False,
               only: list[str] | None = None) -> Result:
        """Stage 6. Hand a parent folder to the platform's upload modal.

        Preview (`apply=False`) is what tells Maestro which problems already
        exist, so it always runs first — the answer cannot be derived locally.

        `only` names the rows to tick, unticking the rest, and the tool refuses
        (exit `2`) if any named slug is not among the detected problems. That is a
        second, independent check of the same thing the lane verifies from the
        preview, made by the code that can actually see the modal.
        """
        args = ["--base", self.base, "--folder", str(Path(folder).resolve()),
                "--output", str(Path(output).resolve()), "--json"]
        if only:
            args += ["--only", ",".join(only)]
        if apply:
            args.append("--apply")
        # The tool waits up to its own `--upload-timeout` (600s by default) for the
        # platform to process a submit, so anything tighter would kill a healthy run.
        r = self._run("problem_uploader.py", "upload", *args, timeout=1200.0)
        r.data = detected(r.stdout, self._read_json(Path(output)))
        return r

    # ---------------------------------------------------------------- audit

    def scrape(self, output: str | Path, *, strict: bool = True) -> Result:
        """The catalog read behind both stage 6.5 and stage 8.

        `--strict` on purpose: a paging failure that returns 40 of 2,000 problems
        would otherwise exit 0, and every slug Maestro just uploaded would look
        absent. A short scrape must fail loudly rather than read as a mismatch.
        """
        args = ["--base", self.base, "--format", "json",
                "--output", str(Path(output).resolve())]
        if strict:
            args.append("--strict")
        r = self._run("problem_scraper.py", "problems", *args)
        r.data = catalog(self._read_json(Path(output)))
        return r

    def audit(self, scrape_json: str | Path, char: str | Path, output: str | Path,
              *, divisions: str = "") -> Result:
        """Stage 8. Actual-vs-expected, joined per slug.

        Exits `1` on any gap *or* mismatch, so `OK` here is the run's completion
        signal. Note it is a pure file-to-file transform — no browser, no session.
        """
        args = ["--input", str(Path(scrape_json).resolve()),
                "--char", str(Path(char).resolve()),
                "--format", "json", "--output", str(Path(output).resolve())]
        if divisions:
            args += ["--divisions", divisions]
        r = self._run("report.py", "audit", *args, timeout=300.0)
        r.data = self._read_json(Path(output))
        return r

    # --------------------------------------------------------------- chores

    def chores(self, char: str | Path, *, tags_mode: str, apply: bool = False,
               divisions: str = "", targets: str = "", list_url: str = "",
               fixmdx: str = "subtasks", stop_on_error: bool = True,
               skip: str = "") -> Result:
        """Stage 7. Difficulty, tags, divisions, MDX repair, translation, listing.

        `tags_mode` is a **whole-run** setting on the Scraper's side, which is why
        the lane invokes this once per exists-group rather than once per batch.

        `stop_on_error` defaults on: the steps are ordered, and continuing past a
        failed one produces a partially-chored batch that the audit then reports
        as several unrelated gaps.

        `skip` is a comma list of stage keys already known to have run. It is what
        makes a resumed chore chain safe — several of the stages cannot be
        replayed (see `stage_retryable`), so a retry has to start where the last
        attempt stopped rather than at the top.
        """
        args = ["--base", self.base, "--char", str(Path(char).resolve()),
                "--tags-mode", tags_mode, "--fixmdx", fixmdx, "--json"]
        if divisions:
            args += ["--divisions", divisions]
        if targets:
            args += ["--targets", targets]
        if list_url:
            args += ["--list-url", list_url]
        if skip:
            args += ["--skip", skip]
        if stop_on_error:
            args.append("--stop-on-error")
        if apply:
            args.append("--apply")
        # Six stages, each a page search + modal + save per problem. The toolkit's
        # own sizing note puts a ten-problem batch in the tens of minutes, so this
        # is deliberately generous — a timeout here is read as an operational
        # failure and retried, which is the expensive way to be wrong.
        r = self._run("batch.py", "run", *args, timeout=max(self.timeout, 10800.0))
        r.data = events(r.stdout)
        return r


# --------------------------------------------------------------------- parsing


def catalog(data: Any) -> list[dict]:
    """Normalise a `problem_scraper problems` payload into a row list.

    The tool has emitted both a bare list and a `{problems: [...]}` envelope over
    its life; `report.py` accepts either, so Maestro does too rather than pinning
    a shape that is not part of the agreed contract.
    """
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        rows = data.get("problems")
        if isinstance(rows, list):
            return [r for r in rows if isinstance(r, dict)]
    return []


def events(stdout: str) -> list[dict]:
    """The `--json` NDJSON stream: one object per line on stdout.

    Unparseable lines are dropped rather than raising. The stream is a progress
    report; a tool that manages to corrupt one line has still told Maestro the
    truth through its exit code, which is the authoritative signal.
    """
    out = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def detected(stdout: str, data: Any = None) -> list[Detected]:
    """The upload modal's per-row detection.

    Read from the `--json` `item` events, not from `--output`. The two carry the
    same fields, but the file is written after `cmd_upload` leaves its browser
    context while every early return happens inside it — including the clean
    preview one (`problem_uploader.py:371` vs `:388`). Preview is precisely the
    run Maestro needs this from, so the file is empty exactly when it matters.

    The events do not have that problem: they are emitted as each row is read,
    before any of those returns. `--output` is still accepted as a second source
    for the apply pass, where the file does get written.
    """
    rows = [e for e in events(stdout) if e.get("event") == "item"]
    if rows:
        return [Detected(id=(e.get("id") or "").strip(), exists=bool(e.get("exists")),
                         overwrite_name=(e.get("overwrite_name") or "").strip(),
                         selected=bool(e.get("selected", True)))
                for e in rows]
    if isinstance(data, dict):
        for r in (data.get("detected") or {}).get("problems") or []:
            if isinstance(r, dict):
                rows.append(r)
        return [Detected(id=(r.get("id") or "").strip(), exists=bool(r.get("exists")),
                         overwrite_name=(r.get("overwrite_name") or "").strip(),
                         selected=bool(r.get("selected")))
                for r in rows]
    return []
