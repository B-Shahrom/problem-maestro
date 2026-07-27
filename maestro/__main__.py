"""`python -m maestro` — start the pipeline, or inspect it.

Configuration is a JSON file rather than flags, because most of it is
credentials-adjacent paths that should not sit in a shell history or a systemd
unit: where the Scraper checkout is, which session file it uses, where the
Middleman answers. The file itself is gitignored along with the secrets it
points at.

`run` starts the scheduler and the dashboard in one process. That is deliberate:
they share a `Store`, which is safe because the store serialises its own access,
and two processes on one SQLite file would be a worse trade than one process with
two threads.
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
from pathlib import Path

from . import brief as brief_mod
from . import feedback
from .dashboard import Dashboard
from .electicode_lane import ElectiCodeLane
from .ingest import Verdict, inspect
from .model import ProblemStage, RunStatus
from .polygon import PolygonClient
from .polygon_lane import PolygonLane
from .scheduler import Scheduler
from .scraper import ScraperClient
from .store import Store

DEFAULTS: dict = {
    "db": "runs/maestro.db",
    "work_dir": "runs",
    "watch_dir": None,
    "middleman_url": "http://127.0.0.1:8000",
    "scraper_repo": None,
    "scraper_state": "session_state.json",
    "electicode_base": "https://www.electicode.com",
    "apply": False,
    "divisions": "",
    "targets": "",
    "list_url": "",
    "fixmdx": "subtasks",
    "dashboard_host": "127.0.0.1",
    "dashboard_port": 8787,
}


def load_config(path: str | Path) -> dict:
    """Read the config, filling in defaults and rejecting unknown keys.

    Unknown keys are an error rather than being ignored: a typo in a key that
    silently kept the default is exactly how `apply` ends up off when the
    operator believed they had turned it on.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if unknown := sorted(set(raw) - set(DEFAULTS)):
        raise SystemExit(f"unknown config key(s): {', '.join(unknown)}\n"
                         f"known keys: {', '.join(sorted(DEFAULTS))}")
    return {**DEFAULTS, **raw}


#: The Scraper tools Maestro actually invokes. Checked at startup because the
#: alternative is a subprocess failing with "can't open file" some minutes into a
#: run, naming a path the operator has to work backwards from.
REQUIRED_TOOLS = ("problem_uploader.py", "problem_scraper.py", "contest_scraper.py",
                  "batch.py", "report.py")


def check_paths(cfg: dict) -> list[str]:
    """Everything wrong with the configured paths, in one go.

    All of it is local and cheap, and every item here is something that would
    otherwise surface later as a failure whose message points at a symptom rather
    than at the config line that caused it.
    """
    problems: list[str] = []

    repo = cfg["scraper_repo"]
    if not repo:
        problems.append("`scraper_repo` is not set — it is the path to the platform-scraper "
                        "checkout (the folder holding problem_uploader.py, batch.py, …)")
    else:
        path = Path(repo)
        if missing := [t for t in REQUIRED_TOOLS if not (path / t).is_file()]:
            hint = ""
            # The common mistake: pointing at `output/`, which is where the
            # Scraper *writes* results, not where its tools live.
            if all((path.parent / t).is_file() for t in REQUIRED_TOOLS):
                hint = f"\n    Did you mean its parent? {path.parent}"
            problems.append(f"`scraper_repo` {path} is missing {', '.join(missing)}{hint}")

    state = Path(cfg["scraper_state"])
    if not state.is_file():
        problems.append(f"`scraper_state` {state} does not exist — log in with "
                        "`contest_scraper.py login --url …` to create it")

    watch = cfg["watch_dir"]
    if watch and not Path(watch).is_dir():
        # Otherwise the sweep finds nothing and reports nothing, and a set dropped
        # into the folder the operator *meant* is never picked up.
        problems.append(f"`watch_dir` {watch} is not a directory")

    return problems


