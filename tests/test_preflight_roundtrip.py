"""Check `preflight.compare()` against the parser whose answer it trusts.

`compare()` reads a `/api/parse` response, and every field it reads is produced
by the Middleman's `zip_parser`. The tests in `test_preflight.py` feed it
hand-written responses, which proves the comparison logic but not the field
mapping — and the mapping is where this can silently rot: a renamed key or a
changed sense of `testsOnly` would make the gate pass everything.

So this builds a response from the real parser, out of a real archive, and
checks that a manifest describing that archive compares clean — then perturbs
the manifest and checks each perturbation is caught.

Skipped when the Middleman checkout isn't on hand. Point `MAESTRO_MIDDLEMAN_REPO`
at it to run this locally.
"""

import os
import sys
from pathlib import Path

import pytest

from maestro.preflight import compare
from tests.conftest import _zip

_CANDIDATES = [os.environ.get("MAESTRO_MIDDLEMAN_REPO"),
               "/workspace/polygon-middleman",
               str(Path.home() / "polygon-middleman")]
SLUG = "edu-arrays-running-max"


@pytest.fixture(scope="module")
def zp():
    for c in _CANDIDATES:
        if c and (Path(c) / "backend" / "zip_parser.py").is_file():
            sys.path.insert(0, str(Path(c) / "backend"))
            try:
                import zip_parser
            except ImportError as e:
                pytest.skip(f"polygon-middleman found at {c} but not importable: {e}")
            return zip_parser
    pytest.skip("polygon-middleman checkout not found (set MAESTRO_MIDDLEMAN_REPO)")


def _response(zp, archives):
    """Mirror how `main.py` builds `/api/parse`'s response from the parser."""
    items = [zp.parse_zip(Path(a).read_bytes()) for a in archives]
    grouped = {}
    for p in items:
        key = zp.base_problem_slug(p["problemName"]) if p["testsOnly"] else p["problemName"]
        grouped.setdefault(key, []).append(p)
    problems = []
    for slug, group in grouped.items():
        m = zp.merge_parsed_group(group)
        problems.append({
            "slug": slug,
            "name": m.get("displayName") or slug,
            "testsOnly": m.get("testsOnly", False),
            "testCount": len(m["tests"]),
            "languages": list(m["languages"].keys()),
            "hasChecker": bool(m["checkerCode"]),
            "hasValidator": bool(m["validatorCode"]),
            "hasSolution": bool(m["solutionCode"]),
            "archiveCount": len(group),
        })
    return {"problems": problems, "parseErrors": []}


@pytest.fixture
def archive(tmp_path):
    z = tmp_path / f"{SLUG}.zip"
    _zip(z, SLUG)
    return z


@pytest.fixture
def truthful(zp, archive):
    """A manifest that describes the archive exactly as the parser reads it."""
    p = _response(zp, [archive])["problems"][0]
    return {"problems": [{
        "slug": p["slug"],
        "title": p["name"],
        "tests": {"total": p["testCount"]},
        "languages": ["EN"],
        "components": {"checker": p["hasChecker"], "solution": p["hasSolution"],
                       "validator": p["hasValidator"]},
    }]}


def test_a_truthful_manifest_compares_clean(zp, archive, truthful):
    assert compare(truthful, _response(zp, [archive])) == []


def test_the_parser_really_does_report_these_fields(zp, archive):
    """Guards against a rename making every check vacuously pass."""
    p = _response(zp, [archive])["problems"][0]
    assert p["slug"] == SLUG
    assert p["testCount"] == 2          # input_s0_idx0 + input_s1_idx0
    assert p["languages"] == ["english"]
    assert (p["hasChecker"], p["hasSolution"], p["hasValidator"]) == (True, True, False)
    assert p["testsOnly"] is False


@pytest.mark.parametrize("field,value,expect", [
    ("tests", {"total": 41}, "P-3"),
    ("components", {"checker": True, "solution": True, "validator": True}, "P-4"),
    ("languages", ["EN", "RU"], "P-5"),
    ("slug", "edu-arrays-running-maximum", "P-2"),
])
def test_each_disagreement_is_caught(zp, archive, truthful, field, value, expect):
    truthful["problems"][0][field] = value
    found = compare(truthful, _response(zp, [archive]))
    assert expect in {f.check for f in found}, found


def test_a_tests_only_pack_is_recognised(zp, tmp_path, truthful):
    """A pack with tests and nothing else appends instead of creating.

    Built to the parser's actual condition — no statement, checker, solution or
    validator, and at least one test — rather than to a guess about it, which is
    the whole reason this file talks to the real parser.
    """
    import zipfile

    z = tmp_path / f"{SLUG}-tests.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr(f"{SLUG}-tests/testset/input_s1_idx1.txt", "3\n")
        zf.writestr(f"{SLUG}-tests/testset/input_s1_idx2.txt", "4\n")

    p = _response(zp, [z])["problems"][0]
    assert p["testsOnly"] is True
    assert p["slug"] == SLUG, "a -tests pack must key to its base problem"
    found = compare(truthful, {"problems": [p], "parseErrors": []})
    assert "P-6" in {f.check for f in found}, found


def test_an_unreadable_archive_surfaces_as_a_parse_error(zp, tmp_path, truthful):
    bad = tmp_path / "broken.zip"
    bad.write_bytes(b"not a zip at all")
    with pytest.raises(Exception):
        zp.parse_zip(bad.read_bytes())
