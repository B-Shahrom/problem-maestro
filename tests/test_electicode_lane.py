import json
from pathlib import Path

import pytest

from maestro.checks import Severity, errors
from maestro.electicode_lane import (RETRY_CAP, ChoreGroup, ElectiCodeLane, chore_groups,
                                     list_landed, stage_key, stage_progress,
                                     stage_retryable)
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
        self.audit_extra: dict = {}
        self.list_items: dict | None = None
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
        """The two sources carry *different columns*, exactly as the real ones do.

        The flight-payload catalog has the limits and no `division_access`; the
        paged table has `division_access` and no limit columns. A fake that
        returned the same rows for both would make every "which source did this
        check read" test vacuous — and that is precisely the bug worth catching,
        since handing both checks the same rows looks identical until one of the
        two fields is missing.
        """
        from_catalog = "--from-catalog" in argv
        slugs = self.catalog_slugs if self.catalog_slugs is not None else self.uploaded
        rows = []
        for s in slugs:
            row = {"s3_id": s, "name": self.catalog_names.get(s, TITLES.get(s, s)),
                   "difficulty": "Easy", "category": "arrays"}
            if from_catalog:
                if self.emit_limits:
                    tl, ml = self.catalog_limits.get(s, (1000, 262144))
                    row["time_limit_ms"], row["memory_limit_kb"] = tl, ml
            else:
                row["division_access"] = self.division_access
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
            # `batch run --json` forwards --json to json-capable children, whose
            # per-item events land on the same stdout. Reproduced here because
            # that interleaving is what the list-membership check reads.
            if ok and key == "list-add" and self.list_items is not None:
                for slug, status in self.list_items.items():
                    out.append({"event": "item", "tool": "list", "op": "add",
                                "id": slug, "ok": status not in ("not_found", "error"),
                                "status": status})
            if not ok:
                break
        return rc, "\n".join(json.dumps(o) for o in out) + "\n", "step failed\n"

    def _report(self, argv):
        out = Path(self._opt(argv, "--output"))
        payload = {"total": 2, "counts": {"difficulty_mismatch": 1}, "issues": {},
                   "checks_run": ["difficulty", "tags", "division"], "skipped": []}
        payload.update(self.audit_extra)
        out.write_text(json.dumps(payload), encoding="utf-8")
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


# ------------------------------------------ an audit that skipped a check


def _audit_result(payload):
    from maestro.scraper import Outcome, Result
    return Result(argv=["report.py"], rc=0, stdout="", stderr="",
                  outcome=Outcome.OK, reason="ok", data=payload)


def test_an_audit_that_skipped_a_check_does_not_finish_the_run(lane):
    """`report audit --char` exits 0 when it *skips* the division check, and says
    so in `skipped`. Reading that is the last link in a chain that otherwise ends
    with an unverified grant marked done."""
    l, store, fake, run_id = lane
    l.divisions = "Electi"
    from maestro.electicode_lane import LaneReport

    rep = LaneReport()
    skipped = _audit_result({"checks_run": ["difficulty", "tags"], "skipped": ["division"],
                             "issues": {}, "total": 2})
    assert l._audit_incomplete(run_id, "Electi", skipped, rep) is True
    assert rep.blocked is BlockReason.AWAITING_APPROVAL
    assert store.get_run(run_id).status is RunStatus.BLOCKED
    note = store.last_event(run_id)["message"]
    assert "division" in note and "not that the batch is correct" in note


def test_an_audit_that_ran_everything_finishes_the_run(lane):
    from maestro.electicode_lane import LaneReport

    l, store, fake, run_id = lane
    l.divisions = "Electi"
    rep = LaneReport()
    full = _audit_result({"checks_run": ["difficulty", "tags", "division"],
                          "skipped": [], "issues": {}, "total": 2})
    assert l._audit_incomplete(run_id, "Electi", full, rep) is False
    assert rep.blocked is None


def test_no_division_configured_means_no_division_check_is_expected(lane):
    from maestro.electicode_lane import LaneReport

    l, store, fake, run_id = lane
    l.divisions = ""
    rep = LaneReport()
    two = _audit_result({"checks_run": ["difficulty", "tags"], "skipped": [], "issues": {}})
    assert l._audit_incomplete(run_id, "", two, rep) is False