#: What Maestro invokes, and the Phase 2 item that added it. Checked because a
#: checkout that predates one of these fails *mid-run* with argparse's own error,
#: minutes in and several stages from anything the operator configured.
#:
#: Read statically from the source rather than by running `--help`: that costs no
#: subprocess, and it still answers correctly on a machine where the Scraper's own
#: dependencies are missing — which is a different problem deserving its own
#: message.
#: `(what, phase, pattern)` per tool. The patterns tolerate the line breaks
#: argparse calls are routinely written with — `sub.add_parser(\n    "session",`
#: is the real shape in `contest_scraper.py`, and matching a bare
#: `add_parser("session"` misses it and reports a working checkout as broken.
REQUIRED_CAPABILITIES: dict[str, list[tuple[str, str, str]]] = {
    "contest_scraper.py": [("the `session` subcommand", "priority 4", r'add_parser\(\s*"session"')],
    "problem_uploader.py": [("the `upload` subcommand", "Phase 1", r'add_parser\(\s*"upload"'),
                            ("`upload --only`", "priority 8", r'"--only"'),
                            ("`upload --json`", "priority 6", r'"--json"')],
    "problem_scraper.py": [("the `problems` subcommand", "Phase 1", r'add_parser\(\s*"problems"')],
    "batch.py": [("the `run` subcommand", "Phase 1", r'add_parser\(\s*"run"'),
                 ("`run --tags-mode`", "Phase 1", r'"--tags-mode"'),
                 ("`run --skip`", "priority 8", r'"--skip"'),
                 ("`run --json`", "priority 6", r'"--json"')],
    "report.py": [("the `audit` subcommand", "Phase 1", r'add_parser\(\s*"audit"'),
                  ("`audit --char`", "priority 5", r'"--char"')],
}


def check_capabilities(cfg: dict) -> list[str]:
    """Whether the configured Scraper checkout can do what Maestro asks of it."""
    repo = Path(cfg["scraper_repo"] or "")
    out: list[str] = []
    for tool, needs in REQUIRED_CAPABILITIES.items():
        try:
            source = (repo / tool).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue  # a missing tool is check_paths' finding, not this one
        for what, added in ((w, a) for w, a, p in needs if not re.search(p, source)):
            out.append(f"{tool} does not support {what} (added by Phase 2 {added}) — "
                       f"this checkout is older than the contract Maestro was built against")
    if out:
        out.append(f"update it:  git -C \"{repo}\" pull")
    return out


