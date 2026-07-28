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
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

ELECTICODE_BASE = "https://www.electicode.com"

#: Seconds of total silence from a tool before it is worth saying so.
#:
#: A chore chain is legitimately quiet for a while — one page load, one modal,
#: one save per problem — so this is not a timeout, it is the line between "slow"
#: and "no longer telling me anything". Without it the two are identical from
#: outside, which is the entire reason a stuck upload is unanswerable.
QUIET_AFTER = 90.0

#: How often the waiter wakes to check the clock. Small enough that a heartbeat
#: lands near its due time, large enough to cost nothing over three hours.
POLL = 2.0


@dataclass(frozen=True, slots=True)
class Progress:
    """One line from a running tool, as it happens.

    `stream` is `stdout`, `stderr`, or `heartbeat` — the last being Maestro's own
    observation that nothing has arrived, which is a fact about the run and not
    about the tool.
    """

    stream: str
    line: str
    elapsed: float


#: What a caller wants told while a tool runs. Called from the reader threads, so
#: it must not block for long and must tolerate being called concurrently.
OnLine = Callable[[Progress], None]

#: `(argv, timeout_seconds, progress) -> (returncode, stdout, stderr)`. Injected
#: so the lane can be tested without a browser, a session, or a Scraper checkout.
Runner = Callable[[list[str], float, OnLine | None], tuple[int, str, str]]


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


#: Windows NTSTATUS values that arrive where an exit code is expected. They are
#: not exit codes at all — the process never reached its own `sys.exit`, so
#: reading them against the Scraper's 0/1/2 taxonomy describes the wrong event
#: entirely.
#:
#: `0xC000013A` is the one that matters here. It is what a console Ctrl-C
#: produces, and on Windows Ctrl-C is delivered to the whole **process group**,
#: so interrupting Maestro also kills the browser it launched. The Playwright
#: `TargetClosedError` that follows is the browser being torn down, not the cause
#: of anything. Reported as "unexpected exit code 3221225786" it reads as a tool
#: that broke; it is an operator who pressed Ctrl-C.
_NTSTATUS = {
    0xC000013A: ("Ctrl-C", "STATUS_CONTROL_C_EXIT"),
    0xC0000005: ("an access violation — the process crashed", "STATUS_ACCESS_VIOLATION"),
    0xC00000FD: ("a stack overflow", "STATUS_STACK_OVERFLOW"),
    0xC0000142: ("a DLL that failed to initialise", "STATUS_DLL_INIT_FAILED"),
    0xC0000374: ("heap corruption", "STATUS_HEAP_CORRUPTION"),
    0xC0000409: ("a stack buffer overrun", "STATUS_STACK_BUFFER_OVERRUN"),
    0x40010004: ("the console closing or the user logging off", "DBG_TERMINATE_PROCESS"),
}

_SIGNALS = {
    signal.SIGINT: "Ctrl-C (SIGINT)",
    signal.SIGTERM: "SIGTERM",
    signal.SIGKILL if hasattr(signal, "SIGKILL") else 9: "SIGKILL — often the OOM killer",
    signal.SIGSEGV if hasattr(signal, "SIGSEGV") else 11: "SIGSEGV — the process crashed",
    signal.SIGABRT if hasattr(signal, "SIGABRT") else 6: "SIGABRT",
    signal.SIGHUP if hasattr(signal, "SIGHUP") else 1: "SIGHUP — the terminal went away",
}


def killed(rc: int) -> str | None:
    """How this process was terminated, if it did not exit on its own.

    Returns `None` for an ordinary exit code. Three encodings reach here for the
    same class of event, and none of them is a number the tool chose:

    * `Popen` reports a POSIX signal as a negative return code.
    * a shell in between reports it as `128 + signum`.
    * Windows reports an NTSTATUS as a large unsigned value.

    Distinguishing them from a real exit code is what stops "killed by Ctrl-C"
    being filed under "the Scraper failed".
    """
    if rc < 0 and (name := _SIGNALS.get(-rc)):
        return f"killed by {name}"
    if rc < 0:
        return f"killed by signal {-rc}"
    if entry := _NTSTATUS.get(rc):
        what, code = entry
        return f"terminated by {what} [{code}, {rc}]"
    if 128 < rc < 160 and (name := _SIGNALS.get(rc - 128)):
        return f"killed by {name} (reported as {rc})"
    return None


def interpret(rc: int, *, session: bool = False) -> tuple[Outcome, str]:
    """Map a Scraper exit code to an action.

    A process that was *killed* is separated from one that exited first. It never
    reached its own exit path, so its return value says nothing about the request
    — which makes RETRY right for all of them, and makes the reason string the
    part that actually matters to whoever reads it.

    An unknown code halts rather than retrying: the tools document three values
    (four for `session`), so a fourth means Maestro is running against a version
    it was not built for, and guessing would be worse than stopping.
    """
    if how := killed(rc):
        return Outcome.RETRY, (
            f"{how}. The tool did not fail — it was stopped from outside, so nothing "
            f"it reported describes the request. If this was Maestro shutting down, "
            f"the stage will resume on the next run")
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


