import io
import json
import zipfile

import pytest

from maestro.model import ProblemStage, ProblemStatus, RunStage, RunStatus
from maestro.polygon import Action, PolygonClient
from maestro.polygon_lane import PolygonLane
from maestro.store import Store
from tests.conftest import SLUGS


def pkg_bytes(slug: str, with_exe: bool = True) -> bytes:
    """A Polygon standard package: flat root, one problem, Windows binaries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("problem.xml", "<problem/>")
        z.writestr("check.cpp", "// checker")
        z.writestr("solutions/solution.cpp", "int main(){}")
        z.writestr("tests/01", "1\n")
        z.writestr("statements/english/problem.tex", "\\begin{problem}")
        if with_exe:
            z.writestr("check.exe", b"MZ\x90\x00")
            z.writestr("solutions/solution.exe", b"MZ\x90\x00")
    return buf.getvalue()


class FakeMiddleman:
    """Scripted Middleman. `script[slug]` is a queue of verify-status bodies."""

    def __init__(self, script: dict[str, list], *, download_status: int = 200):
        self.script = script
        self.download_status = download_status
        self.jobs: dict[str, str] = {}
        self.submits: list[list[str]] = []
        self._n = 0

    def __call__(self, method, url, body):
        if method == "POST" and url.endswith("/api/import-problem"):
            self.submits.append(body["files"])
            self._n += 1
            job = f"job{self._n}"
            slug = self._slug_of(body["files"][0])
            self.jobs[job] = slug
            return 202, json.dumps({"jobId": job, "state": "running",
                                    "problems": [], "parseErrors": []}).encode()
        if "/api/verify-status/" in url:
            job = url.rsplit("/", 1)[-1]
            if job not in self.jobs:
                return 404, json.dumps({"detail": f"Unknown jobId: {job}"}).encode()
            slug = self.jobs[job]
            q = self.script[slug]
            body_ = q.pop(0) if len(q) > 1 else q[0]
            return 200, json.dumps(body_).encode()
        if "/api/download-package/" in url:
            if self.download_status != 200:
                return self.download_status, b"No READY package for this problem yet."
            job = url.split("/api/download-package/")[1].split("?")[0]
            return 200, pkg_bytes(self.jobs[job])
        raise AssertionError(f"unexpected {method} {url}")

    @staticmethod
    def _slug_of(path: str) -> str:
        return path.replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".zip")


def verify(slug, code, action, *, vcode=None, vaction=None, pid=563710, pkg=1425709):
    p = {"slug": slug, "errorCode": code, "clientAction": action, "problemId": pid}
    if vcode:
        p["verify"] = {"state": "X", "code": vcode, "clientAction": vaction, "packageId": pkg}
    return {"jobId": "j", "state": "running", "problems": [p], "parseErrors": []}


READY = lambda s: verify(s, "IMPORTED", "proceed", vcode="VERIFY_READY", vaction="success")   # noqa: E731
RUNNING = lambda s: verify(s, "IMPORTED", "proceed", vcode="VERIFY_RUNNING", vaction="wait")  # noqa: E731
IMPORTED = lambda s: verify(s, "IMPORTED", "proceed")                                          # noqa: E731


@pytest.fixture
def lane(tmp_path, set_dir, watch=None):
    store = Store(tmp_path / "m.db")
    from maestro.ingest import ingest
    run_id = ingest(set_dir, store).run_id
    store.set_run(run_id, stage=RunStage.POLYGON, status=RunStatus.RUNNING)

    def build(script, **kw):
        fake = FakeMiddleman(script, **kw)
        return PolygonLane(store, PolygonClient(transport=fake), tmp_path / "work"), fake

    yield store, run_id, build
    store.close()


def drive(lane_obj, run_id, passes=12):
    reports = []
    for _ in range(passes):
        r = lane_obj.step(run_id)
        reports.append(r)
        if r.run_advanced_to or lane_obj.store.get_run(run_id).status is RunStatus.FAILED:
            break
    return reports


def test_happy_path_reaches_upload(lane):
    store, run_id, build = lane
    ln, fake = build({s: [IMPORTED(s), READY(s)] for s in SLUGS})
    drive(ln, run_id)
    run = store.get_run(run_id)
    assert run.stage is RunStage.UPLOAD
    assert all(p.stage is ProblemStage.SHAPED and p.status is ProblemStatus.OK
               for p in run.problems)


def test_one_job_per_problem(lane):
    store, run_id, build = lane
    ln, fake = build({s: [READY(s)] for s in SLUGS})
    drive(ln, run_id)
    assert len(fake.submits) == 2
    assert all(len(files) == 1 for files in fake.submits)


def test_identity_map_is_recorded(lane):
    store, run_id, build = lane
    ln, _ = build({s: [READY(s)] for s in SLUGS})
    drive(ln, run_id)
    p = store.problems(run_id)[0]
    assert p.polygon_problem_id == 563710 and p.polygon_package_id == 1425709


def test_extracted_layout_is_what_the_uploader_wants(lane):
    store, run_id, build = lane
    ln, _ = build({s: [READY(s)] for s in SLUGS})
    drive(ln, run_id)
    parent = ln.upload_dir(run_id)
    assert sorted(p.name for p in parent.iterdir()) == sorted(SLUGS)
    one = parent / SLUGS[0]
    assert (one / "problem.xml").is_file() and (one / "tests" / "01").is_file()


def test_windows_binaries_are_pruned(lane):
    store, run_id, build = lane
    ln, _ = build({s: [READY(s)] for s in SLUGS})
    drive(ln, run_id)
    assert list(ln.upload_dir(run_id).rglob("*.exe")) == []


def test_still_building_does_not_advance(lane):
    store, run_id, build = lane
    ln, _ = build({s: [RUNNING(s)] for s in SLUGS})
    for _ in range(3):
        ln.step(run_id)
    run = store.get_run(run_id)
    assert run.stage is RunStage.POLYGON
    assert all(p.stage is ProblemStage.IMPORTED for p in run.problems)


def test_verify_failure_quarantines_that_problem_only(lane):
    store, run_id, build = lane
    bad, good = SLUGS
    ln, _ = build({
        bad: [verify(bad, "IMPORTED", "proceed", vcode="VERIFY_FAILED", vaction="halt")],
        good: [READY(good)],
    })
    drive(ln, run_id)
    run = store.get_run(run_id)
    states = {p.slug: p.status for p in run.problems}
    assert states[bad] is ProblemStatus.QUARANTINED
    assert states[good] is ProblemStatus.OK
    assert run.stage is RunStage.UPLOAD  # the survivor still proceeds


def test_quarantined_problem_is_absent_from_the_upload_folder(lane):
    """Exclusion is structural — no filter to remember, nothing to forget."""
    store, run_id, build = lane
    bad, good = SLUGS
    ln, _ = build({
        bad: [verify(bad, "IMPORTED", "proceed", vcode="VERIFY_FAILED", vaction="halt")],
        good: [READY(good)],
    })
    drive(ln, run_id)
    assert [p.name for p in ln.upload_dir(run_id).iterdir()] == [good]


def test_every_problem_failing_fails_the_run(lane):
    store, run_id, build = lane
    ln, _ = build({s: [verify(s, "IMPORTED", "proceed", vcode="VERIFY_FAILED", vaction="halt")]
                   for s in SLUGS})
    drive(ln, run_id)
    run = store.get_run(run_id)
    assert run.status is RunStatus.FAILED
    assert "no problems survived" in [e["message"] for e in store.events(run_id)][-1]


def test_lost_job_resubmits_instead_of_failing(lane):
    """The Middleman restarting must not kill a batch."""
    store, run_id, build = lane
    ln, fake = build({s: [READY(s)] for s in SLUGS})
    ln.step(run_id)                      # submit
    fake.jobs.clear()                    # middleman restarted, registry gone
    r = ln.step(run_id)
    assert set(r.actions.values()) == {Action.RESUBMIT}
    assert all(p.polygon_job_id is None for p in store.problems(run_id))
    drive(ln, run_id)
    assert store.get_run(run_id).stage is RunStage.UPLOAD


def test_step_failed_retries_then_quarantines_at_the_cap(lane):
    store, run_id, build = lane
    slug = SLUGS[0]
    ln, _ = build({
        slug: [verify(slug, "STEP_FAILED", "retry")],
        SLUGS[1]: [READY(SLUGS[1])],
    })
    drive(ln, run_id, passes=20)
    p = next(x for x in store.problems(run_id) if x.slug == slug)
    assert p.status is ProblemStatus.QUARANTINED
    assert "content error" in p.error


def test_attempts_reset_when_a_stage_advances(lane):
    """A retry budget spent on import must not be inherited by the build."""
    store, run_id, build = lane
    slug = SLUGS[0]
    store.bump_attempt(run_id, slug)
    store.bump_attempt(run_id, slug)
    store.set_problem(run_id, slug, stage=ProblemStage.IMPORTED)
    assert next(p for p in store.problems(run_id) if p.slug == slug).attempts == 0


def test_download_not_ready_waits_rather_than_failing(lane):
    store, run_id, build = lane
    ln, _ = build({s: [READY(s)] for s in SLUGS}, download_status=404)
    for _ in range(4):
        r = ln.step(run_id)
    assert set(r.actions.values()) == {Action.WAIT}
    assert store.get_run(run_id).status is not RunStatus.FAILED


def test_lane_is_a_noop_outside_the_polygon_stage(lane):
    store, run_id, build = lane
    store.set_run(run_id, stage=RunStage.CHORES)
    ln, fake = build({s: [READY(s)] for s in SLUGS})
    assert ln.step(run_id).idle
    assert fake.submits == []


def test_progress_survives_a_restart(lane, tmp_path):
    store, run_id, build = lane
    ln, fake = build({s: [IMPORTED(s), READY(s)] for s in SLUGS})
    ln.step(run_id)
    ln.step(run_id)
    before = {p.slug: p.stage for p in store.problems(run_id)}
    store.close()

    with Store(tmp_path / "m.db") as reopened:
        assert {p.slug: p.stage for p in reopened.problems(run_id)} == before
        resumed = PolygonLane(reopened, PolygonClient(transport=fake), tmp_path / "work")
        drive(resumed, run_id)
        assert reopened.get_run(run_id).stage is RunStage.UPLOAD
