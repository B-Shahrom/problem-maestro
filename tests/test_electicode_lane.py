import json
from pathlib import Path

import pytest

from maestro.electicode_lane import (RETRY_CAP, ChoreGroup, ElectiCodeLane, chore_groups,
                                     stage_key, stage_progress, stage_retryable)
from maestro.model import (BlockReason, Problem, ProblemSeed, ProblemStage,
                           ProblemStatus, RunStage, RunStatus)
from maestro.scraper import ScraperClient
from maestro.store import Store

from tests.conftest import SLUGS

TITLES = {"edu-arrays-running-max": "Running Maximum",
          "edu-arrays-largest-gap": "Largest Gap"}


class FakeScraper:
    """Stands in for the Scraper's CLIs at the argv boundary.

    It answers the way the real tools do — including the one that matters most:
    `problem_uploader upload` writes nothing to `--output` on a preview, so the
    detection has to come back through stderr.
    """

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.calls: list[list[str]] = []
        self.session_rc = 0
        self.upload_rc = 0        # the preview pass
        self.apply_rc = 0         # the mutating pass
        self.scrape_rc = 0
        self.chores_rc = 0
        self.chore_plan = [("metadata", "metadata"), ("division", "division")]
        self.chore_steps = None               # override the start event's `total`
        self.chore_warnings: list[str] = []
        self.fail_at = "division"             # which stage stops on a non-zero rc
        self.audit_rc = 0
        self.exists: set[str] = set()
        self.overwrite_names: dict[str, str] = {}
        self.catalog_slugs: list[str] | None = None      # None → every uploaded slug
        self.catalog_names: dict[str, str] = {}
        self.catalog_limits: dict[str, tuple[int, int]] = {}
        self.emit_limits = True
        self.division_access = "Electi"
        self.uploaded: list[str] = []
        self.detect_only: list[str] | None = None        # None → every folder handed over

    # ------------------------------------------------------------------ hook

    def __call__(self, argv: list[str], timeout: float, progress=None):
        self.calls.append(argv)
        tool = Path(argv[1]).name
        return getattr(self, f"_{tool.removesuffix('.py')}")(argv)

    @staticmethod
    def _opt(argv: list[str], name: str, default: str = "") -> str:
        return argv[argv.index(name) + 1] if name in argv else default

    # ------------------------------------------------------------- the tools

    def _contest_scraper(self, argv):
        return self.session_rc, json.dumps({"valid": self.session_rc == 0}), ""

    def _problem_uploader(self, argv):
        folder = Path(self._opt(argv, "--folder"))
        names = self.detect_only if self.detect_only is not None else \
            sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.is_dir() else []
        out = [{"event": "start", "tool": "upload", "op": "upload",
                "total": len(names), "apply": "--apply" in argv}]
        for n in names:
            out.append({"event": "item", "tool": "upload", "op": "upload", "id": n,
                        "ok": True, "exists": n in self.exists, "selected": True,
                        "overwrite_name": self.overwrite_names.get(n, TITLES.get(n, n))
                        if n in self.exists else None})
        ndjson = "\n".join(json.dumps(o) for o in out) + "\n"
        if "--apply" not in argv:
            return self.upload_rc, ndjson, ""
        # The tool refuses when a requested slug isn't among the detected rows.
        want = [s for s in self._opt(argv, "--only").split(",") if s]
        if missing := [s for s in want if s not in names]:
            return 2, ndjson, f"--only: not detected: {', '.join(missing)}\n"
        if self.apply_rc == 0:
            self.uploaded = want or names
        return self.apply_rc, ndjson, ""

    def _problem_scraper(self, argv):
        slugs = self.catalog_slugs if self.catalog_slugs is not None else self.uploaded
        rows = []
        for s in slugs:
            row = {"s3_id": s, "name": self.catalog_names.get(s, TITLES.get(s, s)),
                   "difficulty": "Easy", "category": "arrays", "division_access": self.division_access}
            if self.emit_limits:
                tl, ml = self.catalog_limits.get(s, (1000, 262144))
                row["time_limit_ms"], row["memory_limit_kb"] = tl, ml
            rows.append(row)
        Path(self._opt(argv, "--output")).write_text(json.dumps(rows), encoding="utf-8")
        return self.scrape_rc, "", ""

    def _batch(self, argv):
        """Runs the plan minus --skip, stopping at `fail_at` (--stop-on-error)."""
        rc = self.chores_rc.pop(0) if isinstance(self.chores_rc, list) else self.chores_rc
        skipped = set(self._opt(argv, "--skip").split(",")) - {""}
        plan = [s for s in self.chore_plan if s[1] not in skipped]
        start = {"event": "start", "tool": "batch", "op": "run",
                 "total": len(plan) if self.chore_steps is None else self.chore_steps,
                 "apply": "--apply" in argv}
        if self.chore_warnings:
            start["warnings"] = self.chore_warnings
        out = [start]
        for name, key in plan:
            ok = not (rc and key == self.fail_at)
            out.append({"event": "item", "tool": "batch", "op": "run", "id": name,
                        "ok": ok, "status": "ok" if ok else "failed",
                        "detail": "exit 0" if ok else "exit 2"})
            if not ok:
                break
        return rc, "\n".join(json.dumps(o) for o in out) + "\n", "step failed\n"

    def _report(self, argv):
        out = Path(self._opt(argv, "--output"))
        out.write_text(json.dumps({"total": 2, "counts": {"difficulty_mismatch": 1},
                                   "issues": {}}), encoding="utf-8")
        return self.audit_rc, "", ""


