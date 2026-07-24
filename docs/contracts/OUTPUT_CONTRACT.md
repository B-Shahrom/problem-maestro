# OUTPUT_CONTRACT.md

**Contract version:** 1.0
**Status:** normative from the next problem set onward.
**Supersedes:** playbook §1 (package structure) and §11 (batch delivery) wherever they conflict.

This document has two halves. **Part A** describes what I actually emit today, with each
statement labelled by how much you can trust it. **Part B** is the pinned contract Maestro
can rely on. Read Part A first — the gap between the two halves is the list of things that
would have broken your pipeline.

Labels used in Part A:

- `[GUARANTEED]` — invariant across every set I have built; safe to depend on retroactively.
- `[USUAL]` — what normally happens, but it is convention, not enforcement. Has held in
  practice, could silently differ.
- `[DRIFTED]` — has demonstrably differed between sets. Do not depend on it.
- `[UNDEFINED]` — no rule exists; I decide ad-hoc per set.

---

## PART A — What I actually produce today

### A.1 Where files land

`[GUARANTEED]` Everything is written to a flat `/mnt/user-data/outputs/` directory in an
ephemeral Linux container and then surfaced to you through the chat UI as download links.

`[GUARANTEED]` **There is no per-set folder.** A 25-problem batch produces 25 loose `.zip`
files plus a loose characteristics file in the same flat directory, alongside any scratch
files I happened to leave there.

`[GUARANTEED]` **The container is ephemeral.** It resets between sessions, and a long batch
delivered across several messages only persists for that session. There is currently no
durable folder for Maestro to watch — the chat download is the real delivery channel. This is
the single biggest structural gap between what I do and what Maestro assumes.

`[USUAL]` Intermediate artifacts (the Python generator, compiled binaries, cross-validation
logs) are written under `/home/claude/` and are not presented. Binaries are compiled to
`/tmp/sol` specifically so they cannot land inside a package — this was a real leak, caught
by you in the Group A1 Week 2 batch.

### A.2 Archive naming

`[GUARANTEED]` One problem = one `.zip`. Archive filename = `{slug}.zip`. The zip contains
exactly one root folder, named identically to the slug.

`[GUARANTEED]` Slugs are lowercase ASCII, dash-separated, no spaces, no underscores, no
non-ASCII, no uppercase. No exceptions across any set.

`[USUAL]` Educational problems: `edu-{lesson}-{title-part}`. Contest/exam problems reuse the
same shape (`edu-greedy-exam-*`, `edu-binary-search-*`). Olympiad tracks use
`toi{year}-{grade}-{letter}`.

`[DRIFTED]` **The lesson segment count is not fixed.** `edu-lca-…` is two segments,
`edu-binary-lifting-…` is three, `edu-greedy-priority-queue-…` is four. Prefix grouping in the
characteristics file therefore relies on longest-match detection, which is a heuristic, not a
parse. A slug cannot be decomposed into `namespace / lesson / title` by splitting on dashes.

`[DRIFTED]` **The title→slug mapping is not a function.** Titles are sometimes shortened or
reworded in the slug ("Block Painted Incorrectly!" → shortened form). Maestro must never
recompute a slug from a title, or vice versa; it must read the pairing.

### A.3 The `<slug>-tests` convention

`[UNDEFINED]` **I have no rule for this today.** Tests normally ship inside the main archive
under `testset/`. Separate tests-only archives have appeared in older sets (the IOI conversion
and Republican Olympiad workflows shipped test archives separately from statements), and when
you request a subset delivery ("only statements and tests") I name the resulting zip ad-hoc.

There is no guarantee today that a tests archive is named `{slug}-tests.zip`, and no guarantee
about its internal folder structure. **Treat any `-tests` archive from a past set as unspecified.**
Part B pins it.

### A.4 Package contents

`[GUARANTEED]` present in every package:

```
{slug}/
├── problem_statement.mdx
├── solution.cpp
├── checker.cpp
└── testset/
    ├── input_s0_idx0.txt
    ├── input_s1_idx0.txt
    └── ...
```

`[GUARANTEED]` Filenames inside the package are **unprefixed** — `solution.cpp`, never
`{slug}-solution.cpp`.

`[GUARANTEED]` Tests are **inputs only**. No expected-output files are ever shipped.

`[GUARANTEED]` No compiled binaries, no `__MACOSX`, no nested archives.

