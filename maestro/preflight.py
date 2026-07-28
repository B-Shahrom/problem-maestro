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


def limits_landed(manifest: dict[str, Any], rows: list[dict[str, Any]]) -> list[Finding]:
    """Did the authored time and memory limits survive the trip? Check L-1…L-2.

    This is the far end of the chain that `polygon_lane` starts by sending the
    limits on import. Sending them is necessary but not sufficient — the value
    passes through the Middleman, `problem.updateInfo`, a Polygon package build,
    a download, and an ElectiCode upload before anyone sees it, and no stage in
    between reports what it applied.

    ElectiCode's own catalog does, which makes this the only place the round trip
    can be closed. It is worth closing because a wrong limit is otherwise
    permanently invisible: it fails no import, no build, no verify and no audit,
    and the platform renders both fields read-only.

    Rows without the fields yield a single finding saying so rather than silence —
    "could not check" and "checked, fine" must not look the same.
    """
    out: list[Finding] = []
    declared = {p.get("slug"): (p.get("limits") or {})
                for p in manifest.get("problems") or [] if p.get("slug")}
    by_id = {r.get("s3_id") or r.get("id"): r for r in rows}

    checked = 0
    for slug, limits in sorted(declared.items()):
        row = by_id.get(slug)
        if row is None:
            continue  # absence is stage 6.5's finding, not this one
        # The catalog reports milliseconds and kilobytes; the manifest is authored
        # in seconds and megabytes.
        for label, want, got, scale, unit in (
            ("time limit", limits.get("time_limit_s"), row.get("time_limit_ms"), 1000, "ms"),
            ("memory limit", limits.get("memory_limit_mb"), row.get("memory_limit_kb"), 1024, "KB"),
        ):
            if want is None or got is None:
                continue
            checked += 1
            if int(round(float(want) * scale)) != int(got):
                out.append(Finding(
                    "L-1", Severity.ERROR,
                    f"{label}: authored {want:g} ({int(round(float(want) * scale))} {unit}), "
                    f"the platform has {int(got)} {unit}", slug))

    if declared and not checked:
        out.append(Finding(
            "L-2", Severity.WARN,
            "the catalog carried no time_limit_ms/memory_limit_kb, so the authored "
            "limits could not be verified. Neither scrape source carries the "
            "fields, so this cannot close until the Scraper exposes them — and "
            "nothing else checks them"))
    return out


def divisions_landed(expected: str, rows: list[dict[str, Any]],
                     slugs: list[str]) -> list[Finding]:
    """Did every problem get the division access the run asked for? Checks D-1…D-2.

    Maestro checks this itself rather than relying on `report audit --char
    --divisions`, because that check **skips itself** in the one state that most
    needs it. The tool drops the division check when no record carries
    `division_access` — reasonable for a catalog-sourced scrape, which never
    carries the field — but "no problem has division access" is also exactly what
    a failed division step looks like, and the skip is announced only on stderr
    while the exit code stays `0`.

    So the shape Maestro must avoid is: divisions never granted → every record
    empty → check skipped → audit clean → run marked done. Computing it here
    costs one comparison over data already in hand.
    """
    # Matched case-insensitively, but reported as the operator wrote it — an error
    # naming `electi` when the config says `Electi` sends them looking for a typo
    # that isn't there.
    wanted = [d.strip() for d in (expected or "").replace("\n", ",").split(",") if d.strip()]
    if not wanted:
        return []

    by_id = {r.get("s3_id") or r.get("id"): r for r in rows}
    out: list[Finding] = []
    seen_field = False
    for slug in slugs:
        row = by_id.get(slug)
        if row is None:
            continue  # absence is stage 6.5's finding
        if "division_access" not in row:
            continue
        seen_field = True
        have = {d.strip().lower() for d in (row.get("division_access") or "").split(",") if d.strip()}
        if missing := [d for d in wanted if d.lower() not in have]:
            out.append(Finding("D-1", Severity.ERROR,
                               f"missing division access: {', '.join(missing)} "
                               f"(has {row.get('division_access') or 'none'})", slug))

    if not seen_field:
        # A catalog-sourced scrape omits the field entirely. That is not a
        # failure, but it must not read as a pass either.
        out.append(Finding("D-2", Severity.WARN,
                           "the scrape carried no division_access, so the requested divisions "
                           f"({expected}) could not be verified — re-check with a paged scrape"))
    return out


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
        else:
            # The Middleman now returns `languageCodes` in the manifest's own
            # vocabulary (`EN`/`RU`), so the two are directly comparable. Before
            # that they were not — it reported `english`/`russian` — and this
            # compared counts, which agrees whenever a set has the right *number*
            # of the wrong languages. Codes when they are offered, counts when
            # they are not, and the difference is stated rather than assumed.
            want = {str(x).strip().upper() for x in (d.get("languages") or [])}
            have = {str(x).strip().upper() for x in (p.get("languageCodes") or [])}
            if want and have:
                if absent := sorted(want - have):
                    err("P-5", f"manifest declares {', '.join(absent)}, which the importer "
                               f"does not parse (it finds {', '.join(sorted(have))})", slug)
                if surplus := sorted(have - want):
                    warn("P-5", f"the importer parses {', '.join(surplus)}, which the manifest "
                                f"does not declare", slug)
            elif want and len(want) != len(langs):
                warn("P-5", f"manifest lists {len(want)} language(s), the importer parsed "
                            f"{len(langs)} ({', '.join(sorted(langs))}) — compared by count "
                            f"because this response carried no languageCodes", slug)

        # P-6 — a tests-only pack appends to a base problem instead of creating
        # one. If the manifest expected a whole problem here, the statement,
        # checker and solution are all silently absent.
        if p.get("testsOnly"):
            err("P-6", "the importer reads this archive as a tests-only pack, so it would "
                       "append tests to an existing problem rather than create one", slug)

    return out