@pytest.fixture
def lane(tmp_path, set_dir):
    store = Store(tmp_path / "maestro.db")
    seeds = [ProblemSeed(slug=s, idx=i, title=TITLES[s], archive=f"{s}.zip")
             for i, s in enumerate(SLUGS, 1)]
    run_id = store.create_run(set_dir.name, set_dir, seeds)
    fake = FakeScraper(tmp_path)
    client = ScraperClient(tmp_path / "scraper", tmp_path / "session.json", runner=fake)
    lane = ElectiCodeLane(store, client, tmp_path / "runs", apply=True, divisions="Electi")

    # The Polygon half's handoff: a folder per problem under runs/<id>/upload.
    for s in SLUGS:
        (lane.upload_dir(run_id) / s).mkdir(parents=True)
        store.set_problem(run_id, s, stage=ProblemStage.SHAPED, status=ProblemStatus.OK)
    store.set_run(run_id, stage=RunStage.UPLOAD, status=RunStatus.RUNNING)
    yield lane, store, fake, run_id
    store.close()


def _drive(lane, store, run_id, stages=4):
    reports = []
    for _ in range(stages):
        reports.append(lane.step(run_id))
    return reports


# ----------------------------------------------------------------- grouping


def test_unknown_exists_groups_with_the_pre_existing_ones():
    """Appending to a fresh problem is harmless; overwriting an old one is not."""
    ps = [Problem(1, "a", 1, "A", "a.zip", existed_before_upload=False),
          Problem(1, "b", 2, "B", "b.zip", existed_before_upload=True),
          Problem(1, "c", 3, "C", "c.zip", existed_before_upload=None)]
    assert chore_groups(ps) == [ChoreGroup("fresh", "reset", ["a"]),
                                ChoreGroup("existing", "add", ["b", "c"])]


def test_a_uniform_batch_is_one_group():
    ps = [Problem(1, "a", 1, "A", "a.zip", existed_before_upload=False)]
    assert chore_groups(ps) == [ChoreGroup("fresh", "reset", ["a"])]


# ------------------------------------------------------------- happy path


def test_a_clean_run_reaches_done(lane):
    lane_, store, fake, run_id = lane
    _drive(lane_, store, run_id)
    run = store.get_run(run_id)
    assert (run.stage, run.status) == (RunStage.DONE, RunStatus.DONE)
    assert all(p.stage is ProblemStage.AUDITED for p in store.active(run_id))


def test_preview_runs_before_apply(lane):
    lane_, store, fake, run_id = lane
    lane_.step(run_id)
    uploads = [c for c in fake.calls if c[1].endswith("problem_uploader.py")]
    assert len(uploads) == 2
    assert "--apply" not in uploads[0] and "--apply" in uploads[1]