`[USUAL]` `problem_statement.mdx` carries Polygon LaTeX despite the `.mdx` extension. It is
not MDX and will not parse as MDX.

`[USUAL]` Exactly one solution (`solution.cpp`). No brute-force reference, no deliberately
wrong solutions, no `solution_2.cpp` — even though a Python reference implementation exists
internally and every test was cross-validated against it. **The Python reference is never
shipped.** If Maestro wants Polygon to verify against a second model solution, it does not
have one.

`[USUAL]` The test generator is a Python script and is **not** shipped unless you ask.

`[GUARANTEED]` **No `validator.cpp` is ever produced.** Constraint validation happens inside
the generator as Python assertions. Polygon's validator slot will be empty on every problem
I have ever delivered. If Maestro expects a validator, it will fail on 100% of packages.

`[UNDEFINED]` Editorials/tutorials. Produced only on request, and when produced they have gone
out as chat content or as a loose file, never with a pinned path inside the package.

### A.5 The samples-in-testset contradiction

`[DRIFTED]` — and this one is live in the playbook right now.

Playbook §1 shows `input_s0_idx0.txt` sitting inside `testset/`. Playbook §3 says subtask 0
is "excluded from the test zip." These are the two halves of the same document contradicting
each other, and both behaviours exist in shipped sets: the Republican Olympiad work explicitly
stripped s0 from the archive, while every recent `edu-*` batch includes it.

For Maestro this decides whether sample tests get imported twice (once as Polygon samples from
the statement, once as tests) or zero times. **See decision D-01 in Part B.**

### A.6 Test file naming

`[GUARANTEED]` `input_s{subtask}_idx{index}.txt`. `s0` = samples. `s1` = main tests when there
are no subtasks; `s1..sN` = per-subtask tests when there are.

`[USUAL]` `idx` is 0-based and contiguous within each subtask group. Contiguity is enforced by
a two-pass rename because naive alphabetical renaming puts `idx10` before `idx2` — a bug that
has occurred and is now guarded against, but the guard is procedural, not verified.

### A.7 Statement languages

`[DRIFTED]` — badly.

The current playbook says the default is **EN + RU**. Several large recent batches shipped
**English-only** under a standing override. Older contest sets shipped **four languages**
(EN/RU/TJ/UZ) in one file. My own carried-over memory of the default says "English-only",
which contradicts the playbook that supersedes it.

`[GUARANTEED]` When multiple languages ship, they are concatenated into the single
`problem_statement.mdx`, each block opening with `\textbf{English}` / `\textbf{Russian}` /
`\textbf{Tajik}` / `\textbf{Uzbek}`, in that order, with no separator between blocks.

`[GUARANTEED]` When only one language ships, **there is no language tag at all**. So the file
format differs structurally between a one-language and a two-language build, and Maestro
cannot detect "this is EN-only" by the same parse it uses for "EN + RU". It must be told.

### A.8 Character set of names

`[GUARANTEED]` Archive names, folder names and file names inside archives: lowercase ASCII,
digits, dash, dot. No spaces, no uppercase, no non-ASCII, ever.

`[GUARANTEED]` **Titles are the opposite** — English Title Case, may contain spaces,
apostrophes, exclamation marks ("Block Painted Incorrectly!"). Titles are display strings and
must be quoted/escaped by anything consuming them.

`[USUAL]` Statement *content* contains Cyrillic when RU/TJ ship. Files are UTF-8, LF endings.

### A.9 Reproducibility

`[GUARANTEED]` **Rebuilding a set does not reproduce it.** Per playbook §8 the generator calls
`random.seed()` with no argument. Rebuilding the same problem yields different test contents,
and — because pooled MD5 deduplication removes a different number of collisions each run — can
yield a *different test count*.

Consequently: today, archive checksums are not stable across rebuilds, the `tests` column in
the characteristics file is not stable across rebuilds, and "the same set always yields the
same file" is **false** for anything downstream of test generation. Part B fixes this.

---

## PART B — The contract (v1.0)

Everything below is what I commit to from the next set onward.

### B.1 Set folder

One problem set = one folder. Nothing else lands in it.

```
{set-name}/
├── MANIFEST.json            ← written LAST; see MANIFEST_SPEC.md
├── characteristics.md       ← see CHARACTERISTICS_SPEC.md
├── PREFLIGHT_REPORT.md      ← see PREFLIGHT.md
├── edu-arrays-running-max.zip
├── edu-arrays-pair-sum.zip
└── edu-arrays-plateau-length.zip
```

