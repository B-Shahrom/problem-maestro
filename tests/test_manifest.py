import json

from maestro.checks import Severity, errors, ok
from maestro.manifest import validate
from tests.conftest import SLUGS, _sha, _zip


def codes(f):
    return {x.check for x in f}


def test_clean_set_passes(set_dir):
    assert ok(validate(set_dir)), [str(x) for x in validate(set_dir)]


def test_missing_manifest_is_not_ingestible(set_dir):
    (set_dir / "MANIFEST.json").unlink()
    f = validate(set_dir)
    assert "M-0" in codes(f) and not ok(f)


def test_unknown_schema_halts_without_best_effort(set_dir, rewrite):
    rewrite(lambda m: m.__setitem__("schema_version", "9.9"))
    f = validate(set_dir)
    assert codes(f) == {"M-1"}  # stops immediately rather than reporting downstream noise


def test_problem_count_mismatch(set_dir, rewrite):
    rewrite(lambda m: m["set"].__setitem__("problem_count", 7))
    assert "M-4" in codes(validate(set_dir))


def test_bad_slug_shapes(set_dir, rewrite):
    rewrite(lambda m: m["problems"][0].__setitem__("slug", "Edu_Bad--Slug"))
    assert "M-5" in codes(validate(set_dir))


def test_slug_ending_in_tests_is_reserved(set_dir, rewrite):
    rewrite(lambda m: m["problems"][0].__setitem__("slug", "edu-arrays-tests"))
    assert "M-5" in codes(validate(set_dir))


def test_checksum_mismatch_detected(set_dir, rewrite):
    rewrite(lambda m: m["problems"][0]["archive"].__setitem__("sha256", "0" * 64))
    assert "M-2" in codes(validate(set_dir))


def test_orphan_zip_means_manifest_does_not_describe_the_folder(set_dir):
    _zip(set_dir / "edu-stray.zip", "edu-stray")
    assert "M-3" in codes(validate(set_dir))


def test_characteristics_checksum_guarded(set_dir):
    (set_dir / "characteristics.md").write_text("tampered", encoding="utf-8")
    assert "M-6" in codes(validate(set_dir))


def test_out_of_vocabulary_tag_halts(set_dir, rewrite):
    rewrite(lambda m: m["problems"][0].__setitem__("tags", ["frequency-array"]))
    f = validate(set_dir)
    assert "M-9" in codes(f)
    assert "silently" in next(x for x in f if x.check == "M-9").message


def test_extra_tags_can_be_allowed_deliberately(set_dir, rewrite):
    rewrite(lambda m: m["problems"][0].__setitem__("tags", ["academy exam"]))
    assert "M-9" in codes(validate(set_dir))
    assert ok(validate(set_dir, extra_tags={"academy exam"}))


def test_subtask_points_must_sum_to_100(set_dir, rewrite):
    rewrite(lambda m: m["problems"][0].__setitem__("subtasks", [
        {"id": 0, "points": 0, "depends_on": []},
        {"id": 1, "points": 60, "depends_on": [0]},
    ]))
    assert "M-10" in codes(validate(set_dir))


def test_sample_subtask_must_be_worth_zero(set_dir, rewrite):
    rewrite(lambda m: m["problems"][0].__setitem__("subtasks", [
        {"id": 0, "points": 10, "depends_on": []},
        {"id": 1, "points": 100, "depends_on": [0]},
    ]))
    assert "M-10" in codes(validate(set_dir))


def test_failed_preflight_blocks_ingest(set_dir, rewrite):
    def bad(m):
        m["preflight"]["status"] = "fail"
        m["preflight"]["checks_failed"] = 2
    rewrite(bad)
    assert "M-13" in codes(validate(set_dir))


def test_waivers_warn_for_human_acknowledgement(set_dir, rewrite):
    rewrite(lambda m: m["preflight"].__setitem__("waivers", ["PF-22: approved"]))
    f = validate(set_dir)
    assert ok(f)  # a waiver is not an error
    assert any(x.check == "M-13" and "acknowledgement" in x.message for x in f)


def test_partial_delivery_warns_to_use_the_update_path(set_dir, rewrite):
    rewrite(lambda m: m["set"].__setitem__("delivery", "partial"))
    assert any(x.check == "M-14" for x in validate(set_dir))


# ---------------------------------------------------------------- M-12