def test_the_preview_decides_each_problem_s_tag_mode(lane):
    lane_, store, fake, run_id = lane
    fake.exists = {SLUGS[1]}
    _drive(lane_, store, run_id, stages=3)

    by_slug = {p.slug: p for p in store.problems(run_id)}
    assert by_slug[SLUGS[0]].existed_before_upload is False
    assert by_slug[SLUGS[1]].existed_before_upload is True

    modes = [(c[c.index("--char") + 1], c[c.index("--tags-mode") + 1])
             for c in fake.calls if c[1].endswith("batch.py")]
    assert sorted(m for _, m in modes) == ["add", "reset"]
    for path, mode in modes:
        text = Path(path).read_text(encoding="utf-8")
        assert (SLUGS[1] in text) == (mode == "add")


def test_each_chore_file_keeps_its_rows_and_tags_aligned(lane):
    """batch.py pairs tag line k to row k, so a filtered file must filter both."""
    lane_, store, fake, run_id = lane
    fake.exists = {SLUGS[1]}
    _drive(lane_, store, run_id, stages=3)

    from maestro.characteristics import parse
    for c in (c for c in fake.calls if c[1].endswith("batch.py")):
        parsed = parse(Path(c[c.index("--char") + 1]).read_text(encoding="utf-8"))
        assert parsed.tags_will_apply
        assert len(parsed.rows) == 1
        expected = {SLUGS[0]: "implementation, arrays", SLUGS[1]: "observation, arrays"}
        assert parsed.tags[0] == expected[parsed.rows[0].slug]


# ------------------------------------------------------------ refusals


def test_an_undetected_folder_stops_before_apply(lane):
    lane_, store, fake, run_id = lane
    fake.detect_only = [SLUGS[0]]
    lane_.step(run_id)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert SLUGS[1] in run.error
    assert not any("--apply" in c for c in fake.calls)


def test_a_title_collision_stops_before_apply(lane):
    """An EXISTS row that names someone else's problem is a real collision."""
    lane_, store, fake, run_id = lane
    fake.exists = {SLUGS[0]}
    fake.overwrite_names = {SLUGS[0]: "Someone Else's Problem"}
    lane_.step(run_id)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "Someone Else's Problem" in run.error
    assert not any("--apply" in c for c in fake.calls)


def test_the_apply_pass_ticks_only_this_run_s_slugs(lane):
    lane_, store, fake, run_id = lane
    lane_.step(run_id)
    apply = next(c for c in fake.calls if "--apply" in c)
    assert sorted(apply[apply.index("--only") + 1].split(",")) == sorted(SLUGS)


def test_a_foreign_folder_warns_but_does_not_stop_the_upload(lane):
    """`--only` unticks it, so the risk is gone; the disagreement still matters."""
    lane_, store, fake, run_id = lane
    fake.detect_only = [*SLUGS, "someone-elses-problem"]
    report = lane_.step(run_id)
    assert report.run_advanced_to is RunStage.RECONCILE
    warned = [e["message"] for e in store.events(run_id) if e["level"] == "warn"]
    assert any("someone-elses-problem" in m for m in warned)
    apply = next(c for c in fake.calls if "--apply" in c)
    assert "someone-elses-problem" not in apply[apply.index("--only") + 1]


def test_an_empty_detection_stops_before_apply(lane):
    lane_, store, fake, run_id = lane
    fake.detect_only = []
    lane_.step(run_id)
    assert store.get_run(run_id).status is RunStatus.FAILED
    assert not any("--apply" in c for c in fake.calls)


def test_a_slug_missing_from_the_catalog_halts_the_run(lane):
    """Stage 6.5 exists for exactly this: upload said yes, the catalog disagrees."""
    lane_, store, fake, run_id = lane
    lane_.step(run_id)
    fake.catalog_slugs = [SLUGS[0]]
    lane_.step(run_id)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert SLUGS[1] in run.error
    assert not any(c[1].endswith("batch.py") for c in fake.calls)


def test_a_renamed_problem_warns_but_continues(lane):
    lane_, store, fake, run_id = lane
    lane_.step(run_id)
    fake.catalog_names = {SLUGS[0]: "Running Max"}
    report = lane_.step(run_id)
    assert report.run_advanced_to is RunStage.CHORES
    warned = [e["message"] for e in store.events(run_id) if e["level"] == "warn"]
    assert any("Running Max" in m for m in warned)


# --------------------------------------------------------------- blocking


def test_an_expired_session_parks_the_run(lane):
    lane_, store, fake, run_id = lane
    fake.session_rc = 3
    report = lane_.step(run_id)
    run = store.get_run(run_id)
    assert (run.status, run.block_reason) == (RunStatus.BLOCKED, BlockReason.SESSION_EXPIRED)
    assert report.blocked is BlockReason.SESSION_EXPIRED
    assert not any(c[1].endswith("problem_uploader.py") for c in fake.calls)


