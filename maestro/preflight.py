"""Cross-check `MANIFEST.json` against what the Middleman will actually import.

Every other ingest check reads the archives through Maestro's own eyes. That is a
second parser, and the one that will be wrong: the Middleman's `zip_parser` is
what decides what gets created on Polygon, so a disagreement between the two is
resolved in its favour by definition.

`POST /api/parse` runs that parser as a dry run — no Polygon calls, no
credentials — which turns "Maestro believes this archive contains 41 tests" into
"the importer reports 41 tests". Running it at the gate means a manifest that
lies about its own archives is caught before any Polygon time is spent, rather
than three stages later as a build failure with an unrelated-looking cause.

Optional by construction. `inspect()` takes the parser as an argument and skips
these checks when it is absent, so ingest still works with the Middleman down.
"""

from __future__ import annotations

from typing import Any

from .checks import Finding, Severity

#: The manifest's `components` keys, and the field each maps to in a parse result.
_COMPONENTS = {
    "checker": "hasChecker",
    "solution": "hasSolution",
    "validator": "hasValidator",
}


def compare(manifest: dict[str, Any], parsed: dict[str, Any]) -> list[Finding]:
    """Check a manifest against a `/api/parse` response. Checks are P-1…P-6."""
    out: list[Finding] = []

    def err(check: str, msg: str, slug: str | None = None) -> None:
        out.append(Finding(check, Severity.ERROR, msg, slug))

    def warn(check: str, msg: str, slug: str | None = None) -> None:
        out.append(Finding(check, Severity.WARN, msg, slug))

    # P-1 — an archive the importer cannot read will not import at all. This is
    # the check that most justifies the round trip: nothing Maestro reads locally
    # can tell it that the *importer* will choke.
    for e in parsed.get("parseErrors") or []:
        err("P-1", f"the importer could not parse {e.get('file')}: {e.get('error')}")

    problems = {p.get("slug"): p for p in parsed.get("problems") or [] if p.get("slug")}
    declared = {p.get("slug"): p for p in manifest.get("problems") or [] if p.get("slug")}

    # P-2 — both directions. A manifest slug the importer does not produce means
    # nothing will be created for it; an importer slug the manifest omits means
    # something will be created that no later stage knows how to chore or audit.
    for missing in sorted(set(declared) - set(problems)):
        err("P-2", "declared in the manifest but the importer produces no such problem "
                   "(check the archive's internal folder name)", missing)
    for extra in sorted(set(problems) - set(declared)):
        err("P-2", "the importer would create this, but it is not in the manifest", extra)

    for slug in sorted(set(declared) & set(problems)):
        d, p = declared[slug], problems[slug]

        # P-3 — the count the manifest promises against the count that will exist.
        want = (d.get("tests") or {}).get("total")
        got = p.get("testCount")
        if want is not None and got is not None and want != got:
            err("P-3", f"manifest declares {want} test(s), the importer finds {got}", slug)

        # P-4 — a missing checker or solution is a build failure minutes later.
        components = d.get("components") or {}
        for key, field in _COMPONENTS.items():
            if key not in components:
                continue
            if bool(components[key]) != bool(p.get(field)):
                err("P-4", f"manifest says {key}={components[key]!r}, the importer finds "
                           f"{p.get(field)!r}", slug)

        # P-5 — no parsed statement means there is nothing to show, and the
        # problem imports as an empty shell that still builds and verifies.
        langs = p.get("languages") or []
        if not langs:
            err("P-5", "the importer parsed no statement language — this would import "
                       "with an empty statement", slug)
        elif (n := len(d.get("languages") or [])) and n != len(langs):
            # Identities are not comparable: the manifest uses `EN`/`RU` while the
            # parser reports `english`/`russian`. The count is, and a difference is
            # worth surfacing without pretending to know which one is right.
            warn("P-5", f"manifest lists {n} language(s), the importer parsed "
                        f"{len(langs)} ({', '.join(sorted(langs))})", slug)

        # P-6 — a tests-only pack appends to a base problem instead of creating
        # one. If the manifest expected a whole problem here, the statement,
        # checker and solution are all silently absent.
        if p.get("testsOnly"):
            err("P-6", "the importer reads this archive as a tests-only pack, so it would "
                       "append tests to an existing problem rather than create one", slug)

    return out