def test_a_tool_that_does_not_say_which_checks_ran_is_noted_not_blocked(lane):
    """An older report.py may well have run everything — but must not read as proof."""
    from maestro.electicode_lane import LaneReport

    l, store, fake, run_id = lane
    l.divisions = "Electi"
    rep = LaneReport()
    silent = _audit_result({"issues": {}, "total": 2})
    assert l._audit_incomplete(run_id, "Electi", silent, rep) is False
    assert "did not report which checks it ran" in store.last_event(run_id)["message"]


def test_a_skipped_audit_check_stops_the_run_short_of_done(lane):
    """The wiring, not just the predicate.

    A test that calls `_audit_incomplete` directly still passes when the call is
    deleted from `_audit` — which is exactly the mutation that would ship a run
    marked DONE on an audit that never checked the divisions.
    """
    l, store, fake, run_id = lane
    l.divisions = "Electi"
    fake.audit_extra = {"checks_run": ["difficulty", "tags"], "skipped": ["division"]}

    _drive(l, store, run_id)
    run = store.get_run(run_id)
    assert run.stage is not RunStage.DONE, "an unverified division grant reached DONE"
    assert run.status is RunStatus.BLOCKED
    assert "did not run every check" in store.last_event(run_id)["message"]


def test_a_complete_audit_still_reaches_done_with_divisions_configured(lane):
    """The gate must not block a run that genuinely checked everything."""
    l, store, fake, run_id = lane
    l.divisions = "Electi"
    _drive(l, store, run_id)
    assert store.get_run(run_id).stage is RunStage.DONE


# ------------------------------- the two scrape sources, which are not ranked


def _scrape_calls(fake):
    return [a for a in fake.calls if "problem_scraper.py" in a[1]]


def test_the_audit_reads_the_catalog_even_when_it_also_pages(lane):
    """The catalog is the only source with the limits; the paged table is the
    only source with division_access. A run granting divisions needs both."""
    l, store, fake, run_id = lane
    l.divisions = "Electi"
    _drive(l, store, run_id)

    audits = [a for a in _scrape_calls(fake) if "catalog-after" in " ".join(a)
              or "catalog-paged" in " ".join(a)]
    catalog = [a for a in audits if "--from-catalog" in a]
    paged = [a for a in audits if "--from-catalog" not in a]
    assert catalog, "the limits source was never read"
    assert paged, "the division source was never read"


def test_a_batch_with_no_divisions_pages_nothing(lane):
    """~41 page loads that would buy nothing: everything stage 8 reads is in the
    one-load catalog once there is no division claim to verify."""
    l, store, fake, run_id = lane
    l.divisions = ""
    _drive(l, store, run_id)
    after = [a for a in _scrape_calls(fake) if "catalog-after" in " ".join(a)]
    assert after and all("--from-catalog" in a for a in after)
    assert not [a for a in _scrape_calls(fake) if "catalog-paged" in " ".join(a)]


def test_the_audit_is_handed_the_file_that_carries_division_access(lane):
    """`report audit --char` skips its division check on catalog-sourced input,
    so a run with divisions must hand it the paged file or the check never runs."""
    l, store, fake, run_id = lane
    l.divisions = "Electi"
    _drive(l, store, run_id)
    (report_call,) = [a for a in fake.calls if "report.py" in a[1]]
    assert "catalog-paged.json" in " ".join(report_call)


def test_the_limits_are_read_from_the_catalog_not_the_paged_rows(lane):
    """The mutation that survived until the fake told the two sources apart.

    With divisions set, the paged scrape is the one handed to the audit — and it
    has no limit columns. A limits check reading *those* rows finds nothing to
    compare and reports L-2 forever, for exactly the runs that granted divisions.
    """
    l, store, fake, run_id = lane
    l.divisions = "Electi"
    fake.emit_limits = True
    _drive(l, store, run_id)

    assert store.get_run(run_id).stage is RunStage.DONE
    notes = [e["message"] for e in store.events(run_id, limit=999)]
    assert not [n for n in notes if "L-2" in n], \
        "the limits check read rows that structurally cannot carry limits"


