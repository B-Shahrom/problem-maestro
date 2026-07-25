import json

from maestro.checks import Severity
from maestro.ingest import Verdict, inspect
from maestro.preflight import compare, limits_landed
from maestro.store import Store
from tests.conftest import SLUGS


def mf(set_dir):
    return json.loads((set_dir / "MANIFEST.json").read_text())


def codes(findings):
    return {f.check for f in findings}


def agreeing(manifest, **overrides):
    """A /api/parse response that matches the manifest, before any override."""
    problems = []
    for p in manifest["problems"]:
        problems.append({
            "slug": p["slug"],
            "name": p["title"],
            "testsOnly": False,
            "testCount": p["tests"]["total"],
            "languages": ["english", "russian"],
            "hasChecker": p["components"]["checker"],
            "hasValidator": p["components"]["validator"],
            "hasSolution": p["components"]["solution"],
            "extraSolutionCount": 0,
            "hasScoring": False,
            "groups": ["0", "1"],
            "archiveCount": 1,
        })
    out = {"problems": problems, "parseErrors": []}
    out.update(overrides)
    return out


def test_an_agreeing_parse_produces_nothing(set_dir):
    assert compare(mf(set_dir), agreeing(mf(set_dir))) == []


def test_an_unparseable_archive_is_reported(set_dir):
    r = agreeing(mf(set_dir), parseErrors=[{"file": "a.zip", "error": "not a zip"}])
    f = compare(mf(set_dir), r)
    assert "P-1" in codes(f) and "not a zip" in f[0].message


def test_a_slug_the_importer_does_not_produce(set_dir):
    """Usually the archive's internal folder name, which the manifest can't see."""
    m = mf(set_dir)
    r = agreeing(m)
    r["problems"][0]["slug"] = "edu-arrays-running-maximum"
    f = compare(m, r)
    assert codes(f) == {"P-2"}
    assert {x.slug for x in f} == {SLUGS[0], "edu-arrays-running-maximum"}


def test_a_test_count_disagreement(set_dir):
    m = mf(set_dir)
    r = agreeing(m)
    r["problems"][0]["testCount"] = 40
    f = compare(m, r)
    assert [x.check for x in f] == ["P-3"]
    assert "41" in f[0].message and "40" in f[0].message


def test_a_missing_checker(set_dir):
    m = mf(set_dir)
    r = agreeing(m)
    r["problems"][1]["hasChecker"] = False
    f = compare(m, r)
    assert [(x.check, x.slug) for x in f] == [("P-4", SLUGS[1])]


def test_no_parsed_statement_is_an_error(set_dir):
    """It imports, builds and verifies — as an empty shell."""
    m = mf(set_dir)
    r = agreeing(m)
    r["problems"][0]["languages"] = []
    f = compare(m, r)
    assert [x.check for x in f] == ["P-5"]
    assert f[0].severity is Severity.ERROR


def test_a_language_count_difference_only_warns(set_dir):
    """`EN`/`RU` against `english`/`russian` — the identities aren't comparable."""
    m = mf(set_dir)
    r = agreeing(m)
    r["problems"][0]["languages"] = ["english"]
    f = compare(m, r)
    assert [(x.check, x.severity) for x in f] == [("P-5", Severity.WARN)]


def test_a_tests_only_pack_where_a_problem_was_expected(set_dir):
    m = mf(set_dir)
    r = agreeing(m)
    r["problems"][0]["testsOnly"] = True
    f = compare(m, r)
    assert [x.check for x in f] == ["P-6"]


# ------------------------------------------------------------------ wiring


def test_ingest_fails_a_set_the_importer_disagrees_with(tmp_path, set_dir):
    def parser(archives):
        m = mf(set_dir)
        r = agreeing(m)
        r["problems"][0]["testCount"] = 3
        return r

    with Store(tmp_path / "m.db") as store:
        result = inspect(set_dir, store, parser=parser)
    assert result.verdict is Verdict.INVALID
    assert "P-3" in codes(result.findings)


def test_ingest_hands_the_parser_the_manifest_s_archives(tmp_path, set_dir):
    seen = []

    def parser(archives):
        seen.extend(a.name for a in archives)
        return agreeing(mf(set_dir))

    with Store(tmp_path / "m.db") as store:
        assert inspect(set_dir, store, parser=parser).verdict is Verdict.READY
    assert sorted(seen) == sorted(f"{s}.zip" for s in SLUGS)


def test_an_unreachable_importer_warns_rather_than_failing_the_set(tmp_path, set_dir):
    """The set may be fine and the service merely down."""
    def parser(archives):
        raise ConnectionRefusedError("connection refused")

    with Store(tmp_path / "m.db") as store:
        result = inspect(set_dir, store, parser=parser)
    assert result.verdict is Verdict.READY
    assert "P-0" in codes(result.findings)


def test_without_a_parser_nothing_changes(tmp_path, set_dir):
    with Store(tmp_path / "m.db") as store:
        result = inspect(set_dir, store)
    assert result.verdict is Verdict.READY
    assert not any(f.check.startswith("P-") for f in result.findings)


# ------------------------------------------------------------------- limits


def catalog_rows(manifest, **override):
    """What the ElectiCode catalog reports: milliseconds and kilobytes."""
    rows = []
    for p in manifest["problems"]:
        lim = p["limits"]
        rows.append({"s3_id": p["slug"],
                     "time_limit_ms": int(lim["time_limit_s"] * 1000),
                     "memory_limit_kb": int(lim["memory_limit_mb"] * 1024)})
    for k, v in override.items():
        rows[0][k] = v
    return rows


def test_matching_limits_produce_nothing(set_dir):
    m = mf(set_dir)
    assert limits_landed(m, catalog_rows(m)) == []


def test_a_wrong_time_limit_is_an_error(set_dir):
    """1s authored, 2s on the platform — invisible to every other check."""
    m = mf(set_dir)
    f = limits_landed(m, catalog_rows(m, time_limit_ms=2000))
    assert [(x.check, x.slug) for x in f] == [("L-1", SLUGS[0])]
    assert "1000 ms" in f[0].message and "2000 ms" in f[0].message


def test_a_wrong_memory_limit_is_an_error(set_dir):
    m = mf(set_dir)
    f = limits_landed(m, catalog_rows(m, memory_limit_kb=131072))
    assert [x.check for x in f] == ["L-1"]
    assert "memory limit" in f[0].message


def test_the_units_are_converted_not_compared_raw(set_dir):
    """The manifest is seconds and megabytes; the catalog is ms and KB."""
    m = mf(set_dir)
    assert limits_landed(m, catalog_rows(m, time_limit_ms=1)) != []
    assert limits_landed(m, catalog_rows(m, memory_limit_kb=256)) != []


def test_a_catalog_without_the_fields_says_so(set_dir):
    """"Could not check" and "checked, fine" must not look the same."""
    m = mf(set_dir)
    rows = [{"s3_id": p["slug"]} for p in m["problems"]]
    f = limits_landed(m, rows)
    assert [x.check for x in f] == ["L-2"]
    assert f[0].severity is Severity.WARN


def test_an_absent_problem_is_not_a_limits_finding(set_dir):
    """A slug missing from the catalog is stage 6.5's finding, not this one."""
    m = mf(set_dir)
    assert limits_landed(m, catalog_rows(m)[:1]) == []