def build(cfg: dict, *, check: bool = True) -> tuple[Scheduler, Store]:
    if check and (problems := check_paths(cfg) + check_capabilities(cfg)):
        raise SystemExit("config problems:\n  - " + "\n  - ".join(problems))
    store = Store(cfg["db"])
    middleman = PolygonClient(cfg["middleman_url"])
    scraper = ScraperClient(cfg["scraper_repo"], cfg["scraper_state"],
                            base=cfg["electicode_base"])
    scheduler = Scheduler(
        store,
        PolygonLane(store, middleman, cfg["work_dir"]),
        ElectiCodeLane(store, scraper, cfg["work_dir"], apply=cfg["apply"],
                       divisions=cfg["divisions"], targets=cfg["targets"],
                       list_url=cfg["list_url"], fixmdx=cfg["fixmdx"]),
        watch_dir=cfg["watch_dir"],
        parser=middleman.parse,
    )
    return scheduler, store


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    scheduler, store = build(cfg)
    dashboard = Dashboard(store, host=cfg["dashboard_host"], port=cfg["dashboard_port"],
                          scheduler=scheduler).start()

    gate = "writes ENABLED" if cfg["apply"] else "preview only — approve runs in the dashboard"
    print(f"Maestro on http://{cfg['dashboard_host']}:{dashboard.port}  ({gate})", file=sys.stderr)
    if cfg["watch_dir"]:
        print(f"Watching {cfg['watch_dir']}", file=sys.stderr)

    def bye(*_):
        # SIGTERM is how a service manager stops this, so it has to mean the same
        # thing as Ctrl-C: stop ticking, and let an in-flight ElectiCode stage
        # finish if it can rather than leaving the platform half-chored.
        scheduler.stop()

    signal.signal(signal.SIGTERM, bye)
    seen: set[str] = set()
    cursors: dict[int, int] = {}

    def tail(run_id: int) -> None:
        """Echo whatever this run has logged since the last tick.

        The event log is the only place a running stage says anything, and until
        now it was only visible in the dashboard. An operator watching a terminal
        saw nothing at all for the twenty minutes an upload takes — which is
        indistinguishable from a hang, and is exactly how a healthy run gets
        killed on suspicion of being stuck.
        """
        rows = store.events(run_id, after_id=cursors.get(run_id, 0), limit=200)
        for r in rows:
            cursors[run_id] = r["id"]
            if r["level"] == "debug" and not args.verbose:
                continue
            mark = {"error": "[!]", "warn": " ! "}.get(r["level"], "   ")
            where = f" {r['slug']}" if r["slug"] else ""
            print(f"{mark} {run_id}{where}: {r['message']}", file=sys.stderr)

    def narrate(report) -> None:
        """Print what changed, once. A quiet loop is indistinguishable from a stuck one."""
        for name in report.ingested:
            print(f"ingested {name}", file=sys.stderr)
        for name, verdict, why in report.rejected:
            # Rejections repeat every tick while the cause persists, so print each
            # distinct one once rather than every five seconds.
            if (line := f"{name}: {verdict} — {why}") not in seen:
                seen.add(line)
                print(f"[!] {line}", file=sys.stderr)
        for path in report.reported:
            print(f"    wrote {path}", file=sys.stderr)
        for item in report.advanced:
            print(f"    {item}", file=sys.stderr)
        for err in report.errors:
            print(f"[!] {err}", file=sys.stderr)
        for run in store.list_runs():
            tail(run.id)

    try:
        scheduler.run_forever(max_ticks=args.max_ticks, on_tick=narrate)
    except KeyboardInterrupt:
        print("\nstopping — waiting for any ElectiCode stage in flight. It runs in its "
              "own process group, so this Ctrl-C did not reach it; it will finish or "
              "time out on its own.", file=sys.stderr)
        scheduler.stop()
    finally:
        dashboard.stop()
        store.close()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """One-shot listing. Useful over ssh, and when the browser is not."""
    cfg = load_config(args.config)
    with Store(cfg["db"]) as store:
        runs = store.list_runs()
        if not runs:
            print("no runs")
            return 0
        for r in runs:
            note = r.block_reason or r.error
            if not note and (last := store.last_event(r.id)) is not None:
                # A stage name has looked identical for twenty minutes whether
                # the upload is progressing or wedged. The last thing it said is
                # what tells them apart.
                note = f"{last['at'][11:19]} {last['message']}"
            flag = "" if r.approved else "  [unapproved]"
            print(f"{r.id:>4}  {r.set_name:<32} {r.stage:<10} {r.status:<8} "
                  f"{len(r.problems):>2}p{flag}  {str(note or '')[:70]}")
    # Non-zero when something wants a human, so a cron or a prompt can react.
    return 1 if any(r.status in (RunStatus.BLOCKED, RunStatus.FAILED) for r in runs) else 0