def test_a_wrong_limit_still_fails_the_run_when_divisions_are_set(lane):
    """…and reading the right source has to keep finding real faults."""
    l, store, fake, run_id = lane
    l.divisions = "Electi"
    fake.emit_limits = True
    fake.catalog_limits = {SLUGS[0]: (5000, 262144)}
    _drive(l, store, run_id)

    assert store.get_run(run_id).status is RunStatus.FAILED
    assert any("L-1" in e["message"] for e in store.events(run_id, limit=999))


def test_a_stage_reporting_its_own_key_is_believed_over_the_name_map():
    """`batch run` now puts `key` on its item events. The display-name mapping
    existed only because it did not, so the reported value wins."""
    assert stage_key("something entirely new", "list-add") == "list-add"
    assert stage_key("fixmdx (subtasks)", None) == "fixmdx"       # fallback still works
    assert stage_key("fixmdx (subtasks)", "") == "fixmdx"         # empty is not an answer


def test_an_unnameable_stage_is_still_never_skipped():
    """The safe direction, under both sources. Re-running a stage costs time;
    skipping one that never ran costs correctness."""
    assert stage_key("a stage from the future", None) is None


def test_stage_progress_prefers_the_reported_key():
    done, failed = stage_progress([
        {"event": "item", "id": "renamed upstream", "key": "metadata", "ok": True},
        {"event": "item", "id": "also renamed", "key": "division", "ok": False},
    ])
    assert done == ["metadata"]
    assert failed == "division"


# ------------------------------------- did the problems actually join the list


def _list_item(slug, status="added", ok=True):
    return {"event": "item", "tool": "list", "op": "add", "id": slug, "ok": ok,
            "status": status}


def test_a_slug_the_list_step_never_mentioned_is_an_error():
    """The de-dupe used to skip a slug silently and the audit never looks at list
    membership, so this is the only place it can be caught."""
    found = list_landed(["a", "b"], [_list_item("a")])
    assert [(f.check, f.slug) for f in errors(found)] == [("LI-1", "b")]


def test_a_slug_that_did_not_join_is_an_error():
    found = list_landed(["a"], [_list_item("a", "not_found", ok=False)])
    assert [f.check for f in errors(found)] == ["LI-2"]
    assert "not_found" in found[0].message


def test_already_present_warns_rather_than_fails():
    """Expected on a re-run; surprising for a set this run just uploaded, since
    the de-dupe matches a slug against a whole title."""
    found = list_landed(["a"], [_list_item("a", "already")])
    assert [f.severity for f in found] == [Severity.WARN]
    assert not errors(found)


def test_a_clean_list_step_says_nothing():
    assert list_landed(["a", "b"], [_list_item("a"), _list_item("b")]) == []


def test_an_absent_event_stream_is_reported_rather_than_passed():
    """No items means the child events did not arrive, not that the list is fine."""
    found = list_landed(["a"], [{"event": "item", "tool": "batch", "op": "run",
                                 "id": "list add", "key": "list-add", "ok": True}])
    assert [f.check for f in found] == ["LI-0"]
    assert "could not be verified" in found[0].message


def test_a_quietly_skipped_problem_fails_the_run(lane):
    """End to end: the chore stage reads the stream and refuses to advance."""
    l, store, fake, run_id = lane
    l.list_url = "https://www.electicode.com/c/9/manage"
    fake.chore_plan = [("metadata", "metadata"), ("list add", "list-add")]
    fake.list_items = {SLUGS[0]: "added"}          # the second slug is never mentioned
    _drive(l, store, run_id)

    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "did not join the contest list" in run.error
    assert any("LI-1" in e["message"] for e in store.events(run_id, limit=999))


def test_a_list_step_that_reported_everything_lets_the_run_continue(lane):
    l, store, fake, run_id = lane
    l.list_url = "https://www.electicode.com/c/9/manage"
    fake.chore_plan = [("metadata", "metadata"), ("list add", "list-add")]
    fake.list_items = {s: "added" for s in SLUGS}
    _drive(l, store, run_id)
    assert store.get_run(run_id).stage is RunStage.DONE
