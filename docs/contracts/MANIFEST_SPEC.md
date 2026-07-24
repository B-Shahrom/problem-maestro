# MANIFEST_SPEC.md

**Schema version:** `1.0`
**Filename:** `MANIFEST.json`, at the root of the set folder.
**Purpose:** the completion sentinel. Its appearance means "every other file in this folder is
final." Its absence means "do not ingest," whatever else is on disk.

Adopted as proposed. Below is the exact schema, the write protocol that makes the sentinel
trustworthy, and the cross-checks Maestro should run before touching Polygon.

---

## 1. Write protocol

The sentinel is only as good as the ordering guarantee behind it, so the ordering is part of
the contract:

1. All archives are built, verified and written into the set folder.
2. `characteristics.md` is written.
3. `PREFLIGHT.md` is executed; `PREFLIGHT_REPORT.md` is written.
4. **Only if PREFLIGHT is a full pass**, `MANIFEST.json` is written — to a temp name in the
   same folder, then atomically renamed to `MANIFEST.json`.

Consequences Maestro can rely on:

- **A failed preflight means no manifest.** A set that fails self-check does not look ingestible.
  It looks unfinished, which is the correct signal.
- The atomic rename means Maestro never sees a partially written manifest — no need to poll
  for size stability or parse-retry.
- Nothing in the folder is modified after the manifest exists. A correction ships as a **new
  set folder** with a `-r2` suffix, never as an in-place edit. If you need in-place fixes,
  say so and I will add a `supersedes` field instead — but re-issuing the folder is safer for
  an unattended importer.

---

