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
import signal
import sys
from pathlib import Path

from .dashboard import Dashboard
from .electicode_lane import ElectiCodeLane
from .model import RunStatus
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


def build(cfg: dict) -> tuple[Scheduler, Store]:
    if not cfg["scraper_repo"]:
        raise SystemExit("config needs `scraper_repo` — the path to the platform-scraper checkout")
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
    dashboard = Dashboard(store, host=cfg["dashboard_host"], port=cfg["dashboard_port"]).start()

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
    try:
        scheduler.run_forever(max_ticks=args.max_ticks)
    except KeyboardInterrupt:
        print("\nstopping — waiting for any ElectiCode stage in flight…", file=sys.stderr)
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
            note = r.block_reason or (r.error or "")
            flag = "" if r.approved else "  [unapproved]"
            print(f"{r.id:>4}  {r.set_name:<32} {r.stage:<10} {r.status:<8} "
                  f"{len(r.problems):>2}p{flag}  {str(note)[:60]}")
    # Non-zero when something wants a human, so a cron or a prompt can react.
    return 1 if any(r.status in (RunStatus.BLOCKED, RunStatus.FAILED) for r in runs) else 0


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
    p_run.set_defaults(func=cmd_run)

    sub.add_parser("status", help="Print every run and exit.").set_defaults(func=cmd_status)
    sub.add_parser("init", help="Write a starter config.").set_defaults(func=cmd_init)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
