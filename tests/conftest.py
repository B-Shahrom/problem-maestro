import hashlib, json, zipfile
from pathlib import Path

import pytest

SLUGS = ["edu-arrays-running-max", "edu-arrays-largest-gap"]


def _zip(path: Path, slug: str, *, extra: dict[str, bytes] | None = None, root: str | None = None) -> None:
    with zipfile.ZipFile(path, "w") as z:
        r = root or slug
        z.writestr(f"{r}/problem_statement.mdx", "\\textbf{Legend}")
        z.writestr(f"{r}/solution.cpp", "int main(){}")
        z.writestr(f"{r}/checker.cpp", "// ncmp")
        z.writestr(f"{r}/testset/input_s0_idx0.txt", "1\n")
        z.writestr(f"{r}/testset/input_s1_idx0.txt", "2\n")
        for name, data in (extra or {}).items():
            z.writestr(name, data)


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


CHAR = """# Characteristics — edu-arrays-20260725

_A set._

---

## General

| idx | slug | title | languages | group | tests | subtasks | checker | TL | ML |
|-----|------|-------|-----------|-------|-------|----------|---------|-----|------|
| 1 | edu-arrays-running-max | Running Maximum | EN, RU | easy | 41 (2+39) | none | ncmp (native) | 1 s | 256 MB |
| 2 | edu-arrays-largest-gap | Largest Gap | EN, RU | medium | 47 (2+45) | none | ncmp (native) | 1 s | 256 MB |

**TOTAL problems:** 2

---

## Suggested tags

1. implementation, arrays
2. observation, arrays

---

## Notes worth flagging

[none]
"""


@pytest.fixture
def set_dir(tmp_path: Path) -> Path:
    d = tmp_path / "edu-arrays-20260725"
    d.mkdir()
    problems = []
    for i, slug in enumerate(SLUGS, 1):
        z = d / f"{slug}.zip"
        _zip(z, slug)
        problems.append({
            "idx": i, "slug": slug, "title": ["Running Maximum", "Largest Gap"][i - 1],
            "archive": {"filename": z.name, "sha256": _sha(z), "bytes": z.stat().st_size},
            "tests_archive": None,
            "components": {"statement": True, "solution": True, "checker": True,
                           "testset": True, "validator": False},
            "languages": ["EN", "RU"],
            "group": ["easy", "medium"][i - 1],
            "tags": [["implementation", "arrays"], ["observation", "arrays"]][i - 1],
            "checker": {"kind": "native", "name": "ncmp", "polygon_id": "std::ncmp.cpp"},
            "subtasks": [],
            "tests": {"samples": 2, "main": 39, "total": 41, "by_group": {"s0": 2, "s1": 39}},
            "limits": {"time_limit_s": 1, "memory_limit_mb": 256},
            "samples_in_testset": True, "seed": "abc123", "notes": None,
        })
    (d / "characteristics.md").write_text(CHAR, encoding="utf-8")
    (d / "PREFLIGHT_REPORT.md").write_text("# ok", encoding="utf-8")
    (d / "MANIFEST.json").write_text(json.dumps({
        "schema_version": "1.0",
        "set": {"name": d.name, "problem_count": 2, "generated_at": "2026-07-25T14:03:11Z",
                "delivery": "full", "languages_default": ["EN", "RU"]},
        "problems": problems,
        "characteristics": {"filename": "characteristics.md",
                            "sha256": _sha(d / "characteristics.md"), "spec_version": "1.0"},
        "preflight": {"status": "pass", "report": "PREFLIGHT_REPORT.md",
                      "checks_run": 25, "checks_failed": 0, "waivers": [], "spec_version": "1.0"},
    }, indent=2), encoding="utf-8")
    return d


@pytest.fixture
def rewrite(set_dir: Path):
    """Mutate the manifest and refresh dependent checksums."""
    def _rw(fn):
        m = json.loads((set_dir / "MANIFEST.json").read_text())
        fn(m)
        (set_dir / "MANIFEST.json").write_text(json.dumps(m), encoding="utf-8")
        return m
    return _rw
