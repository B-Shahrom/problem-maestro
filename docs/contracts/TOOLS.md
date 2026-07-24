# TOOLS.md

**Version:** 1.0 — specification only, no implementation.

The capability set required to author problem packages headlessly, i.e. to do everything
currently done by a general-purpose code sandbox plus a human collaborator. Grouped by layer:
primitives, build, verification, packaging, handoff.

Conventions used below:

- All paths are relative to a **workspace root** the runtime supplies. Absolute paths and `..`
  traversal are rejected by every tool that takes a path.
- Every tool returns `{ "ok": bool, "error": string|null, … }`. `ok: false` never raises; the
  caller decides.
- Timeouts, when applicable, are in seconds and always have a server-side ceiling.

---

## Layer 1 — Primitives

### `fs_write`
**Purpose** Create or overwrite a text file.
**Input** `{ path, content, encoding? = "utf-8", newline? = "lf" }`
**Returns** `{ ok, bytes_written, sha256 }`
**Notes** Creates parent directories. Rejects paths outside the workspace. Always writes LF
unless told otherwise — CRLF in a test file is a real failure mode.

### `fs_read`
**Purpose** Read a text file, optionally a line range.
**Input** `{ path, start_line? , end_line?, max_bytes? }`
**Returns** `{ ok, content, total_lines, truncated }`

### `fs_list`
**Purpose** List a directory tree.
**Input** `{ path, recursive? = true, glob? }`
**Returns** `{ ok, entries: [{ path, kind: "file"|"dir", bytes, sha256? }] }`
**Notes** `sha256` computed for files only when `include_hashes` is set, since hashing a large
test set is not free.

### `fs_delete`
**Purpose** Remove a file or directory. Needed to clear scratch files before PF-01.
**Input** `{ path, recursive? = false }`
**Returns** `{ ok, removed: [path] }`

### `checksum`
**Purpose** Hash one or more files.
**Input** `{ paths: [path], algo? = "sha256" }`
**Returns** `{ ok, hashes: { path: hex } }`
**Notes** Separate from `fs_list` so the manifest step can hash exactly the archives without
walking the tree.

---

## Layer 2 — Build and execution

### `compile_cpp`
**Purpose** Compile a C++ source to a binary **outside** the package tree.
**Input** `{ source_path, output_path, std? = "c++17", flags? = ["-O2","-Wall"], include_dirs? }`
**Returns** `{ ok, output_path, stdout, stderr, warnings: [string], seconds }`
**Notes** Must refuse an `output_path` inside a package folder — binary leakage into archives
has actually happened. `testlib.h` must be on the include path by default so checkers compile
without extra setup.

### `run_binary`
**Purpose** Run a compiled binary on one input, with limits.
**Input** `{ binary_path, stdin_path? , stdin_text?, args? = [], timeout_s? = 10,
memory_limit_mb? = 1024 }`
**Returns** `{ ok, exit_code, stdout, stderr, wall_seconds, peak_memory_mb, timed_out }`
**Notes** `wall_seconds` and `peak_memory_mb` are what feed the TL/ML rationale, so they must
be measured, not estimated. Needs to be callable a few thousand times per set without
per-call process-spawn overhead dominating — a batched variant is worth having:

### `run_binary_batch`
**Purpose** Run one binary over a whole directory of inputs.
**Input** `{ binary_path, input_glob, timeout_s?, memory_limit_mb?, parallelism? = 4 }`
**Returns** `{ ok, results: [{ input_path, exit_code, stdout_path, wall_seconds,
peak_memory_mb, timed_out }], worst_wall_seconds, worst_input_path }`
**Notes** `worst_wall_seconds` / `worst_input_path` are exactly the values needed for §13 of the
system prompt, so returning them directly avoids a second pass.

### `run_python`
**Purpose** Execute a Python script — the generator, and the reference implementation.
**Input** `{ script_path?, code?, args? = [], stdin_text?, timeout_s? = 300,
allow_writes_under? }`
**Returns** `{ ok, exit_code, stdout, stderr, seconds, files_written: [path] }`
**Notes** `files_written` matters: the generator's job is to emit test files, and the caller
needs to know exactly what appeared without diffing the tree. `allow_writes_under` scopes
writes to the package's `testset/`.

---

## Layer 3 — Verification