def test_a_missing_session_file_parks_rather_than_fails(lane):
    lane_, store, fake, run_id = lane
    fake.session_rc = 1
    lane_.step(run_id)
    assert store.get_run(run_id).block_reason is BlockReason.SESSION_EXPIRED


def test_preview_mode_gates_the_mutating_stages(lane):
    lane_, store, fake, run_id = lane
    lane_.apply = False
    report = lane_.step(run_id)
    assert report.blocked is BlockReason.AWAITING_APPROVAL
    assert not any(c[1].endswith("problem_uploader.py") for c in fake.calls)


def test_preview_mode_still_reconciles(lane):
    """Reconcile and audit only read, so a dry run gets to prove what it would find."""
    lane_, store, fake, run_id = lane
    lane_.step(run_id)
    lane_.apply = False
    report = lane_.step(run_id)
    assert report.run_advanced_to is RunStage.CHORES


def test_an_audit_gap_blocks_instead_of_retrying(lane):
    lane_, store, fake, run_id = lane
    fake.audit_rc = 1
    _drive(lane_, store, run_id)
    run = store.get_run(run_id)
    assert run.status is RunStatus.BLOCKED
    assert run.block_reason is BlockReason.AWAITING_APPROVAL
    reports = [e["message"] for e in store.events(run_id) if e["level"] == "warn"]
    assert any("difficulty mismatch: 1" in m for m in reports)


# ----------------------------------------------------------------- retries


def test_an_operational_failure_retries_then_fails(lane):
    lane_, store, fake, run_id = lane
    fake.scrape_rc = 2
    lane_.step(run_id)                       # upload succeeds
    for _ in range(RETRY_CAP):
        lane_.step(run_id)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "catalog scrape" in run.error
    scrapes = [c for c in fake.calls if c[1].endswith("problem_scraper.py")]
    assert len(scrapes) == RETRY_CAP


def test_a_validation_failure_does_not_retry(lane):
    lane_, store, fake, run_id = lane
    fake.scrape_rc = 1
    lane_.step(run_id)
    lane_.step(run_id)
    assert store.get_run(run_id).status is RunStatus.FAILED
    assert len([c for c in fake.calls if c[1].endswith("problem_scraper.py")]) == 1


def test_a_chore_failure_stops_before_the_audit(lane):
    lane_, store, fake, run_id = lane
    fake.chores_rc = 2
    _drive(lane_, store, run_id, stages=3)
    assert store.get_run(run_id).stage is RunStage.CHORES
    assert not any(c[1].endswith("report.py") for c in fake.calls)


# ----------------------------------------------------------- quarantine


def test_a_stopped_run_is_not_stepped_again(lane):
    """Otherwise the retry cap does not hold — a scheduler just starts it over."""
    lane_, store, fake, run_id = lane
    fake.detect_only = []
    lane_.step(run_id)
    assert store.get_run(run_id).status is RunStatus.FAILED
    before = len(fake.calls)
    assert lane_.step(run_id).stage is None
    assert len(fake.calls) == before


def test_existed_before_upload_is_written_once(lane):
    """A late apply failure sees Maestro's own upload as EXISTS on the retry."""
    lane_, store, fake, run_id = lane
    fake.apply_rc = 2                         # the modal never closed
    lane_.step(run_id)
    assert all(p.existed_before_upload is False for p in store.active(run_id))

    fake.apply_rc = 0
    fake.exists = set(SLUGS)                  # the retry sees what the first try made
    lane_.step(run_id)
    assert all(p.existed_before_upload is False for p in store.active(run_id))
    modes = {c[c.index("--tags-mode") + 1] for c in fake.calls if c[1].endswith("batch.py")}
    assert modes in ({"reset"}, set())


def test_an_appending_metadata_stage_is_not_retried(lane):
    """`_append_value` never de-dupes, so a repeat yields "arrays, arrays"."""
    lane_, store, fake, run_id = lane
    fake.exists = set(SLUGS)                  # every problem takes --tags-mode add
    fake.chores_rc, fake.fail_at = 2, "metadata"
    _drive(lane_, store, run_id, stages=3)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "not idempotent" in run.error and "metadata" in run.error
    assert len([c for c in fake.calls if c[1].endswith("batch.py")]) == 1