def _detached() -> dict[str, Any]:
    """Launch options that put the child outside Maestro's signal group.

    Ctrl-C in a console is delivered to the whole process group on both
    platforms, so without this, interrupting Maestro also kills the browser it
    launched — mid-upload, mid-chore, whenever it happens to land. That is the
    worst possible moment: `scheduler.stop()` exists precisely to let an
    in-flight ElectiCode stage finish rather than leave the platform half-chored,
    and it cannot do that for a child that is already dead.

    The cost is that a Maestro killed outright leaves a browser running. That is
    the better failure: a stray process is visible and cheap to clean up, while a
    batch interrupted between "tags applied" and "divisions granted" is neither.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _kill_tree(p: subprocess.Popen) -> None:
    """Kill the tool *and* the browser it spawned.

    `Popen.kill` reaches one process. Playwright's chromium is a grandchild, and
    an orphaned one keeps holding the ElectiCode session — so the next stage
    fails in a way that has nothing to do with the next stage.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                           capture_output=True, timeout=30)
            return
        except (OSError, subprocess.SubprocessError):
            pass  # fall through to the single-process kill
    else:
        try:
            group = os.getpgid(p.pid)
            # Only ever signal a group the child owns. If `_detached` did not
            # take effect the child shares Maestro's group, and killing that
            # group kills Maestro — and, under a test runner, the test runner.
            # Losing the browser is the lesser failure by a wide margin.
            if group != os.getpgid(0):
                os.killpg(group, signal.SIGKILL)
                return
        except (OSError, AttributeError):
            pass
    try:
        p.kill()
    except OSError:
        pass


def run_streamed(argv: list[str], timeout: float,
                 progress: OnLine | None = None, *,
                 quiet_after: float = QUIET_AFTER) -> tuple[int, str, str]:
    """Run a tool, reporting each line as it arrives rather than at the end.

    The previous implementation captured output and returned it on exit. For a
    tool that runs for tens of minutes that means the operator learns nothing at
    all until it is over — and if it never gets there, nothing ever. "Something
    is stuck uploading" was not a hard question to answer; it was an unanswerable
    one, because the only observation available was the absence of a result.

    Output still accumulates and is still returned whole, so every caller that
    parses it is unaffected. What is new is that it also goes out live, and that
    a silent tool is reported as silent instead of as nothing.
    """
    started = time.monotonic()
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace",
                             bufsize=1, **_detached())
    except OSError as e:
        # A missing interpreter or tool. Report it in the shape every caller
        # already handles rather than raising through the lane.
        return 2, "", f"could not start {argv[1] if len(argv) > 1 else argv[0]}: {e}"

    out: list[str] = []
    err: list[str] = []
    lock = threading.Lock()
    last = [started]

    def pump(stream, sink: list[str], name: str) -> None:
        try:
            for raw in stream:
                line = raw.rstrip("\r\n")
                with lock:
                    sink.append(line)
                    last[0] = time.monotonic()
                if progress is not None and line.strip():
                    try:
                        progress(Progress(name, line, time.monotonic() - started))
                    except Exception:  # noqa: BLE001
                        pass  # a reporting failure must not kill the run it reports on
        finally:
            stream.close()

    readers = [threading.Thread(target=pump, args=(p.stdout, out, "stdout"),
                                name="scraper-out", daemon=True),
               threading.Thread(target=pump, args=(p.stderr, err, "stderr"),
                                name="scraper-err", daemon=True)]
    for t in readers:
        t.start()

    # The waiter only notices silence when it wakes, so its interval bounds how
    # precisely a quiet period can be observed. Deriving it from `quiet_after`
    # keeps the two consistent instead of leaving a caller who asked for a short
    # heartbeat to wonder why none arrives.
    step = min(POLL, max(0.05, quiet_after / 4))
    while True:
        try:
            p.wait(timeout=step)
            break
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            if now - started >= timeout:
                _kill_tree(p)
                for t in readers:
                    t.join(5)
                # A hung browser is operational, not a bad request: report it as
                # the Scraper's own "operational failure" code so the retry
                # policy applies.
                return 2, "\n".join(out), "\n".join(
                    err + [f"timed out after {timeout:.0f}s and was killed"])
            with lock:
                quiet = now - last[0]
            if progress is not None and quiet >= quiet_after:
                progress(Progress(
                    "heartbeat",
                    f"still running after {now - started:.0f}s, but nothing on stdout or "
                    f"stderr for {quiet:.0f}s"
                    + (f" — last: {out[-1][:120]}" if out else " — no output at all yet"),
                    now - started))
                with lock:
                    last[0] = now  # one heartbeat per quiet period, not one per poll

    for t in readers:
        t.join(5)
    return p.returncode, "\n".join(out), "\n".join(err)