Set-name grammar: `^[a-z0-9]+(-[a-z0-9]+)*-\d{8}(-r\d+)?$` — a lowercase dashed key plus a
`YYYYMMDD` date, with an optional `-r2`, `-r3` rerun suffix. Example:
`edu-arrays-20260725`, `toi2026-11-20260725-r2`.

Exactly three non-archive files, exactly those three names. No scratch files, no generators
(unless requested, see B.4), no logs.

### B.2 Archive naming

```
main archive:   {slug}.zip
tests archive:  {slug}-tests.zip      (conditional, see B.5)
```

Slug grammar, enforced:

```
^[a-z0-9]+(-[a-z0-9]+)*$
```

- lowercase ASCII letters, digits, single dashes between segments
- length 3–80 characters
- no leading/trailing dash, no `--`, no underscore, no dot, no space, no non-ASCII
- **must not end in `-tests`** (reserved, prevents collision with the tests-archive name)
- unique within a set; assumed globally unique on the platform

Shape by kind:

| kind | pattern | example |
|---|---|---|
| educational lesson | `edu-{lesson}-{title-part}` | `edu-binary-lifting-kth-ancestor` |
| contest / exam (edu-derived) | `edu-{lesson}-{qualifier}-{title-part}` | `edu-greedy-exam-job-order` |
| olympiad track | `toi{year}-{grade}-{letter}` | `toi2026-11-b` |

`{title-part}` is *derived from* the English title by: strip diacritics → lowercase → replace
each run of non-alphanumerics with `-` → trim dashes → optionally shorten at a word boundary
for length. **Because shortening is allowed, the derivation is one-way.** Maestro must read the
slug↔title pairing from `MANIFEST.json` and never recompute either side.

The zip's single root folder is named exactly `{slug}` (or `{slug}-tests`). Nothing sits at the
zip root beside it.

### B.3 Main archive contents

```
{slug}/
├── problem_statement.mdx        MANDATORY
├── solution.cpp                 MANDATORY
├── checker.cpp                  MANDATORY
├── testset/                     MANDATORY, ≥1 s0 file and ≥1 s1 file
│   ├── input_s0_idx0.txt
│   ├── input_s0_idx1.txt
│   ├── input_s1_idx0.txt
│   └── …
├── editorial.mdx                OPTIONAL — only when requested
└── generator.py                 OPTIONAL — only when requested
```

| path | status | notes |
|---|---|---|
| `problem_statement.mdx` | mandatory | Polygon LaTeX, UTF-8, LF. Not real MDX. |
| `solution.cpp` | mandatory | C++17, `-O2`, single accepted solution. |
| `checker.cpp` | mandatory | Present **even when a standard checker is used** — it is then a verbatim copy of the testlib standard checker. `MANIFEST.json` says which. |
| `testset/*.txt` | mandatory | Inputs only. Never any `.out`/`.ans`/`answer_*`. |
| `editorial.mdx` | optional | Only if requested. Same LaTeX conventions; code inline via `lstlisting`. |
| `generator.py` | optional | Only if requested. Self-contained, seeded per B.7. |
| `validator.cpp` | **not produced** | Constraint checks live in the generator. Maestro must not require one. |

Forbidden inside any archive: compiled binaries, `__MACOSX`, `.DS_Store`, `.git`, nested zips,
expected-output files, the Python reference implementation, any file whose name contains
uppercase, spaces, or non-ASCII.

### B.4 One problem = one folder inside one archive

A problem is never split across archives, except for the tests-archive case in B.5. No
problem-set-level shared files inside archives.

### B.5 The `{slug}-tests.zip` convention — pinned

A tests archive is produced in exactly two situations, and never otherwise:

1. **Subset delivery** — you asked for tests only (or for a component set that excludes the
   solution/checker).
2. **Partial re-delivery** — a previously delivered problem needed a test regeneration and
   nothing else changed.

Contents, always:

```
{slug}-tests/
└── testset/
    ├── input_s0_idx0.txt
    └── …
```

Rules:

- The tests archive is a **strict subset** of the main archive's `testset/`, byte-identical.
- When both archives exist for the same slug, the main archive still contains its own full
  `testset/`. The tests archive is redundant, not authoritative — **on conflict, the main
  archive wins.**
- A `{slug}-tests.zip` may exist without `{slug}.zip` **only** in a partial re-delivery, and
  in that case `MANIFEST.json` marks the problem `"delivery": "partial"` and lists which
  components are present. Maestro must not treat a partial set as a fresh import.