### `cross_validate`
**Purpose** The core correctness gate: run the C++ solution and the Python reference over every
test and confirm they agree.
**Input** `{ solution_binary, reference_script, testset_dir, comparison? = "tokens"|"exact"|
"reals", epsilon?, timeout_s? }`
**Returns** `{ ok, tests_run, mismatches: [{ input_path, solution_output, reference_output,
first_diff_token }], worst_wall_seconds }`
**Notes** Must report **all** mismatches, not just the first — a systematic off-by-one looks
different from a single edge case, and the distinction changes what you fix.

### `validate_tests`
**Purpose** Constraint validation plus structural checks over a `testset/`.
**Input** `{ testset_dir, constraints_script? }`
**Returns** `{ ok, files, groups: { "s0": n, "s1": m }, index_gaps: [string],
naming_violations: [path], duplicates: [[path, path]], constraint_failures: [{path, message}] }`
**Notes** Duplicate detection **pools all groups together** (s0 + s1…), because Polygon rejects
cross-group duplicates and per-group dedup has silently passed bad sets. `index_gaps` catches
the `idx10`-before-`idx2` rename bug.

### `validate_statement`
**Purpose** Static lint of `problem_statement.mdx` against the house rules. The highest-value
tool here, because these are the failures that ship undetected.
**Input** `{ statement_path, expected_languages: ["EN","RU"], has_subtasks: bool,
allowed_macros?: [string] }`
**Returns**

```json
{ "ok": false,
  "languages_detected": ["EN","RU"],
  "sections_found": ["Problem Name","Legend","Input format","Output format","Note"],
  "findings": [
    { "rule": "PF-23", "severity": "error", "line": 88,
      "message": "non-English \\textbf{} argument 'Условие' outside allowed exceptions" },
    { "rule": "PF-22", "severity": "error", "line": 61,
      "message": "Note contains method-hint token 'two pointers'" },
    { "rule": "PF-21", "severity": "error", "line": 12,
      "message": "\\textit is forbidden" }
  ] }
```

**Checks:** section presence and order; balanced braces and `\begin`/`\end`; even `$` count per
block; `\textit`, `\boldmath`, `\lstinputlisting` absent; `tabular` only inside Scoring;
language tags consistent with `expected_languages`; non-English `\textbf{}` arguments limited to
language tags and scoring cells; Note present and free of the method-hint token list; no
TODO/TBD/placeholder; Problem Name value is plain text.

### `validate_checker`
**Purpose** Lint `checker.cpp` for the rules that Polygon will not catch until a real submission
fails.
**Input** `{ checker_path }`
**Returns** `{ ok, kind: "native"|"custom", native_name?, findings: [{rule, severity, line,
message}] }`
**Checks:** `readEof()` used anywhere (error — must be `seekEof()`); any unbounded/unnamed
`read*` call on `ouf`/`ans` (error); jury-stream failures reported as `_wa` rather than `_fail`
(warning); `stoi` without range guard (warning); `compress()` usage (warning). Also fingerprints
the file against the known standard checkers so `kind`/`native_name` can be filled without
guessing.

### `run_checker`
**Purpose** Execute a compiled checker on a triple, to confirm it accepts correct output and
rejects wrong output.
**Input** `{ checker_binary, input_path, participant_output_path, jury_answer_path }`
**Returns** `{ ok, verdict: "ok"|"wa"|"pe"|"fail", message, seconds }`
**Notes** Needed for the negative tests — a checker that accepts everything passes every
positive test.

### `measure_limits`
**Purpose** Derive a defensible TL/ML from measurement.
**Input** `{ solution_binary, testset_dir, target_margin? = 3.0 }`
**Returns** `{ ok, worst_wall_seconds, worst_input_path, peak_memory_mb,
suggested_time_limit_s, suggested_memory_limit_mb, rationale }`
**Notes** `rationale` is a prose string of the exact form the manifest requires, so the value is
generated once and reused rather than re-written by hand.

---

## Layer 4 — Packaging

