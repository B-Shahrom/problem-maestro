# PREFLIGHT.md

**Spec version:** 1.0
**When:** after every archive, and `characteristics.md`, are final — and **before**
`MANIFEST.json` is written.
**Output:** `PREFLIGHT_REPORT.md` in the set folder, plus an explicit pass/fail line in the
chat message that delivers the set.

A set is not "done" because the last archive was built. It is done when this checklist passes.
**On any FAIL, `MANIFEST.json` is not written** — so a failed set cannot be ingested, it can
only look unfinished, which is the correct signal for an unattended importer.

Each check states the predicate in a form that can be evaluated mechanically, so that when
this eventually runs as a script rather than as my own reading, nothing has to be
reinterpreted.

---

## Section 1 — Set structure

**PF-01 · Set folder is clean**
The folder contains exactly: N `.zip` files, `characteristics.md`, `PREFLIGHT_REPORT.md`, and
(after this check) `MANIFEST.json`. Nothing else — no scratch files, no generators unless
requested, no logs, no `.pyc`.
FAIL → remove strays, rerun.

**PF-02 · Set name is well-formed**
Matches `^[a-z0-9]+(-[a-z0-9]+)*-\d{8}(-r\d+)?$`.

**PF-03 · Archive ↔ characteristics bijection**
Every slug in the `characteristics.md` General table has an archive `{slug}.zip` on disk, and
every `{slug}.zip` on disk appears exactly once in the General table. Set equality in both
directions, not just equal counts.

**PF-04 · Slugs are unique and legal**
Within the set, slugs are pairwise distinct; each matches `^[a-z0-9]+(-[a-z0-9]+)*$`, is 3–80
chars, has no `--`, no leading/trailing dash, and does **not** end in `-tests`.

**PF-05 · Tests archives have a base problem**
For every `{X}-tests.zip` present, `{X}` is a slug in the General table. In a `full` delivery,
`{X}.zip` also exists on disk.

---

## Section 2 — characteristics.md integrity

**PF-06 · Positional lists align**
`Suggested tags` and `Checkers used` each have exactly N numbered entries; entry *k* carries
the number *k*; and the order matches General row order. No gaps, no duplicates, no partial
fills.

**PF-07 · Every section present**
All sections from `CHARACTERISTICS_SPEC.md` §2 exist in the specified order. Any section with
no members contains the single line `[none]` — never omitted, never blank.

**PF-08 · No blanks, no placeholders**
No cell or list item anywhere in the file is empty, `TBD`, `TODO`, `???`, `N/A` (except where
§2 explicitly permits `N/A` for a subset delivery), or a template bracket such as `[slug-1]`,
`[Title 1]`, `[BATCH NAME]`.
Regex sweep: `\[(slug|Title|BATCH|none-check)[^\]]*\]`, `\bTBD\b`, `\bTODO\b`, `\?\?\?`.
*(`[none]` is the one permitted bracketed token.)*

**PF-09 · Arithmetic is consistent**
For each row, `tests` reads `N (a+b)` with `a + b == N`. `TOTAL problems` equals the row count.
`TOTAL tests` equals the sum of the row `N` values. Difficulty buckets in `Easy`/`Medium`/`Hard`
partition the General rows exactly — every row appears in exactly one bucket, matching its
`group` cell.

**PF-10 · Tags are in-vocabulary**
Every tag on every numbered line appears in the closed vocabulary
(`CHARACTERISTICS_SPEC.md` §4). 1–4 tags per line. FAIL on any unknown tag — do not silently
coin one.

**PF-11 · Languages agree three ways**
For each problem, the `languages` cell, the `MANIFEST.json` `languages` array, and what is
actually in `problem_statement.mdx` all name the same set. (Statement check: a multi-language
file contains exactly one `\textbf{English}` / `\textbf{Russian}` / `\textbf{Tajik}` /
`\textbf{Uzbek}` tag per declared language and no others; a single-language file contains no
language tag at all.)

**PF-12 · Ordering is the difficulty total order**
Re-derive the §3.3 sort from the recorded difficulty vectors and confirm it reproduces the
`idx` assignment. This catches a row inserted or reordered by hand after scoring.

---

## Section 3 — Package contents

**PF-13 · Mandatory files present**
Each `{slug}.zip` contains exactly one root folder named `{slug}`, and within it:
`problem_statement.mdx`, `solution.cpp`, `checker.cpp`, and a `testset/` directory with at
least one `input_s0_*.txt` and at least one `input_s1_*.txt`.

**PF-14 · Limits are justified**
TL is `1 s` and ML is `256 MB`, or else `limits.limits_rationale` is a non-empty string naming
the measured worst case. A non-default limit with no rationale is a FAIL.

**PF-15 · Archive hygiene**
No compiled binaries (ELF/Mach-O magic bytes, or an executable-mode file with no extension),
no `__MACOSX`, `.DS_Store`, `.git`, `*.pyc`, no nested `.zip`, no absolute paths or `..`
segments, no entry name containing uppercase, spaces, or non-ASCII.

