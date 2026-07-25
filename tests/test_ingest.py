import json
import shutil

import pytest

from maestro.ingest import Verdict, candidates, ingest, inspect, scan
from maestro.model import RunStage, RunStatus
from maestro.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        yield s


@pytest.fixture
def watch(tmp_path, set_dir):
    """A watched folder containing the good set."""
    w = tmp_path / "watch"
    w.mkdir()
    shutil.move(str(set_dir), str(w / set_dir.name))
    return w


def test_clean_set_ingests_and_seeds_the_run(watch, store):
    (res,) = scan(watch, store)
    assert res.verdict is Verdict.READY and res.run_id
    run = store.get_run(res.run_id)
    assert run.set_name == "edu-arrays-20260725"
    # A registered run must be where a lane will look for it, or it never starts.
    assert run.stage is RunStage.POLYGON
    assert run.status is RunStatus.RUNNING
    assert [p.slug for p in run.problems] == ["edu-arrays-running-max", "edu-arrays-largest-gap"]


def test_ingest_does_not_modify_the_set_folder(watch, store):
    d = watch / "edu-arrays-20260725"
    before = sorted(p.name for p in d.iterdir())
    ingest(d, store)
    assert sorted(p.name for p in d.iterdir()) == before


def test_second_scan_does_not_re_ingest(watch, store):
    scan(watch, store)
    (again,) = scan(watch, store)
    assert again.verdict is Verdict.ALREADY_INGESTED
    assert len(store.list_runs()) == 1


# ---------------------------------------------- incomplete vs invalid

def test_manifest_only_folder_is_incomplete_not_invalid(tmp_path, store):
    d = tmp_path / "watch" / "edu-x-20260725"
    d.mkdir(parents=True)
    res = inspect(d, store)
    assert res.verdict is Verdict.INCOMPLETE and res.retryable


