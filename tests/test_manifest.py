import json

from maestro.checks import ok
from maestro.manifest import validate
from tests.conftest import _sha, _zip


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