### `package_zip`
**Purpose** Build a deterministic archive.
**Input** `{ source_dir, output_path, root_folder_name, deterministic? = true,
exclude? = ["*.pyc","__pycache__","__MACOSX",".DS_Store"] }`
**Returns** `{ ok, output_path, sha256, bytes, entries: [path] }`
**Notes** `deterministic: true` means entries written in sorted order, fixed timestamp
`1980-01-01T00:00:00`, fixed compression level, no extended attributes — so the same inputs
produce a byte-identical archive and a stable `sha256`. Must reject any file matching the
binary heuristic (ELF/Mach-O magic) and any entry name with uppercase, spaces or non-ASCII.

### `inspect_zip`
**Purpose** Audit an archive without extracting it (PF-13, PF-15, PF-16, and Maestro's M-12).
**Input** `{ archive_path }`
**Returns** `{ ok, root_folders: [name], entries: [{path, bytes, is_binary}],
findings: [{rule, message}] }`
**Notes** Reports path traversal, absolute paths, nested archives, multiple root folders,
binaries, and any `testset/` entry not matching `input_s\d+_idx\d+\.txt`.

---

## Layer 5 — Handoff

### `emit_characteristics`
**Purpose** Render `characteristics.md` from structured per-problem records.
**Input** `{ set_name, description, problems: [ {slug, title, languages, group, difficulty,
tests, subtasks, checker, limits, tags, notes} ], spec_version }`
**Returns** `{ ok, path, sha256, warnings: [string] }`
**Notes** Owns the ordering, the prefix grouping, the `[none]` handling and the totals, so the
layout cannot drift between sets by being hand-assembled. It should refuse to render — not
warn — on an out-of-vocabulary tag or an unrecognised group name.

### `run_preflight`
**Purpose** Execute the `PREFLIGHT.md` checklist over a finished set folder.
**Input** `{ set_dir, spec_version, waivers?: [{check_id, reason}] }`
**Returns**

```json
{ "ok": false, "checks_run": 25, "passed": 23, "failed": 2, "na": 0,
  "results": [{ "id": "PF-22", "status": "fail",
                "detail": "edu-arrays-largest-gap: Note contains 'for each'" }],
  "report_path": "PREFLIGHT_REPORT.md" }
```

**Notes** Composes the validators above rather than reimplementing them. Writes the report file
as a side effect. Must be runnable repeatedly and idempotently.

### `emit_manifest`
**Purpose** Write `MANIFEST.json` as the completion sentinel.
**Input** `{ set_dir, set_metadata, problems: [...], characteristics_sha256, preflight_result,
schema_version }`
**Returns** `{ ok, path, sha256, refused_reason? }`
**Notes** **Must refuse to write when `preflight_result.failed > 0`** — that refusal is the
mechanism that makes the sentinel meaningful. Writes to a temp name in the same directory and
`os.replace`s it into position so no partial file is ever observable. Must be the last write to
the set folder.

---

## Optional — integration

### `fetch_standard_checker`
**Purpose** Retrieve a canonical testlib standard checker source by name so it can be copied
verbatim rather than reconstructed from memory.
**Input** `{ name: "ncmp"|"wcmp"|"lcmp"|"fcmp"|"yesno"|"nyesno"|"rcmp4"|"rcmp6"|"rcmp9"|"hcmp" }`
**Returns** `{ ok, source, polygon_id, sha256 }`
**Notes** Worth having as a tool rather than as recalled text: "copy it verbatim" is only true
if the copy is fetched.

### `notion_upsert_problems`
**Purpose** Sync a finished set into the problem-tracking database.
**Input** `{ data_source_id, problems: [{ slug, title, module, week, course, topic_tags,
developer, statement_latex }], dry_run? = true }`
**Returns** `{ ok, created: [{slug, page_id}], updated: [...], errors: [{slug, message}] }`
**Notes** Property names must match the target schema exactly, including capitalisation and
spacing. Numeric-looking fields such as a module number are strings. Tags arrive already mapped
to the dashed spelling. Default `dry_run: true` — an unattended run that silently creates
duplicate pages is worse than one that does nothing.

---

## What is deliberately not a tool

- **"Generate a problem idea."** Authoring is the model's job, not a service call.
- **"Score difficulty."** The rubric is judgement plus one measured input (LOC), which
  `fs_read` already provides. Wrapping it as a tool would hide the reasoning rather than record
  it.
- **A Polygon client.** That is Maestro's half of the pipeline; the handoff boundary is the set
  folder plus its manifest, and keeping the authoring side unable to touch Polygon is a useful
  safety property.