def test_archive_still_copying_is_incomplete(watch, store):
    """A short file means a copy in flight — look again later, don't reject."""
    d = watch / "edu-arrays-20260725"
    z = d / "edu-arrays-running-max.zip"
    z.write_bytes(z.read_bytes()[: len(z.read_bytes()) // 2])
    res = inspect(d, store)
    assert res.verdict is Verdict.INCOMPLETE and res.retryable


def test_missing_archive_is_incomplete(watch, store):
    d = watch / "edu-arrays-20260725"
    (d / "edu-arrays-largest-gap.zip").unlink()
    assert inspect(d, store).verdict is Verdict.INCOMPLETE


def test_right_size_wrong_checksum_is_invalid_not_incomplete(watch, store):
    """A fully-copied but corrupt file will never become valid by waiting."""
    d = watch / "edu-arrays-20260725"
    z = d / "edu-arrays-running-max.zip"
    data = bytearray(z.read_bytes())
    data[-1] ^= 0xFF  # same length, different content
    z.write_bytes(bytes(data))
    res = inspect(d, store)
    assert res.verdict is Verdict.INVALID and not res.retryable


def test_failed_preflight_is_invalid_even_while_still_copying(watch, store):
    """One permanent error is decisive — a bad set doesn't heal when the copy ends."""
    d = watch / "edu-arrays-20260725"
    m = json.loads((d / "MANIFEST.json").read_text())
    m["preflight"]["status"] = "fail"
    m["preflight"]["checks_failed"] = 1
    (d / "MANIFEST.json").write_text(json.dumps(m))
    (d / "edu-arrays-largest-gap.zip").unlink()   # transient error alongside it
    assert inspect(d, store).verdict is Verdict.INVALID


def test_unparseable_manifest_is_treated_as_mid_write(tmp_path, store):
    d = tmp_path / "edu-y-20260725"
    d.mkdir()
    (d / "MANIFEST.json").write_text('{"schema_ver')
    assert inspect(d, store).verdict is Verdict.INCOMPLETE


def test_invalid_set_is_not_registered(watch, store):
    d = watch / "edu-arrays-20260725"
    m = json.loads((d / "MANIFEST.json").read_text())
    m["schema_version"] = "9.9"
    (d / "MANIFEST.json").write_text(json.dumps(m))
    res = ingest(d, store)
    assert res.verdict is Verdict.INVALID and res.run_id is None
    assert store.list_runs() == []


# ------------------------------------------------ characteristics gate

def test_characteristics_failure_blocks_ingest(watch, store):
    d = watch / "edu-arrays-20260725"
    text = (d / "characteristics.md").read_text()
    (d / "characteristics.md").write_text(text.replace("2. observation, arrays\n", ""))
    m = json.loads((d / "MANIFEST.json").read_text())
    import hashlib
    m["characteristics"]["sha256"] = hashlib.sha256(
        (d / "characteristics.md").read_bytes()).hexdigest()
    (d / "MANIFEST.json").write_text(json.dumps(m))
    res = ingest(d, store)
    assert res.verdict is Verdict.INVALID
    assert any(f.check == "C-2" for f in res.findings)
    assert store.list_runs() == []


def test_warnings_are_recorded_but_do_not_block(watch, store):
    d = watch / "edu-arrays-20260725"
    m = json.loads((d / "MANIFEST.json").read_text())
    m["preflight"]["waivers"] = ["PF-22: approved by hand"]
    (d / "MANIFEST.json").write_text(json.dumps(m))
    res = ingest(d, store)
    assert res.verdict is Verdict.READY
    assert any("acknowledgement" in e["message"] for e in store.events(res.run_id))


def test_candidates_ignores_loose_files(tmp_path):
    w = tmp_path / "w"
    (w / "a-set").mkdir(parents=True)
    (w / "stray.zip").write_bytes(b"x")
    assert [p.name for p in candidates(w)] == ["a-set"]


def test_scan_on_missing_watch_dir_is_quiet(tmp_path, store):
    assert scan(tmp_path / "nope", store) == []


# ------------------------------------------------- what actually got checked


def test_a_manifest_rejection_does_not_claim_the_rest_was_checked(watch, store):
    """The characteristics and importer checks are gated behind a clean manifest.

    Reporting only the findings would make a manifest-level rejection look like a
    complete account of what is wrong with the set, and the author would fix four
    things and be rejected again for a fifth nobody had looked at yet.
    """
    d = watch / "edu-arrays-20260725"
    m = json.loads((d / "MANIFEST.json").read_text())
    m["set"]["problem_count"] = 99
    (d / "MANIFEST.json").write_text(json.dumps(m))

    res = inspect(d, store)
    assert res.verdict is Verdict.INVALID
    assert res.checked == frozenset({"manifest"})


def test_a_clean_set_records_the_characteristics_check_as_run(watch, store):
    res = inspect(watch / "edu-arrays-20260725", store)
    assert res.verdict is Verdict.READY
    assert res.checked == frozenset({"manifest", "characteristics"}), \
        "no parser was given, so the importer pre-flight must not be claimed"


def test_an_unreachable_importer_is_not_a_pre_flight(watch, store):
    """Empty findings are produced by agreement and by unreachability alike."""
    def down(_archives):
        raise ConnectionError("connection refused")

    res = inspect(watch / "edu-arrays-20260725", store, parser=down)
    assert "importer" not in res.checked
    assert any(f.check == "P-0" for f in res.findings)


def test_a_reachable_importer_is_recorded_as_run(watch, store):
    def agrees(archives):
        return {"problems": [], "parseErrors": []}

    res = inspect(watch / "edu-arrays-20260725", store, parser=agrees)
    assert "importer" in res.checked


def test_the_run_registered_by_ingest_carries_what_was_checked(watch, store):
    res = ingest(watch / "edu-arrays-20260725", store)
    assert res.run_id and res.checked == frozenset({"manifest", "characteristics"})