- `MANIFEST.json` always carries an explicit `tests_archive` field (object or `null`). Maestro
  must read that field rather than probing the filesystem for the name.

### B.6 Decisions that were previously ambiguous

**D-01 — sample tests in `testset/`: INCLUDED.**
`s0` files ship inside `testset/` in both archive kinds. `MANIFEST.json` carries
`"samples_in_testset": true` on every problem so the assumption is explicit and machine-checked
rather than inferred. The playbook §3 clause "excluded from the test zip" is retired as a legacy
of the olympiad workflow. *If Polygon-side import double-counts samples, overturn this with one
line and I will flip the default and the manifest flag together.*

**D-02 — language detection: MANIFEST is authoritative.**
I am **not** changing statement authoring (a single-language statement still carries no language
tag). Maestro must read `languages` from `MANIFEST.json` and must not infer the language set by
parsing the statement. The characteristics file carries the same list; PREFLIGHT check PF-11
asserts the two agree and that both agree with what is actually in the file.

**D-03 — default language set: `["EN","RU"]`.**
Per playbook §2, which supersedes my older English-only habit. TJ/UZ only on explicit request.
Every set states its language set in the manifest regardless, so a per-set override is data,
not drift.

**D-04 — checker identity is structured, not prose.**
`MANIFEST.json` carries `checker: {"kind": "native"|"custom", "name": "ncmp"|null,
"polygon_id": "std::ncmp.cpp"|null}`. Maestro should prefer setting Polygon's built-in checker
by `polygon_id` and treat the bundled `checker.cpp` as a fallback. *Verify the exact
`std::*.cpp` id strings against your Polygon instance once — I am confident of the naming
convention but not of every id.*

### B.7 Determinism amendments

These change how I build, and they are the reason the contract can promise stable checksums.

