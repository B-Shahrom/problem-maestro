"""Durable job store.

A run can outlive the process: a Polygon build takes ~60s per problem, a chore
chain takes tens of minutes, and either can be interrupted by an expired
ElectiCode session. Every transition is committed so a restarted Maestro can
resume from the last completed stage rather than from the beginning.

SQLite, stdlib only. One file, WAL mode, no ORM — the schema is small and the
access patterns are known.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .model import (
    BlockReason,
    Problem,
    ProblemSeed,
    ProblemStage,
    ProblemStatus,
    Run,
    RunStage,
    RunStatus,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    set_name     TEXT NOT NULL UNIQUE,
    set_dir      TEXT NOT NULL,
    stage        TEXT NOT NULL,
    status       TEXT NOT NULL,
    block_reason TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS problems (
    run_id                INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    slug                  TEXT    NOT NULL,
    idx                   INTEGER NOT NULL,
    title                 TEXT    NOT NULL,
    archive               TEXT    NOT NULL,
    stage                 TEXT    NOT NULL,
    status                TEXT    NOT NULL,
    error                 TEXT,
    polygon_problem_id    INTEGER,
    polygon_package_id    INTEGER,
    electicode_slug       TEXT,
    existed_before_upload INTEGER,
    updated_at            TEXT    NOT NULL,
    PRIMARY KEY (run_id, slug)
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    slug    TEXT,
    level   TEXT NOT NULL,
    message TEXT NOT NULL,
    at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_problems_run   ON problems(run_id, stage, status);
CREATE INDEX IF NOT EXISTS idx_events_run     ON events(run_id, id);
"""