def cmd_inspect(args: argparse.Namespace) -> int:
    """Say what Maestro makes of every folder in the watch directory.

    The answer to "I dropped a set in and nothing happened". A rejected candidate
    is never silently dropped by the scheduler either, but this reports without
    starting anything and prints every finding rather than the first.
    """
    cfg = load_config(args.config)
    if not cfg["watch_dir"]:
        raise SystemExit("config has no `watch_dir`")
    watch = Path(cfg["watch_dir"])

    loose = sorted(p.name for p in watch.iterdir() if p.is_file())
    folders = sorted(p for p in watch.iterdir() if p.is_dir())
    if loose:
        # The likeliest mistake, and it is invisible otherwise: the sweep only
        # looks at directories, so a delivered .zip sitting here is never seen.
        print(f"[!] {len(loose)} loose file(s) in {watch}, which are ignored — a set is a "
              f"FOLDER of per-problem archives plus characteristics.md and MANIFEST.json:",
              file=sys.stderr)
        for name in loose:
            print(f"      {name}", file=sys.stderr)
    if not folders:
        print(f"no set folders in {watch}", file=sys.stderr)
        return 1

    with Store(cfg["db"]) as store:
        worst = 0
        for folder in folders:
            result = inspect(folder, store)
            print(f"\n{folder.name}: {result.verdict}", file=sys.stderr)
            for f in result.findings:
                print(f"    {f.severity.value:<5} {f.check}"
                      f"{' ' + f.slug if f.slug else ''}: {f.message}", file=sys.stderr)
            # Naming what did not run matters more here than in the scheduler: this
            # command is read as a verdict on the whole set, and two of the three
            # check families are gated behind a clean manifest.
            if skipped := [label for key, label in feedback.FAMILIES.items()
                           if key not in result.checked]:
                print(f"    not checked: {'; '.join(skipped)}", file=sys.stderr)
            if args.report and (path := feedback.write(result)) is not None:
                print(f"    wrote {path}", file=sys.stderr)
            if result.verdict is Verdict.READY:
                print("    ready — the next tick will ingest it", file=sys.stderr)
            elif result.verdict is Verdict.INCOMPLETE:
                print("    still arriving, or MANIFEST.json is missing — Maestro will "
                      "re-check every tick", file=sys.stderr)
                worst = max(worst, 1)
            elif result.verdict is Verdict.INVALID:
                worst = 2
    return worst