def test_binary_in_archive_rejected(set_dir, rewrite):
    slug = "edu-arrays-running-max"
    z = set_dir / f"{slug}.zip"
    _zip(z, slug, extra={f"{slug}/solution.exe": b"MZ\x90\x00binary"})
    rewrite(lambda m: m["problems"][0]["archive"].update(
        {"sha256": _sha(z), "bytes": z.stat().st_size}))
    f = validate(set_dir)
    assert "M-12" in codes(f)
    assert any("binary" in x.message for x in f)


def test_wrong_root_folder_rejected(set_dir, rewrite):
    slug = "edu-arrays-running-max"
    z = set_dir / f"{slug}.zip"
    _zip(z, slug, root="not-the-slug")
    rewrite(lambda m: m["problems"][0]["archive"].update(
        {"sha256": _sha(z), "bytes": z.stat().st_size}))
    assert "M-12" in codes(validate(set_dir))


def test_uppercase_entry_rejected(set_dir, rewrite):
    slug = "edu-arrays-running-max"
    z = set_dir / f"{slug}.zip"
    _zip(z, slug, extra={f"{slug}/README.md": b"x"})
    rewrite(lambda m: m["problems"][0]["archive"].update(
        {"sha256": _sha(z), "bytes": z.stat().st_size}))
    assert "M-12" in codes(validate(set_dir))


def test_short_file_reports_one_cause_not_three(set_dir):
    """A truncated archive must not also report a checksum and a zip-parse failure.

    Ingest tells "still copying" from "corrupt" by looking at which findings appear,
    so derivative failures from the same root cause would drown the signal.
    """
    z = set_dir / "edu-arrays-running-max.zip"
    z.write_bytes(z.read_bytes()[:40])
    f = [x for x in validate(set_dir) if x.slug == "edu-arrays-running-max"]
    assert len(f) == 1
    assert "bytes on disk" in f[0].message


# ------------------------------------------ the limits nobody was checking


def _with_limits(set_dir, slug, **limits):
    m = json.loads((set_dir / "MANIFEST.json").read_text())
    for p in m["problems"]:
        if p["slug"] == slug:
            p["limits"] = {**p.get("limits", {}), **limits}
    (set_dir / "MANIFEST.json").write_text(json.dumps(m))
    return validate(set_dir)


def test_a_non_default_limit_without_a_rationale_is_refused(set_dir):
    """An intentional bump and a typo are the same edit without one."""
    found = _with_limits(set_dir, SLUGS[0], time_limit_s=2, limits_rationale=None)
    assert any(f.check == "M-15" and f.slug == SLUGS[0] for f in errors(found))


def test_a_non_default_memory_limit_needs_one_too(set_dir):
    found = _with_limits(set_dir, SLUGS[0], memory_limit_mb=512, limits_rationale="")
    assert any(f.check == "M-15" for f in errors(found))


def test_a_stated_rationale_satisfies_it(set_dir):
    found = _with_limits(set_dir, SLUGS[0], time_limit_s=2, measured_worst_s=0.81,
                         limits_rationale="reference worst case 0.81s, 2.5x margin")
    assert not [f for f in found if f.check == "M-15"]


def test_the_default_limits_need_no_justification(set_dir):
    found = _with_limits(set_dir, SLUGS[0], time_limit_s=1, memory_limit_mb=256,
                         limits_rationale=None)
    assert not [f for f in found if f.check == "M-15"]


def test_a_reference_that_does_not_fit_its_own_limit_is_an_error(set_dir):
    """Its own measurement says the intended solution TLEs."""
    found = _with_limits(set_dir, SLUGS[0], time_limit_s=1, measured_worst_s=1.2)
    bad = [f for f in errors(found) if f.check == "M-16"]
    assert bad and "TLEs" in bad[0].message


def test_a_thin_margin_warns_rather_than_blocks(set_dir):
    """0.7s under 1s passes today and fails on a slower judge — worse than a
    hard failure, because it looks fine until it does not."""
    found = _with_limits(set_dir, SLUGS[0], time_limit_s=1, measured_worst_s=0.7)
    thin = [f for f in found if f.check == "M-16"]
    assert thin and thin[0].severity is Severity.WARN
    assert "1.4x margin" in thin[0].message
    assert not errors(found)


def test_a_healthy_margin_says_nothing(set_dir):
    found = _with_limits(set_dir, SLUGS[0], time_limit_s=2, measured_worst_s=0.81,
                         limits_rationale="2.5x margin")
    assert not [f for f in found if f.check == "M-16"]


def test_no_measurement_means_no_margin_finding(set_dir):
    """Absent is not the same as bad — the field is optional in the schema."""
    found = _with_limits(set_dir, SLUGS[0], time_limit_s=1, measured_worst_s=None)
    assert not [f for f in found if f.check == "M-16"]