1. **Seeded generation.** The generator seeds from the slug:
   `seed = int(sha256(slug.encode()).hexdigest()[:16], 16)`. This is different for every
   problem (satisfying the playbook's anti-correlation rule) *and* reproducible. The seed is
   recorded in `MANIFEST.json`.
2. **Deterministic zips.** All entries written in sorted path order, fixed timestamp
   `1980-01-01 00:00:00`, fixed `ZIP_DEFLATED` level, no extended attributes. Same inputs →
   byte-identical archive → stable `sha256`.
3. **Post-dedup counts only.** Test counts are read from the built `testset/` after pooled
   s0+s1 MD5 deduplication, never estimated.

With (1)–(3), rebuilding a set from the same source reproduces every archive byte-for-byte.
Without them — i.e. for every set delivered before this contract — it does not.

### B.8 Ordering and identity

- `idx` is 1-based, assigned by the difficulty total order defined in `CHARACTERISTICS_SPEC.md`
  §3. That order is a *total* order (score, then reference-solution LOC, then slug
  lexicographic), so ties cannot reorder between runs.
- The `idx` in `characteristics.md`, the array order in `MANIFEST.json`, and the numbering of
  the Suggested-tags section are the same ordering. PREFLIGHT PF-06 asserts it.
- The slug is the only join key. Titles, indices and filenames are all derived views.

---

## APPENDIX A — Known drift and ambiguity (candid list)

Ordered by how likely each is to break an unattended pipeline.

**Blocking — would fail or corrupt a run today**

1. **No durable output folder.** Delivery is chat downloads from an ephemeral container.
   Maestro has nothing to watch until you wire a copy step. (§A.1)
2. **No completion signal.** Nothing today distinguishes "still writing archive 12 of 25" from
   "done". Long batches are delivered across multiple messages. (Fixed by `MANIFEST.json`.)
3. **Samples-in-testset is contradicted inside the playbook itself**, and both behaviours
   exist in shipped sets. Silent double-import or silent zero-import. (§A.5 / D-01)
4. **Builds are not reproducible.** Unseeded RNG plus dedup means test contents *and counts*
   change per rebuild; checksums are meaningless before B.7. (§A.9)
5. **`{slug}-tests` has no rule at all.** Name and internal layout are ad-hoc. (§A.3)
6. **No validator is ever produced.** If Maestro's Polygon step expects one, it fails on
   every problem. (§A.4)
7. **Only one solution ships.** No second model solution for Polygon to cross-verify. (§A.4)

**Metadata — would mis-tag the right problem, or tag the wrong one**

8. **Two incompatible tag vocabularies coexist.** The characteristics template recommends the
   Codeforces set with spaces (`brute force`, `dfs and similar`); Notion `Topic Tags` uses
   lowercase-dashed (`frequency-array`, `custom-comparators`). Same concept, two spellings, and
   the template explicitly calls its list "a recommendation, not a hard whitelist" — i.e.
   free-text in practice. This will break an automated assign step.
   (Fixed: closed vocabulary + Notion mapping in `CHARACTERISTICS_SPEC.md` §4.)
9. **Difficulty buckets have drifted across sets**: `easy/medium/hard` (template),
   `Easy/Mid/Hard` (contest sets), `Very Easy/Easy/Mid/Hard` (Week 3 greedy), and raw
   Codeforces-style ratings 800–1900 (Republican Olympiad). Four schemes, no conversion.
   (Fixed: single 3-bucket scheme + scored rubric, `CHARACTERISTICS_SPEC.md` §3.)
10. **Difficulty assignment itself was intuition.** No rubric, no recorded justification, not
    reproducible even by me. (Fixed: 5-axis score, emitted per problem.)
11. **Language default is contradicted three ways** — playbook says EN+RU, recent batches
    shipped EN-only, older contests shipped four. (Fixed: D-03 + manifest field.)
12. **Single-language statements carry no language tag**, so the statement's structure differs
    between 1-language and n-language builds. (Fixed by D-02: don't parse, read the manifest.)
13. **`checker: native` vs the bundled `checker.cpp`.** The characteristics file says
    `ncmp (native)` while a full copy of ncmp also sits in the archive. Whether Polygon should
    use its built-in or upload the file was never stated. (Fixed by D-04.)

**Format — the consumer will guess, and sometimes guess wrong**

14. **`characteristics.md` format is specified twice, incompatibly.** Playbook §11 mandates
    uppercase plain-text section titles, blank-line separation, explicitly *no* `---` rules, and
    a 5-column General table with no tags/checkers/notes sections. `characteristics-template.md`
    mandates `## Title Case` headers, `---` rules, bullet lists, a 10-column General table, and
    numbered Suggested-tags/Checkers-used sections. The playbook declares itself the winner on
    conflict *and* tells me to follow the template. **I cannot satisfy both, and which one I
    followed has depended on which set.** This is the single worst ambiguity in the current
    instructions. (Resolved in favour of the template, which matches your Deliverable-2
    description; `CHARACTERISTICS_SPEC.md` is now the sole authority.)
15. **The file extension changed** from `characteristics.txt` to `characteristics.md` without
    the section format being migrated with it.
16. **Playbook §10's per-problem table is malformed** — the last two rows have two columns
    where the header has three, and "Suggested Memory Limit" is given in `{x}s` (seconds) for a
    memory value. It has been reproduced with that defect.
17. **Prefix grouping is a heuristic.** "Longest match" on lesson prefixes has no ground truth;
    `edu-greedy-exchange-*` vs `edu-greedy-*` is a judgement call I make per batch.
18. **Empty-section handling is newer than most sets.** The `[none]` convention exists in the
    template; older deliveries omitted empty sections entirely, which is indistinguishable from
    a section being dropped.
19. **`.mdx` extension on a LaTeX file.** Harmless to me, a trap for any tooling that trusts
    extensions.
20. **Titles are unconstrained display strings** — spaces, `!`, apostrophes — while everything
    else is strict lowercase ASCII. Any un-escaped use in a path or shell command breaks.
21. **TL/ML are defaults with ad-hoc bumps** (1 s / 256 MB, raised "when warranted"). The
    warrant was never recorded, so a bumped limit looks identical to a typo. (Fixed:
    `MANIFEST.json` requires a `limits_rationale` whenever a limit deviates from default.)

**Process**

22. **Corrections are absorbed forward, not retroactively.** Rules like `\texttt{}` over
    `{\tt …}`, S1-not-S3 for single subtasks, and out-of-tree binary compilation each landed
    mid-history. Older archives on disk predate them. A "re-verify everything ever shipped"
    pass has never run.
23. **Per-problem chat "Characteristic Table" (§10) and the batch `characteristics.md` (§11)
    overlap but disagree** in fields and vocabulary, and only one of them is a file.
24. **I cannot enforce any of this on myself across sessions.** These documents are only as
    binding as the project instructions that load them. `PREFLIGHT.md` exists so that a
    failure is at least *visible* in the transcript rather than discovered downstream.