def cmd_forget(args: argparse.Namespace) -> int:
    """Delete Maestro's record of a run. Previews unless `--yes`.

    The preview is not politeness. Deleting a run removes what Maestro knows and
    nothing else: problems already imported to Polygon and rows already uploaded
    to ElectiCode stay exactly where they are, and the set folder — still sitting
    in the watch directory — becomes eligible for ingest again the moment the
    name is free. Both of those are usually what the operator wants and neither
    is guessable from the word "delete".
    """
    cfg = load_config(args.config)
    with Store(cfg["db"]) as store:
        run = store.get_run(args.run_id)
        if run is None:
            print(f"no run {args.run_id}", file=sys.stderr)
            return 1

        uploaded = [p.slug for p in run.problems if p.electicode_slug
                    or p.stage in (ProblemStage.UPLOADED, ProblemStage.RECONCILED,
                                   ProblemStage.CHORED, ProblemStage.AUDITED)]
        imported = [p.slug for p in run.problems if p.polygon_problem_id]

        print(f"run {run.id}  {run.set_name}  {run.stage}/{run.status}  "
              f"{len(run.problems)} problem(s)", file=sys.stderr)
        print(f"  set folder: {run.set_dir}", file=sys.stderr)
        if imported:
            print(f"  [!] {len(imported)} problem(s) exist on Polygon and are NOT removed: "
                  f"{', '.join(imported[:5])}{'…' if len(imported) > 5 else ''}", file=sys.stderr)
        if uploaded:
            print(f"  [!] {len(uploaded)} problem(s) are on ElectiCode and are NOT removed: "
                  f"{', '.join(uploaded[:5])}{'…' if len(uploaded) > 5 else ''}", file=sys.stderr)
        print(f"  after deleting, the folder is eligible for ingest again — move it out of "
              f"{cfg['watch_dir']} first if you do not want the run to start over",
              file=sys.stderr)

        # RUNNING means a `maestro run` elsewhere may be mid-subprocess for this
        # batch. Deleting under it leaves a browser writing to the platform for a
        # run that no longer exists.
        if run.status is RunStatus.RUNNING and not args.force:
            print(f"\nrun {run.id} is RUNNING — stop `maestro run` first, or pass --force "
                  f"if you are certain nothing is driving it", file=sys.stderr)
            return 1
        if not args.yes:
            print(f"\nnothing deleted. Re-run with --yes to delete run {run.id}.",
                  file=sys.stderr)
            return 1

        store.delete_run(run.id)
    print(f"deleted run {run.id} ({run.set_name})", file=sys.stderr)
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    """Render the authoring brief for one set. Stage 0, by hand for now.

    Goes to stdout rather than a file so it can be piped or pasted, and because
    the brief is not a durable artefact — it is derived from the store and the
    gate's own constants, so regenerating it is always cheaper than finding the
    copy that was sent.
    """
    cfg = load_config(args.config)
    try:
        mix = brief_mod.parse_mix(args.mix)
    except ValueError as e:
        raise SystemExit(str(e))

    b = brief_mod.Brief(
        name=args.name,
        prefix=args.prefix or "",
        topic=args.topic or "",
        mix=mix,
        languages=tuple(x.strip() for x in args.languages.split(",") if x.strip()),
        notes=args.notes or "",
    )

    # The store answers one question no flag can: whether this name is already
    # spent. `set_name` is UNIQUE, so a repeat is delivered into silence.
    with Store(cfg["db"]) as store:
        taken = {r.set_name for r in store.list_runs()}
    if problems := brief_mod.check(b, taken):
        for f in problems:
            print(f"  - {f.message}", file=sys.stderr)
        return 1

    contracts = Path(args.contracts) if args.with_contracts else None
    print(brief_mod.render(b, contracts_dir=contracts))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Validate the config without starting anything. Run this first."""
    cfg = load_config(args.config)
    if problems := check_paths(cfg) + check_capabilities(cfg):
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    gate = "writes ENABLED" if cfg["apply"] else "preview only"
    print(f"config OK — {gate}, dashboard on "
          f"{cfg['dashboard_host']}:{cfg['dashboard_port']}", file=sys.stderr)
    if cfg["watch_dir"]:
        sets = sorted(p.name for p in Path(cfg["watch_dir"]).iterdir() if p.is_dir())
        print(f"watching {cfg['watch_dir']} — {len(sets)} set folder(s): "
              f"{', '.join(sets) or '(none yet)'}", file=sys.stderr)
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    path = Path(args.config)
    if path.exists():
        raise SystemExit(f"{path} already exists — not overwriting it")
    path.write_text(json.dumps(DEFAULTS, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {path}. Set `scraper_repo` before running.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="maestro", description=__doc__.split("\n")[0])
    parser.add_argument("--config", default="config.json", help="Path to the config JSON.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Start the scheduler and the dashboard.")
    p_run.add_argument("--max-ticks", type=int, default=None,
                       help="Stop after this many ticks (for a one-shot sweep).")
    p_run.add_argument("-v", "--verbose", action="store_true",
                       help="Echo every line the Scraper tools write, not just their "
                            "own progress events.")
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("status", help="Print every run and exit.").set_defaults(func=cmd_status)
    sub.add_parser("init", help="Write a starter config.").set_defaults(func=cmd_init)
    sub.add_parser("check", help="Validate the config's paths and exit.").set_defaults(func=cmd_check)
    p_forget = sub.add_parser("forget", help="Delete Maestro's record of a run.")
    p_forget.add_argument("run_id", type=int)
    p_forget.add_argument("--yes", action="store_true", help="Actually delete it.")
    p_forget.add_argument("--force", action="store_true",
                          help="Delete even a RUNNING run. Stop `maestro run` first.")
    p_forget.set_defaults(func=cmd_forget)

    p_brief = sub.add_parser("brief", help="Render the authoring brief for a new set.")
    p_brief.add_argument("name", help="The set name — `set.name` in the manifest.")
    p_brief.add_argument("--mix", required=True,
                         help="Problems per group: `easy=2,medium=2,hard=1` or `2:2:1`.")
    p_brief.add_argument("--prefix", help="Shared slug prefix, e.g. edu-arrays.")
    p_brief.add_argument("--topic", help="One line on what the set covers.")
    p_brief.add_argument("--languages", default="EN", help="Statement languages, comma-separated.")
    p_brief.add_argument("--notes", help="Anything else the author needs for this set.")
    p_brief.add_argument("--with-contracts", action="store_true",
                         help="Inline the contract documents, for a session that lacks them.")
    p_brief.add_argument("--contracts", default="docs/contracts",
                         help="Where the contract documents live.")
    p_brief.set_defaults(func=cmd_brief)

    p_inspect = sub.add_parser("inspect", help="Say what Maestro makes of each watch-dir folder.")
    p_inspect.add_argument("--report", action="store_true",
                           help="Also write a correction request beside each rejected folder, "
                                "for handing back to the author.")
    p_inspect.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