## 2. Schema

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Electicode problem-set manifest",
  "type": "object",
  "required": ["schema_version", "set", "problems", "characteristics", "preflight"],
  "additionalProperties": false,
  "properties": {
    "schema_version": { "const": "1.0" },

    "set": {
      "type": "object",
      "required": ["name", "problem_count", "generated_at", "delivery", "languages_default"],
      "additionalProperties": false,
      "properties": {
        "name":          { "type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*-\\d{8}(-r\\d+)?$" },
        "title":         { "type": "string" },
        "description":   { "type": "string" },
        "problem_count": { "type": "integer", "minimum": 1 },
        "generated_at":  { "type": "string", "format": "date-time",
                           "description": "UTC, ISO-8601 with Z, e.g. 2026-07-25T14:03:11Z" },
        "delivery":      { "enum": ["full", "partial"] },
        "languages_default": {
          "type": "array", "items": { "enum": ["EN", "RU", "TJ", "UZ"] }, "minItems": 1
        },
        "supersedes":    { "type": ["string", "null"],
                           "description": "set name this rerun replaces, or null" },
        "contract_version": { "type": "string", "description": "OUTPUT_CONTRACT.md version" },
        "authoring_model":  { "type": "string" }
      }
    },

    "problems": {
      "type": "array", "minItems": 1,
      "items": {
        "type": "object",
        "required": ["idx", "slug", "title", "archive", "tests_archive", "languages",
                     "group", "difficulty", "tags", "checker", "subtasks", "tests",
                     "limits", "samples_in_testset", "seed", "components"],
        "additionalProperties": false,
        "properties": {
          "idx":  { "type": "integer", "minimum": 1 },
          "slug": { "type": "string", "pattern": "^[a-z0-9]+(-[a-z0-9]+)*$", "maxLength": 80 },
          "title": { "type": "string", "minLength": 1 },
          "titles_translated": {
            "type": "object",
            "description": "title per non-EN language actually shipped; keys from RU/TJ/UZ",
            "additionalProperties": { "type": "string" }
          },

          "archive": {
            "type": ["object", "null"],
            "required": ["filename", "sha256", "bytes"],
            "additionalProperties": false,
            "properties": {
              "filename": { "type": "string", "pattern": "^[a-z0-9-]+\\.zip$" },
              "sha256":   { "type": "string", "pattern": "^[a-f0-9]{64}$" },
              "bytes":    { "type": "integer", "minimum": 1 }
            }
          },
          "tests_archive": {
            "type": ["object", "null"],
            "description": "null when no -tests archive was produced (the normal case)",
            "required": ["filename", "sha256", "bytes"],
            "additionalProperties": false,
            "properties": {
              "filename": { "type": "string", "pattern": "^[a-z0-9-]+-tests\\.zip$" },
              "sha256":   { "type": "string", "pattern": "^[a-f0-9]{64}$" },
              "bytes":    { "type": "integer", "minimum": 1 }
            }
          },

          "components": {
            "type": "object",
            "description": "which files are actually inside the main archive",
            "required": ["statement", "solution", "checker", "testset"],
            "additionalProperties": false,
            "properties": {
              "statement": { "type": "boolean" },
              "solution":  { "type": "boolean" },
              "checker":   { "type": "boolean" },
              "testset":   { "type": "boolean" },
              "editorial": { "type": "boolean", "default": false },
              "generator": { "type": "boolean", "default": false },
              "validator": { "const": false,
                             "description": "never produced; see OUTPUT_CONTRACT B.3" }
            }
          },

          "languages": {
            "type": "array", "items": { "enum": ["EN", "RU", "TJ", "UZ"] }, "minItems": 1,
            "description": "authoritative. Do NOT infer this by parsing the statement."
          },

          "group": { "enum": ["easy", "medium", "hard"] },
          "difficulty": {
            "type": "object",
            "required": ["prereq", "insight", "impl", "pressure", "traps", "total",
                         "group", "impl_loc"],
            "additionalProperties": false,
            "properties": {
              "prereq":   { "type": "integer", "minimum": 0, "maximum": 3 },
              "insight":  { "type": "integer", "minimum": 0, "maximum": 3 },
              "impl":     { "type": "integer", "minimum": 0, "maximum": 3 },
              "pressure": { "type": "integer", "minimum": 0, "maximum": 3 },
              "traps":    { "type": "integer", "minimum": 0, "maximum": 3 },
              "total":    { "type": "integer", "minimum": 0, "maximum": 15 },
              "group":    { "enum": ["easy", "medium", "hard"] },
              "impl_loc": { "type": "integer", "minimum": 1 }
            }
          },

          "tags": {
            "type": "array", "minItems": 1, "maxItems": 4,
            "items": { "type": "string" },
            "description": "from the closed vocabulary in CHARACTERISTICS_SPEC.md §4, primary first"
          },

          "checker": {
            "type": "object",
            "required": ["kind", "name", "polygon_id"],
            "additionalProperties": false,
            "properties": {
              "kind":       { "enum": ["native", "custom"] },
              "name":       { "type": ["string", "null"] },
              "polygon_id": { "type": ["string", "null"],
                              "description": "e.g. std::ncmp.cpp; null when kind=custom" }
            }
          },

          "subtasks": {
            "type": "array",
            "description": "empty array when the problem has no subtasks",
            "items": {
              "type": "object",
              "required": ["id", "points", "depends_on"],
              "additionalProperties": false,
              "properties": {
                "id":         { "type": "integer", "minimum": 0 },
                "points":     { "type": "integer", "minimum": 0, "maximum": 100 },
                "depends_on": { "type": "array", "items": { "type": "integer", "minimum": 0 } },
                "constraint": { "type": "string" }
              }
            }
          },

          "tests": {
            "type": "object",
            "required": ["samples", "main", "total", "by_group"],
            "additionalProperties": false,
            "properties": {
              "samples":  { "type": "integer", "minimum": 1 },
              "main":     { "type": "integer", "minimum": 1 },
              "total":    { "type": "integer", "minimum": 2 },
              "by_group": { "type": "object",
                            "additionalProperties": { "type": "integer", "minimum": 0 },
                            "description": "keys s0, s1, … ; counts per test group" }
            }
          },

          "limits": {
            "type": "object",
            "required": ["time_limit_s", "memory_limit_mb"],
            "additionalProperties": false,
            "properties": {
              "time_limit_s":     { "type": "number", "exclusiveMinimum": 0 },
              "memory_limit_mb":  { "type": "integer", "minimum": 64 },
              "measured_worst_s": { "type": "number" },
              "limits_rationale": { "type": ["string", "null"],
                                    "description": "REQUIRED (non-null) when either limit is non-default" }
            }
          },

          "samples_in_testset": { "type": "boolean" },
          "seed": { "type": "string",
                    "description": "generator seed, derived as sha256(slug)[:16] as hex" },
          "notes": { "type": ["string", "null"] }
        }
      }
    },

    "characteristics": {
      "type": "object",
      "required": ["filename", "sha256", "spec_version"],
      "additionalProperties": false,
      "properties": {
        "filename":     { "const": "characteristics.md" },
        "sha256":       { "type": "string", "pattern": "^[a-f0-9]{64}$" },
        "spec_version": { "type": "string" }
      }
    },

    "preflight": {
      "type": "object",
      "required": ["status", "report", "checks_run", "checks_failed", "spec_version"],
      "additionalProperties": false,
      "properties": {
        "status":        { "const": "pass",
                           "description": "the manifest is only written on a full pass" },
        "report":        { "const": "PREFLIGHT_REPORT.md" },
        "checks_run":    { "type": "integer", "minimum": 1 },
        "checks_failed": { "const": 0 },
        "waivers":       { "type": "array", "items": { "type": "string" },
                           "description": "check IDs waived with human approval, with reason appended" },
        "spec_version":  { "type": "string" }
      }
    }
  }
}
```

### Notes on a few fields

- **`sha256` values are over the raw archive bytes.** They are only stable across rebuilds
  because of the determinism amendments in `OUTPUT_CONTRACT.md` §B.7 (slug-derived seed,
  fixed-timestamp deterministic zip writing). For any set built before contract v1.0, treat
  checksums as integrity-only, not as build identity.
- **`archive` may be `null`** exactly when `delivery` is `"partial"` and only a `-tests`
  archive shipped. In a `"full"` set, `archive` is never null.
- **`components.validator` is pinned to `false`.** It exists so that a Maestro step which
  needs a validator fails loudly against the schema instead of silently importing a problem
  with an empty validator slot.
- **`seed` is a string**, not a number — the value exceeds IEEE-754 safe integer range and
  would be corrupted by any JSON parser that maps numbers to doubles.
- **`generated_at` is UTC with a `Z` suffix.** Never local time, never offset-less.

---

## 3. Maestro cross-checks before ingest

Run all of these; halt the run on any failure. They are deliberately redundant with
`PREFLIGHT.md` — mine runs on my side, these run on yours, and neither should be trusted alone.

| # | check |
|---|---|
| M-1 | `schema_version` is a version this Maestro build understands. Unknown version → halt, don't best-effort. |
| M-2 | Every `problems[].archive.filename` exists in the folder; `sha256` and `bytes` match. |
| M-3 | Every `.zip` in the folder is referenced by exactly one manifest entry. No orphans. |
| M-4 | `set.problem_count` equals `len(problems)`. |
| M-5 | `slug` values are unique, match the slug regex, and none ends in `-tests`. |
| M-6 | `characteristics.md` exists and its `sha256` matches. |
| M-7 | Every General-table row in `characteristics.md` has a manifest entry with the same slug, and vice versa — same count, same set, same `idx` ordering. |
| M-8 | Per problem, `group`, `languages`, `tests.total`, `checker`, TL and ML agree between manifest and characteristics. Any disagreement is a halt, not a merge. |
| M-9 | Every `tags[]` entry is in the closed vocabulary. Unknown tag → halt (an unknown tag would otherwise be created silently in the tag store). |
| M-10 | Non-empty `subtasks`: non-sample points sum to 100; subtask 0 has 0 points. |
| M-11 | `tests_archive` non-null → that file exists and its base `slug` has a manifest entry. |
| M-12 | Opening each archive: exactly one root folder, named `{slug}`; no binaries; no path traversal (`..`, absolute paths); no uppercase or non-ASCII entry names. |
| M-13 | `preflight.status == "pass"` and `checks_failed == 0`. Any `waivers` present → require human acknowledgement before proceeding. |
| M-14 | `set.delivery == "partial"` → do not run the fresh-import path; route to the update path. |

---

## 4. Example

```json
{
  "schema_version": "1.0",
  "set": {
    "name": "edu-arrays-20260725",
    "title": "Course 1 / Group A2 / Week 5 — one-dimensional arrays",
    "description": "Traversal, running aggregates, adjacent-pair scanning.",
    "problem_count": 2,
    "generated_at": "2026-07-25T14:03:11Z",
    "delivery": "full",
    "languages_default": ["EN", "RU"],
    "supersedes": null,
    "contract_version": "1.0",
    "authoring_model": "claude-opus-5"
  },
  "problems": [
    {
      "idx": 1,
      "slug": "edu-arrays-running-max",
      "title": "Running Maximum",
      "titles_translated": { "RU": "Текущий максимум" },
      "archive": {
        "filename": "edu-arrays-running-max.zip",
        "sha256": "3f2a91c7d4b8e05612aa47f9c3d0b18e77a4562c9e1f8b3d05a7c264e9f01b3a",
        "bytes": 18432
      },
      "tests_archive": null,
      "components": {
        "statement": true, "solution": true, "checker": true, "testset": true,
        "editorial": false, "generator": false, "validator": false
      },
      "languages": ["EN", "RU"],
      "group": "easy",
      "difficulty": {
        "prereq": 0, "insight": 0, "impl": 0, "pressure": 0, "traps": 1,
        "total": 1, "group": "easy", "impl_loc": 22
      },
      "tags": ["implementation", "arrays"],
      "checker": { "kind": "native", "name": "ncmp", "polygon_id": "std::ncmp.cpp" },
      "subtasks": [],
      "tests": { "samples": 2, "main": 39, "total": 41, "by_group": { "s0": 2, "s1": 39 } },
      "limits": {
        "time_limit_s": 1, "memory_limit_mb": 256,
        "measured_worst_s": 0.06, "limits_rationale": null
      },
      "samples_in_testset": true,
      "seed": "9c1f4a7b2e5d0836",
      "notes": null
    },
    {
      "idx": 2,
      "slug": "edu-sorting-podium-order",
      "title": "Podium Order",
      "titles_translated": { "RU": "Порядок на подиуме" },
      "archive": {
        "filename": "edu-sorting-podium-order.zip",
        "sha256": "b70e4c1d9a3f28650cc71e4b8d29a0f3517c46de8b2a91f4c0d63e5807ab12cf",
        "bytes": 27904
      },
      "tests_archive": null,
      "components": {
        "statement": true, "solution": true, "checker": true, "testset": true,
        "editorial": false, "generator": false, "validator": false
      },
      "languages": ["EN", "RU"],
      "group": "medium",
      "difficulty": {
        "prereq": 1, "insight": 2, "impl": 1, "pressure": 1, "traps": 2,
        "total": 7, "group": "medium", "impl_loc": 58
      },
      "tags": ["sortings", "greedy", "arrays"],
      "checker": { "kind": "custom", "name": null, "polygon_id": null },
      "subtasks": [],
      "tests": { "samples": 3, "main": 55, "total": 58, "by_group": { "s0": 3, "s1": 55 } },
      "limits": {
        "time_limit_s": 2, "memory_limit_mb": 256,
        "measured_worst_s": 0.74,
        "limits_rationale": "TL 2s: reference worst case 0.74s on n=2e5 adversarial input, ~2.7x margin"
      },
      "samples_in_testset": true,
      "seed": "40b8e2d16c9a7f53",
      "notes": "Multiple valid orderings; the custom checker validates the participant ordering directly."
    }
  ],
  "characteristics": {
    "filename": "characteristics.md",
    "sha256": "c5a01e93b7d4682f10ae35c9d8b0741e63f2a95c4d18b70e2fa96c530e1d84b7",
    "spec_version": "1.0"
  },
  "preflight": {
    "status": "pass",
    "report": "PREFLIGHT_REPORT.md",
    "checks_run": 22,
    "checks_failed": 0,
    "waivers": [],
    "spec_version": "1.0"
  }
}
```