**PF-16 · Tests are inputs only**
Every file under `testset/` matches `^input_s\d+_idx\d+\.txt$`. No `.out`, `.ans`,
`answer_*`, `expected_*`. Indices within each `s{k}` group are 0-based and contiguous with no
gaps.

**PF-17 · No duplicate tests**
MD5 over the full pooled test set (s0 + s1..sN together) yields N distinct hashes. Dedup within
a group only is not sufficient — Polygon rejects cross-group duplicates too.

**PF-18 · Solution and checker are real**
`solution.cpp` compiles clean under `g++ -std=c++17 -O2 -Wall`; the cross-validation record
shows zero mismatches against the independent Python reference across **all** tests, not just
samples. `checker.cpp` compiles against `testlib.h`. Every participant-stream read in a custom
checker uses the bounded/named form; the stream is closed with `seekEof()`, never `readEof()`.

**PF-19 · Subtask integrity**
Where subtasks exist: subtask 0 is the samples and worth 0 points; non-sample points sum to
exactly 100; a single non-sample subtask is numbered S1; dependencies are listed explicitly
including transitive ones, in ascending order; every declared subtask has at least one test
file.

---

## Section 4 — Statement quality

**PF-20 · Statement is complete**
No `TODO`, `TBD`, `FIXME`, `XXX`, `lorem`, `<placeholder>`, `[…]`, or a bare `\textbf{}` with
an empty argument. No trailing "to be written" prose.

**PF-21 · LaTeX resolves**
Braces balance. Every `\begin{X}` has a matching `\end{X}`. No `\textit`, no `\boldmath`, no
`\lstinputlisting`, no macro outside the Polygon LaTeX manual's documented set. Math delimiters
`$` occur an even number of times per block. Section headers present in the required order.

**PF-22 · Note block exists and does not leak method**
Every custom-developed problem has a `\textbf{Note}` block explaining at least one sample.
Sweep each Note for: *pointer, pointers, window, deque, median, binary search, prefix sum,
prefix sums, sliding, two pointers, monotonic, frequency map, maintain, for each, for every,
sort by, greedily, approach, algorithm, optimal strategy, priority queue, DP, dynamic
programming, we can*. Any hit is a FAIL until rewritten as a claim about the sample's actual
numbers.

**PF-23 · Translated headers stay English**
In a multi-language statement, scan every `\textbf{...}`. Each non-English argument must be
either a language tag (`Russian`/`Tajik`/`Uzbek`) or a cell inside the scoring table's
`tabular`. Any other non-English `\textbf{}` — a translated `Legend`, `Input format`, etc. — is
a FAIL. *(This is a repeat offender: it shipped undetected on the 25-problem two-pointers
batch.)*

**PF-24 · No tables outside Scoring**
`\begin{tabular}` appears only inside the `\textbf{Scoring}` section, and only there.

**PF-25 · Output is never empty**
For every problem, the worst-case/degenerate input still produces at least one line of output
(a count-first `0`, a sentinel `-1`, or a fixed message). Verified by running the reference
solution on the minimal and degenerate tests, not by reading the statement.

---

## Reporting rules

- Report the result **explicitly** in the delivery message. Not "the set is done" — a line of
  the form `PREFLIGHT: 25 checks, 25 pass, 0 fail` or `PREFLIGHT: 25 checks, 23 pass, 2 FAIL
  (PF-10, PF-22)`.
- On FAIL: fix and rerun the whole checklist. Do not report a partial pass and ship anyway; do
  not write `MANIFEST.json`.
- A check may be **waived** only with your explicit approval in the conversation. A waiver is
  recorded in `PREFLIGHT_REPORT.md` and in `manifest.preflight.waivers` with its reason, so
  Maestro can require a human acknowledgement (cross-check M-13) rather than silently
  proceeding.
- `N/A` is legitimate for structurally inapplicable checks — PF-19 on a set with no subtasks,
  PF-23 on an English-only set, PF-05 with no tests archives. `N/A` is not a failure and not a
  waiver, but the report must say *why* it is N/A.

---

## PREFLIGHT_REPORT.md template

```markdown
# Preflight — {set-name}

Spec: PREFLIGHT.md v1.0 · Contract: OUTPUT_CONTRACT.md v1.0
Run at: {UTC ISO-8601}
Problems: {N} · Archives: {N} · Tests archives: {M}

**RESULT: PASS — {R} checks run, {P} pass, {F} fail, {A} n/a**

| id | check | result | detail |
|---|---|---|---|
| PF-01 | set folder clean | PASS | 12 zips + 2 md, no strays |
| PF-02 | set name well-formed | PASS | edu-arrays-20260725 |
| PF-03 | archive ↔ characteristics bijection | PASS | 12 ↔ 12, set-equal |
| … | | | |
| PF-19 | subtask integrity | N/A | no subtasks in this set |
| … | | | |

## Failures

[none]

## Waivers

[none]

## Notes

- {anything a human should look at even though it passed}
```