def test_an_overwriting_metadata_stage_is_retried(lane):
    """`--tags-mode reset` re-fills a fixed value, so a repeat converges."""
    lane_, store, fake, run_id = lane
    fake.chores_rc, fake.fail_at = 2, "metadata"
    _drive(lane_, store, run_id, stages=3)
    assert store.get_run(run_id).status is RunStatus.RUNNING


def test_a_division_failure_is_retried_even_in_an_append_group(lane):
    """`division set` is declarative, so the group's tag mode is irrelevant to it."""
    lane_, store, fake, run_id = lane
    fake.exists = set(SLUGS)
    fake.chores_rc, fake.fail_at = 2, "division"
    _drive(lane_, store, run_id, stages=3)
    assert store.get_run(run_id).status is RunStatus.RUNNING


def test_a_resumed_chain_skips_the_stages_that_landed(lane):
    """Otherwise the retry re-appends tags that were already applied."""
    lane_, store, fake, run_id = lane
    fake.exists = set(SLUGS)
    fake.chores_rc, fake.fail_at = [2, 0], "division"
    _drive(lane_, store, run_id, stages=3)     # metadata ok, division fails
    lane_.step(run_id)                         # retry

    runs = [c for c in fake.calls if c[1].endswith("batch.py")]
    assert "--skip" not in runs[0]
    assert runs[1][runs[1].index("--skip") + 1] == "metadata"
    assert store.get_run(run_id).stage is RunStage.AUDIT


def test_a_list_add_failure_is_not_retried(lane):
    """`list add` de-dupes against row titles while being handed slugs."""
    lane_, store, fake, run_id = lane
    lane_.list_url = "https://www.electicode.com/en/admin/contests/1/manage"
    fake.chore_plan = [("metadata", "metadata"), ("list add", "list-add")]
    fake.chores_rc, fake.fail_at = 2, "list-add"
    _drive(lane_, store, run_id, stages=3)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "list-add" in run.error


def test_an_unattributable_failure_is_not_retried(lane):
    """A stage Maestro cannot name is a stage it knows nothing about replaying."""
    lane_, store, fake, run_id = lane
    fake.chore_plan = [("some new stage", "whatever")]
    fake.chores_rc, fake.fail_at = 2, "whatever"
    _drive(lane_, store, run_id, stages=3)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "unknown" in run.error


# ------------------------------------------------------- stage bookkeeping


def test_stage_names_map_to_the_keys_only_skip_understands():
    assert stage_key("fixmdx (subtasks)") == "fixmdx"
    assert stage_key("fixmdx (all)") == "fixmdx"
    assert stage_key("list add") == "list-add"
    assert stage_key("list reorder") == "list-reorder"
    assert stage_key("metadata") == "metadata"
    assert stage_key("something else") is None


def test_stage_progress_reads_the_prefix_and_the_failure():
    evts = [{"event": "start", "total": 3},
            {"event": "item", "id": "metadata", "ok": True},
            {"event": "item", "id": "division", "ok": False}]
    assert stage_progress(evts) == (["metadata"], "division")


def test_stage_progress_ignores_a_stage_it_cannot_name():
    """An unnameable stage is neither skipped later nor called a known failure."""
    evts = [{"event": "item", "id": "brand new", "ok": True},
            {"event": "item", "id": "also new", "ok": False}]
    assert stage_progress(evts) == ([], None)


def test_only_metadata_depends_on_the_tag_mode():
    assert stage_retryable("metadata", "reset") is True
    assert stage_retryable("metadata", "add") is False
    assert stage_retryable("division", "add") is True
    assert stage_retryable("list-add", "reset") is False
    assert stage_retryable("custom", "reset") is False
    assert stage_retryable(None, "reset") is False


def test_resuming_a_failed_run_skips_the_group_that_landed(lane):
    """The operator clears the failure; the completed group must not run twice."""
    lane_, store, fake, run_id = lane
    fake.exists = {SLUGS[1]}
    fake.fail_at = "metadata"                 # in an `add` group: stops for a human
    fake.chores_rc = [0, 2, 0]                # fresh ok, existing fails, then ok
    _drive(lane_, store, run_id, stages=3)
    assert store.get_run(run_id).status is RunStatus.FAILED

    store.set_run(run_id, status=RunStatus.RUNNING)
    lane_.step(run_id)
    ran = [Path(c[c.index("--char") + 1]).name
           for c in fake.calls if c[1].endswith("batch.py")]
    assert ran == ["characteristics-fresh.md", "characteristics-existing.md",
                   "characteristics-existing.md"]
    assert store.get_run(run_id).stage is RunStage.AUDIT