#: The tools that accept `--state` on their parent parser — i.e. everything that
#: drives a browser. `report.py` does **not**: it is a pure file-to-file
#: transform over an existing scrape, so it has no session to load and its parser
#: has no such option.
#:
#: Sending it anyway is not a harmless extra. argparse consumes the flag it does
#: not know, then matches the *path* against the `command` positional and reports
#: `invalid choice: 'C:\\...\\session_state.json' (choose from 'audit')` — which
#: reads exactly like a stale checkout, and is what stage 8 failed with for every
#: run until this was found.
STATEFUL_TOOLS = frozenset({
    "problem_uploader.py", "problem_scraper.py", "contest_scraper.py",
    "batch.py", "problem_editor.py",
})


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
        self._run_argv: Runner = runner or run_streamed
        self.python = python or sys.executable or "python3"
        self.timeout = timeout

    # ------------------------------------------------------------- plumbing

    def _run(self, tool: str, sub: str, *args: str,
             session: bool = False, timeout: float | None = None,
             progress: OnLine | None = None) -> Result:
        state = ["--state", str(self.state)] if tool in STATEFUL_TOOLS else []
        argv = [self.python, str(self.repo / tool), *state, sub, *args]
        rc, out, err = self._run_argv(argv, timeout or self.timeout, progress)
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

    def session(self, *, progress: OnLine | None = None) -> Result:
        """Non-mutating auth check. Run before anything that needs a browser.

        Cheap relative to what it guards: without it, an expired session surfaces
        as a mid-batch upload failure whose cause is a timed-out selector.
        """
        r = self._run("contest_scraper.py", "session", "--base", self.base, "--json",
                      session=True, timeout=180.0, progress=progress)
        try:
            r.data = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            r.data = None
        return r

    # --------------------------------------------------------------- upload

    def upload(self, folder: str | Path, output: str | Path, *, apply: bool = False,
               only: list[str] | None = None, progress: OnLine | None = None) -> Result:
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
        r = self._run("problem_uploader.py", "upload", *args, timeout=1200.0,
                      progress=progress)
        r.data = detected(r.stdout, self._read_json(Path(output)))
        return r

    # ---------------------------------------------------------------- audit

    def scrape(self, output: str | Path, *, strict: bool = True,
               from_catalog: bool = False, progress: OnLine | None = None) -> Result:
        """The catalog read behind both stage 6.5 and stage 8.

        `--strict` on purpose: a paging failure that returns 40 of 2,000 problems
        would otherwise exit 0, and every slug Maestro just uploaded would look
        absent. A short scrape must fail loudly rather than read as a mismatch.

        `from_catalog` reads the flight payload in **one page load** instead of
        paging the table ~40×, which is the difference between seconds and
        minutes on a 2,000-problem platform. It is not a free win: the catalog
        carries a subset of the columns, and `division_access` is not among them.
        So it is the caller's decision, made per stage from what that stage
        actually reads — see `ElectiCodeLane._needs_paged_scrape`.

        The tool falls back to the paged scrape by itself if the catalog cannot
        be read, and marks its output `source` either way, so a caller can tell
        which it got rather than assuming.
        """
        args = ["--base", self.base, "--format", "json",
                "--output", str(Path(output).resolve())]
        if from_catalog:
            args.append("--from-catalog")
        if strict:
            args.append("--strict")
        r = self._run("problem_scraper.py", "problems", *args, progress=progress)
        r.data = catalog(self._read_json(Path(output)))
        return r

    def audit(self, scrape_json: str | Path, char: str | Path, output: str | Path,
              *, divisions: str = "", progress: OnLine | None = None) -> Result:
        """Stage 8. Actual-vs-expected, joined per slug.

        Exits `1` on any gap *or* mismatch, so `OK` here is the run's completion
        signal. Note it is a pure file-to-file transform — no browser, no session.
        """
        args = ["--input", str(Path(scrape_json).resolve()),
                "--char", str(Path(char).resolve()),
                "--format", "json", "--output", str(Path(output).resolve())]
        if divisions:
            args += ["--divisions", divisions]
        r = self._run("report.py", "audit", *args, timeout=300.0, progress=progress)
        r.data = self._read_json(Path(output))
        return r

    # --------------------------------------------------------------- chores

    def chores(self, char: str | Path, *, tags_mode: str, apply: bool = False,
               divisions: str = "", targets: str = "", list_url: str = "",
               fixmdx: str = "subtasks", stop_on_error: bool = True,
               skip: str = "", progress: OnLine | None = None) -> Result:
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
        r = self._run("batch.py", "run", *args, timeout=max(self.timeout, 10800.0),
                      progress=progress)
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
