"""Core state vocabulary.

The pipeline is a two-level state machine, not a linear sequence. Stages 3-5
(Polygon: import, build+verify, download) run **per problem** — one problem can
fail verification while its siblings succeed. Stages 6-8 (ElectiCode: upload,
chores, audit) run **per batch** — the uploader takes a parent directory and the
chore runner takes one characteristics file for the whole set.

`RunStage` therefore tracks the batch; `ProblemStage` tracks each problem through
the per-problem half and then follows the batch through the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class RunStage(StrEnum):
    """Batch-level position in the pipeline."""

    INGEST = "ingest"          # watch folder, validate manifest + characteristics
    POLYGON = "polygon"        # stages 3-5, fans out per problem
    UPLOAD = "upload"          # stage 6, one parent folder
    RECONCILE = "reconcile"    # stage 6.5, scrape catalog and match slugs
    CHORES = "chores"          # stage 7, batch.py
    AUDIT = "audit"            # stage 8, report.py audit
    DONE = "done"

    @property
    def next(self) -> "RunStage | None":
        order = list(RunStage)
        i = order.index(self)
        return order[i + 1] if i + 1 < len(order) else None


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    BLOCKED = "blocked"    # needs a human: expired session, or an apply-gate awaiting approval
    FAILED = "failed"
    DONE = "done"


class ProblemStage(StrEnum):
    """Per-problem position. Advances independently through the Polygon half."""

    PENDING = "pending"
    IMPORTED = "imported"
    BUILT = "built"            # buildPackage(verify=true) reached READY
    DOWNLOADED = "downloaded"
    SHAPED = "shaped"          # extracted, folder named <slug>, binaries pruned
    UPLOADED = "uploaded"
    RECONCILED = "reconciled"  # ElectiCode slug confirmed against the manifest
    CHORED = "chored"
    AUDITED = "audited"


class ProblemStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"
    QUARANTINED = "quarantined"
    """Excluded from the batch after a per-problem failure.

    A quarantined problem is dropped from the upload parent folder *and* filtered
    out of the characteristics file handed to `batch.py` — otherwise the chore
    runner acts on a slug that was never uploaded. Its siblings continue.
    """


class BlockReason(StrEnum):
    SESSION_EXPIRED = "session_expired"
    AWAITING_APPROVAL = "awaiting_approval"


@dataclass(frozen=True, slots=True)
class ProblemSeed:
    """What the manifest gives us before any stage has run."""

    slug: str
    idx: int
    title: str
    archive: str


@dataclass(slots=True)
class Problem:
    run_id: int
    slug: str
    idx: int
    title: str
    archive: str
    stage: ProblemStage = ProblemStage.PENDING
    status: ProblemStatus = ProblemStatus.PENDING
    error: str | None = None

    # Identity map. The slug is the join key; these are what each system calls it.
    # Nothing downstream may assume they agree — stage RECONCILE is what proves it.
    polygon_problem_id: int | None = None
    polygon_package_id: int | None = None
    electicode_slug: str | None = None

    polygon_job_id: str | None = None
    attempts: int = 0
    """Tries at the *current* stage. Reset when the stage advances."""

    existed_before_upload: bool | None = None
    """Drives the reset-vs-add tag decision.

    `problem_editor assign --category` uses `fill()`, which replaces the field
    rather than appending, so applying authored tags to a problem that already
    carried curricular ones (e.g. `academy exam`) would destroy them. Fresh
    problems take `--category`; pre-existing ones take `--category-add`. Unknown
    (`None`) must never be treated as fresh.
    """


@dataclass(slots=True)
class Run:
    id: int
    set_name: str
    set_dir: str
    stage: RunStage = RunStage.INGEST
    status: RunStatus = RunStatus.PENDING
    block_reason: BlockReason | None = None
    error: str | None = None

    divisions: str | None = None
    """Division access this batch asks for — see `maestro.divisions`.

    `None` means never chosen, so the configured default applies. `""` means
    chosen and deliberately empty, which must NOT fall back: an operator who
    unticked everything asked for no divisions, not for the default.
    """

    approved: bool = False
    """An operator has let this run write to ElectiCode.

    Per-run rather than a scheduler-wide `apply` flag, because that is the only
    shape in which `AWAITING_APPROVAL` means anything — a global flag cannot be
    flipped for one batch.
    """

    created_at: str = ""
    updated_at: str = ""
    problems: list[Problem] = field(default_factory=list)