def test_an_empty_plan_is_a_failure_not_a_success(lane):
    """batch.py exits 0 when every step was skipped — nothing was applied."""
    lane_, store, fake, run_id = lane
    fake.chore_steps = 0
    _drive(lane_, store, run_id, stages=3)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "no steps" in run.error


def test_silently_dropped_tags_are_a_failure(lane):
    """build_plan discards ALL tags on a count mismatch, warns, and exits 0."""
    lane_, store, fake, run_id = lane
    fake.chore_warnings = ["tags skipped: 3 tag line(s) but 2 problem(s) — "
                           "they must line up with the General table"]
    _drive(lane_, store, run_id, stages=3)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "tags silently dropped" in run.error


def test_a_quarantined_problem_is_left_out_of_the_chore_file(lane):
    """It was never uploaded, so choring it would target a slug that isn't there."""
    lane_, store, fake, run_id = lane
    store.quarantine(run_id, SLUGS[1], "verification failed")
    fake.detect_only = [SLUGS[0]]
    _drive(lane_, store, run_id, stages=3)
    chores = [c for c in fake.calls if c[1].endswith("batch.py")]
    assert chores
    for c in chores:
        assert SLUGS[1] not in Path(c[c.index("--char") + 1]).read_text(encoding="utf-8")


def test_a_wrong_limit_on_the_platform_fails_the_audit(lane):
    """Both fields are read-only there — only a corrected re-import fixes it."""
    lane_, store, fake, run_id = lane
    fake.catalog_limits = {SLUGS[0]: (2000, 262144)}   # authored 1 s / 256 MB
    _drive(lane_, store, run_id)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "wrong limits" in run.error
    assert not any(c[1].endswith("report.py") for c in fake.calls)


def test_matching_limits_let_the_audit_proceed(lane):
    lane_, store, fake, run_id = lane
    _drive(lane_, store, run_id)
    assert store.get_run(run_id).stage is RunStage.DONE
    assert any(c[1].endswith("report.py") for c in fake.calls)


def test_a_catalog_without_limits_warns_but_continues(lane):
    """The check is new; its absence must not block a run that is otherwise clean."""
    lane_, store, fake, run_id = lane
    fake.emit_limits = False
    _drive(lane_, store, run_id)
    assert store.get_run(run_id).stage is RunStage.DONE
    warned = [e["message"] for e in store.events(run_id) if e["level"] == "warn"]
    assert any("could not be verified" in w for w in warned)


def test_an_approved_run_writes_without_a_scheduler_wide_apply(lane):
    """The apply gate has to be releasable for one batch, or it means nothing."""
    lane_, store, fake, run_id = lane
    lane_.apply = False
    assert lane_.step(run_id).blocked is BlockReason.AWAITING_APPROVAL

    store.approve(run_id)
    store.set_run(run_id, status=RunStatus.RUNNING)
    report = lane_.step(run_id)
    assert report.blocked is None
    assert report.run_advanced_to is RunStage.RECONCILE
    assert any("--apply" in c for c in fake.calls)


def test_approval_does_not_bypass_the_idempotency_rules(lane):
    """Approving says "you may write", not "retry anything"."""
    lane_, store, fake, run_id = lane
    lane_.apply = False
    store.approve(run_id)
    fake.exists = set(SLUGS)
    fake.chores_rc, fake.fail_at = 2, "metadata"
    _drive(lane_, store, run_id, stages=3)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "not idempotent" in run.error


def test_a_batch_with_no_division_access_fails_the_audit(lane):
    """`report audit` skips its division check in this exact state and exits 0."""
    lane_, store, fake, run_id = lane
    fake.division_access = ""
    _drive(lane_, store, run_id)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "division access" in run.error
    assert not any(c[1].endswith("report.py") for c in fake.calls)


def test_granted_divisions_let_the_audit_proceed(lane):
    lane_, store, fake, run_id = lane
    _drive(lane_, store, run_id)
    assert store.get_run(run_id).stage is RunStage.DONE