# Stages a problem passes through per-problem (the Polygon half). Beyond SHAPED
# the batch drives it, so per-problem readiness is only meaningful up to here.
_PER_PROBLEM_STAGES = (
    ProblemStage.PENDING,
    ProblemStage.IMPORTED,
    ProblemStage.BUILT,
    ProblemStage.DOWNLOADED,
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield self._db
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    # ---------------------------------------------------------------- runs

    def create_run(self, set_name: str, set_dir: str | Path, seeds: list[ProblemSeed]) -> int:
        """Register a validated set. Fails if the set name is already known.

        Uniqueness is on `set_name`, which carries a date and an optional `-rN`
        rerun suffix, so a corrected re-delivery is a distinct run rather than a
        mutation of the original.
        """
        if not seeds:
            raise ValueError("a run needs at least one problem")
        now = _now()
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO runs (set_name, set_dir, stage, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?)",
                (set_name, str(set_dir), RunStage.INGEST, RunStatus.PENDING, now, now),
            )
            run_id = int(cur.lastrowid)
            db.executemany(
                "INSERT INTO problems (run_id, slug, idx, title, archive, stage, status, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [
                    (run_id, s.slug, s.idx, s.title, s.archive,
                     ProblemStage.PENDING, ProblemStatus.PENDING, now)
                    for s in seeds
                ],
            )
        return run_id

    def get_run(self, run_id: int) -> Run | None:
        row = self._db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        run = Run(
            id=row["id"],
            set_name=row["set_name"],
            set_dir=row["set_dir"],
            stage=RunStage(row["stage"]),
            status=RunStatus(row["status"]),
            block_reason=BlockReason(row["block_reason"]) if row["block_reason"] else None,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
        run.problems = self.problems(run_id)
        return run

    def list_runs(self) -> list[Run]:
        ids = [r["id"] for r in self._db.execute("SELECT id FROM runs ORDER BY id DESC")]
        return [r for r in (self.get_run(i) for i in ids) if r is not None]

    def set_run(
        self,
        run_id: int,
        *,
        stage: RunStage | None = None,
        status: RunStatus | None = None,
        block_reason: BlockReason | None = None,
        error: str | None = None,
    ) -> None:
        sets, args = ["updated_at=?"], [_now()]
        if stage is not None:
            sets.append("stage=?"); args.append(str(stage))
        if status is not None:
            sets.append("status=?"); args.append(str(status))
            # Clearing BLOCKED must clear its reason, or a stale one leaks into the UI.
            if status is not RunStatus.BLOCKED and block_reason is None:
                sets.append("block_reason=NULL")
        if block_reason is not None:
            sets.append("block_reason=?"); args.append(str(block_reason))
        if error is not None:
            sets.append("error=?"); args.append(error)
        args.append(run_id)
        with self._tx() as db:
            db.execute(f"UPDATE runs SET {', '.join(sets)} WHERE id=?", args)

    def block(self, run_id: int, reason: BlockReason, message: str) -> None:
        """Park a run for a human.

        An expired ElectiCode session is a *state*, not an error: the operator
        logs in on the host and the run resumes where it stopped. Same for an
        apply-gate awaiting approval.
        """
        self.set_run(run_id, status=RunStatus.BLOCKED, block_reason=reason)
        self.log(run_id, "warn", message)

    # ------------------------------------------------------------ problems

    def problems(self, run_id: int) -> list[Problem]:
        rows = self._db.execute(
            "SELECT * FROM problems WHERE run_id=? ORDER BY idx", (run_id,)
        )
        return [self._problem(r) for r in rows]

    @staticmethod
    def _problem(r: sqlite3.Row) -> Problem:
        existed = r["existed_before_upload"]
        return Problem(
            run_id=r["run_id"],
            slug=r["slug"],
            idx=r["idx"],
            title=r["title"],
            archive=r["archive"],
            stage=ProblemStage(r["stage"]),
            status=ProblemStatus(r["status"]),
            error=r["error"],
            polygon_problem_id=r["polygon_problem_id"],
            polygon_package_id=r["polygon_package_id"],
            electicode_slug=r["electicode_slug"],
            existed_before_upload=None if existed is None else bool(existed),
        )

    def set_problem(
        self,
        run_id: int,
        slug: str,
        *,
        stage: ProblemStage | None = None,
        status: ProblemStatus | None = None,
        error: str | None = None,
        polygon_problem_id: int | None = None,
        polygon_package_id: int | None = None,
        electicode_slug: str | None = None,
        existed_before_upload: bool | None = None,
    ) -> None:
        sets, args = ["updated_at=?"], [_now()]
        for col, val in (
            ("stage", str(stage) if stage else None),
            ("status", str(status) if status else None),
            ("error", error),
            ("polygon_problem_id", polygon_problem_id),
            ("polygon_package_id", polygon_package_id),
            ("electicode_slug", electicode_slug),
            ("existed_before_upload", None if existed_before_upload is None else int(existed_before_upload)),
        ):
            if val is not None:
                sets.append(f"{col}=?"); args.append(val)
        args += [run_id, slug]
        with self._tx() as db:
            cur = db.execute(
                f"UPDATE problems SET {', '.join(sets)} WHERE run_id=? AND slug=?", args
            )
            if cur.rowcount == 0:
                raise KeyError(f"no problem {slug!r} in run {run_id}")

    def quarantine(self, run_id: int, slug: str, reason: str) -> None:
        """Drop one problem from the batch and keep the run going.

        The caller must also exclude the slug from the upload parent folder and
        filter it out of the characteristics file — `batch.py` acting on a slug
        that was never uploaded is the failure this prevents.
        """
        self.set_problem(run_id, slug, status=ProblemStatus.QUARANTINED, error=reason)
        self.log(run_id, "warn", f"quarantined: {reason}", slug=slug)

    def active(self, run_id: int) -> list[Problem]:
        """Problems still in the batch — everything not quarantined or failed."""
        return [
            p for p in self.problems(run_id)
            if p.status not in (ProblemStatus.QUARANTINED, ProblemStatus.FAILED)
        ]

    def ready_for(self, run_id: int, stage: ProblemStage) -> list[Problem]:
        """Active problems whose previous per-problem stage is complete.

        Only meaningful inside the Polygon half; past SHAPED the batch advances
        problems together and this returns nothing.
        """
        if stage not in _PER_PROBLEM_STAGES[1:] and stage is not ProblemStage.SHAPED:
            return []
        order = list(ProblemStage)
        prev = order[order.index(stage) - 1]
        return [
            p for p in self.active(run_id)
            if p.stage is prev and p.status in (ProblemStatus.PENDING, ProblemStatus.OK)
        ]

    # -------------------------------------------------------------- events

    def log(self, run_id: int, level: str, message: str, slug: str | None = None) -> int:
        with self._tx() as db:
            cur = db.execute(
                "INSERT INTO events (run_id, slug, level, message, at) VALUES (?,?,?,?,?)",
                (run_id, slug, level, message, _now()),
            )
        return int(cur.lastrowid)

    def events(self, run_id: int, after_id: int = 0, limit: int = 500) -> list[sqlite3.Row]:
        """Cursor-based tail, so the dashboard can poll without re-reading."""
        return list(self._db.execute(
            "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
            (run_id, after_id, limit),
        ))

    # -------------------------------------------------------------- resume

    def resume_point(self, run_id: int) -> tuple[RunStage, list[Problem]]:
        """Where a restarted process should pick up.

        Returns the run's stage and, inside the Polygon half, the problems that
        still owe work at it. A blocked run reports its stage unchanged — the
        caller decides whether the block has cleared.
        """
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"no run {run_id}")
        if run.stage is not RunStage.POLYGON:
            return run.stage, []
        pending = [
            p for p in self.active(run_id)
            if p.stage is not ProblemStage.SHAPED or p.status is not ProblemStatus.OK
        ]
        return run.stage, pending
