# Phase 1 findings — cross-actor analysis

*All three actors returned Phase 1. This document records what only becomes visible when the
three reports are read against each other. Each actor was truthful and thorough about its own
system; every problem below sits in the gaps **between** them, which is exactly where nobody
was looking.*

**Evidence status.** The problem-developer's six files are in `docs/contracts/` (full text).
The Polygon Middleman and Platform Scraper deliverables exist in their own repos; here they are
represented only by their **chat summaries**. Claims sourced from those summaries are marked
`[summary]` and must be re-checked against the actual documents. See §7 for what is still needed.

---

## 1. The seam does not match. A shaping step is mandatory.

This was the designated highest-risk unknown. It resolves badly, which is the useful outcome —
it was going to resolve badly whether or not anyone asked.

**Polygon side — confirmed against a real extracted package** `[summary]`. The Middleman
downloaded and unzipped problem #563392 from the live account. The `standard` package is:

- **flat at the root** — `problem.xml`, `check.cpp`, `files/`, `scripts/`, `solutions/`,
  `statement-sections/{english,russian}/`, `statements/`, `tests/` (194 entries, 276 total)
- **one problem per package.** Multi-problem packages do not exist.
- **no slug anywhere in the path.** The identifier appears only in the download *filename*
  (`{problemId}-r{revision}-{type}.zip`) and inside `problem.xml`.
- **the app downloads but never extracts.** Extraction is Maestro's job.

**ElectiCode side — the uploader wants a parent directory of per-problem folders**, with the
slug derived from the folder name (per the plan; the Scraper's `expected-input-tree.txt` has not
yet been read).

**Therefore Maestro must build a shaping stage between 5 and 6** that, per problem:

1. extracts `{problemId}-r{rev}-standard.zip`
2. creates `<parent>/<slug>/` — and **the slug is not present in the Polygon artifact's path**,
   so it must come from Maestro's own import-time identity map, or be parsed out of `problem.xml`
3. lays out the contents in whatever shape the uploader accepts, which may be a rename or may be
   a genuine transformation

Step 3 is the open question, and it is the difference between an afternoon and a subsystem.
`expected-input-tree.txt` decides it. Until then the shaping stage is scoped as "unknown, at
least a rename."

**Consequence for the roadmap:** stage 5→6 is no longer a hand-off, it is a component. It needs
its own state, its own failure mode, and its own tests.

---

## 2. Identity is broken at both ends, and the audit cannot catch it

Each actor reported its own half. Together they describe a system in which metadata can be
written to the wrong problem and nothing anywhere will notice.

| Link | State | Source |
|---|---|---|
| slug → Polygon problem | Package filename carries the **numeric problem id only**; slug lives in `problem.xml` or in Maestro's own record | `[summary]` Middleman §5 |
| Polygon → ElectiCode | Upload returns **no IDs**; discoverable only by a follow-up scrape | `[summary]` Scraper §6 |
| ElectiCode addressing | Downstream tools resolve a slug by **page search** and take `.first` **without re-verifying the row's slug** — a substring slug can hit the wrong problem and report success. `list add` is the sole exact-match exception | `[summary]` Scraper §6 |
| Detection | `report.py audit` is **presence-only** (`report.py:60-66`) — it checks that *some* value exists, not that it is correct or on the right problem | `[summary]` Scraper §12 |

Read together: **a mis-targeted assign passes a green audit.** There is currently no point in
the chain where this class of error is detectable, which makes it a data-integrity hole rather
than a robustness gap. Slug prefixes in this corpus are highly collision-prone by construction
(`edu-greedy-*`, `edu-binary-search-*`), so this is a live risk, not a theoretical one.

**This changes the Scraper's Phase 2 priority order.** Exact-match slug resolution moves to the
top, ahead of `--json`: observability into a process that can still silently corrupt is worth
less than making the corruption impossible. See §6.

---

## 3. Three of the mutating tools cannot express failure

`[summary]` Scraper §2:

- `list_editor reorder` returns **0 even when the final order does not match the target**
  (`list_editor.py:784-792`)
- `problem_editor detail` returns **0 on item failures** (`:569`)
- `problem_uploader upload` **cannot express partial failure at all** — one batch submit,
  success is defined as "the modal closed" (`:278`)

Maestro was designed to gate on exit codes. For these three tools, an exit code of 0 currently
means "the process did not crash." Gating on that is worse than not gating, because it
manufactures false confidence.

Until Phase 2 lands, Maestro must treat those three as **unverified** and require an independent
post-condition read — which needs §2's exact-match addressing to be trustworthy. The two items
are entangled: neither is sufficient alone.

---

## 4. There is no auth story at all

`[summary]` Scraper §4: no non-mutating session check, no expired-session exit code anywhere.
Every tool only tests `state_path.exists()` — i.e. it checks that a cookie *file* is present,
never that the session behind it is alive. An expired session degrades to a generic `2`, or, in
`list reorder`, **silently exits 0 having done nothing** (`:764-765`).

The `BLOCKED_ON_AUTH` parked state cannot be implemented until a real session check exists. Until
then, the pre-flight is a file-existence check, which will pass on a dead session and let a
40-minute batch start and half-apply.

---

## 5. Four contradictions the actors could not see individually

### C-1 · `characteristics.md` is now specified twice, independently

The problem-developer wrote `CHARACTERISTICS_SPEC.md` v1.0 — 10-column General table, numbered
positional tags/checkers, `[none]` for empty sections, prefix grouping, a 5-axis difficulty
rubric, a closed 42-tag vocabulary. It says of itself: *"Expected to be superseded: you said the
authoritative format is coming from the consuming tool."*

The Scraper wrote `characteristics.schema.md` from the actual parser at `batch.py:131` `[summary]`.

**They will not match.** And the failure mode is the dangerous one: `[summary]` the parser is
**lenient and never raises**. A format mismatch therefore does not error — it silently yields
empty or partial metadata, which then passes a presence-only audit (§2). Two independently
reasonable design decisions compose into silent data loss.

**Resolution:** the Scraper's schema wins on **layout** (it is the consumer). The developer's
spec wins on **semantics** — the closed vocabulary, the difficulty rubric, the determinism
amendments are policy the parser does not enforce and should not. Maestro validates the
intersection *before* invoking `batch.py`, and never relies on `batch.py` to reject a bad file.

### C-2 · Validator and second solution vs. Polygon verify — **RESOLVED, not a blocker**

> **Answered empirically.** Verify passes with one solution and no validator; 220/220 existing
> problems have `READY` verified packages. See `resolved-questions.md`. The text below records
> the original concern and why it was worth checking.

The developer is unambiguous: **no `validator.cpp` is ever produced** (validator slot empty on
every problem ever delivered), and **exactly one solution ships** — the Python reference is never
included, so Polygon has nothing to cross-verify against.

The Middleman's stage 4 is `buildPackage(full=false, verify=true)`. **Neither report states
whether Polygon's verify step requires a validator, or requires more than one solution.**

If it does, stage 4 fails on **every problem, permanently**, and the fix is a large change to how
problems are authored — not a Maestro change at all. This is a single empirical question that
gates the entire pipeline and nobody has answered it. It is the first thing to resolve. See §7.

### C-3 · Sample tests: double-import or zero-import — **RESOLVED, works**

> **Confirmed in code and in Polygon.** `input_s0_*.txt` → group `"0"` → `useInStatements: true`.
> The authoring contract's mandatory-`s0` rule closes the one caveat. See `resolved-questions.md`.

The developer's D-01 pins `samples_in_testset: true` — `s0` files ship inside `testset/`. Their
statement format deliberately omits `\textbf{Example input}` / `\textbf{Example output}` blocks
because *"Polygon manages samples separately."*

So samples exist **only** as `testset/input_s0_*.txt`, and the import pipeline must explicitly
mark those tests as samples in Polygon (`useInStatements`). The Middleman's §3 pipeline trace
should say whether it does; the summary does not.

- If it marks them → correct.
- If it does not → statements render with **no examples at all**. Every student sees a problem
  with no worked sample. This passes verify, passes the audit, and is a pure quality failure
  discovered only by a human reading the published problem.

### C-4 · The developer's "biggest structural gap" is already solved by their own design

`OUTPUT_CONTRACT.md` §A.1 flags, as the single biggest gap, that there is **no durable output
folder** — delivery is chat downloads from an ephemeral container, so Maestro has nothing to
watch.

That is true and it does need a wiring decision. But it is **not a blocker**, because
`MANIFEST_SPEC.md` already neutralises it: Maestro ingests only when `MANIFEST.json` is present
*and* every archive it names exists with a matching `sha256` (cross-checks M-2, M-3). Completeness
is validated **by content, not by arrival order** — so a human downloading 25 zips and dropping
them into a watched folder in arbitrary order, over several minutes, is safe. A partial drop
looks unfinished, which is the correct signal.

Stage 1 stays manual in v1, exactly as the plan intended. The sentinel design is sound; it just
needs a folder to land in.

---

## 6. Revised Phase 2 priorities

### Platform Scraper — reordered

The original list is unchanged in content but wrong in order, now that §2 and §3 are known.

| # | Item | Why here |
|---|---|---|
| 1 | **Exact-match slug resolution, fail on ambiguity** *(new)* | Correctness. Without it every downstream write can silently target the wrong problem (§2). |
| 2 | **Stable exit codes, partial failure distinguishable** | Detection. Fix `list_editor reorder`, `problem_editor detail`, `problem_uploader upload` first (§3). |
| 3 | **Non-mutating session check + expired exit code** | Pre-flight; unblocks `BLOCKED_ON_AUTH` (§4). |
| 4 | **`report.py audit --char characteristics.md`** *(new)* | Turns a presence check into a correctness check — the only thing that catches §2 in production. |
| 5 | `--json` NDJSON on every tool | Observability. |
| 6 | `batch.py validate --char` | Fail a bad characteristics file in seconds, not 40 minutes (§C-1). |
| 7 | `--only` / `--skip` stage selection | Resume after mid-chain failure. |

Items 1–4 are correctness; 5–7 are ergonomics. The original order had ergonomics first.

### Polygon Middleman — scope confirmed, with two prerequisites

They are right that the framing was stale: `runImportPipeline` is already in the backend, but
**synchronously** — `POST /api/import/problem` blocks for the entire import `[summary]`
(`main.py:764`). A multi-minute blocking HTTP call is precisely what breaks under an orchestrator:
no progress, no correlation id, no resumability, and a timeout that leaves indeterminate state.

**Confirmed scope:** async + job-based, `GET /api/verify-status/{id}`, React migrated onto the
same endpoints, TS duplication deleted, plus per-error codes (§9 taxonomy) since retry-vs-halt
currently requires parsing free-text.

**Two things to answer before writing any of it**, because either could change what gets built:

- **C-2** — does a package with one solution and no validator pass `buildPackage(verify=true)`?
  One empirical run against a real problem.
- **C-3** — does the import pipeline mark `s0` tests as samples?

Also noted and useful: **Polygon `FAILED` arrives as HTTP 200** on the raw proxy endpoints
(`main.py:160-165`) — read the body `status`, never the HTTP code. And there is **no rollback**:
commit is gated on `errors===0`, so a mid-pipeline failure leaves an *uncommitted* working copy
and re-running is idempotent (`pipeline.ts:145-146`). That last part is good news — retry is safe.

---

## 7. What is still missing

### From the Platform Scraper

| Priority | File | Unblocks |
|---|---|---|
| 1 | `docs/maestro/expected-input-tree.txt` | The seam diff (§1) — decides rename vs. transform |
| 2 | `docs/maestro/characteristics.schema.md` | The format diff (§C-1) |
| 3 | `docs/MAESTRO_CLI_CONTRACT.md` | §5, §6, §9 especially |
| 4 | `docs/maestro/tools.json` | Invocation contracts for the process supervisor |
| 5 | `docs/maestro/characteristics.example.md` | Generator target for the developer |

### From the Polygon Middleman

| Priority | File | Unblocks |
|---|---|---|
| 1 | `docs/maestro/package-tree.txt` | The seam diff (§1) |
| 2 | `docs/MAESTRO_INTEGRATION.md` | §3 (sample marking → C-3), §4, §5 |
| 3 | `docs/maestro/errors.md` | Retry-vs-halt policy |
| 4 | `docs/maestro/openapi.json` — `/api/import/*` paths only | Client generation |

### Answers, not files

- **C-2** — validator/second-solution vs. verify. Empirical, one run, gates everything.
- **C-3** — sample marking in the import pipeline.
- **D-04** (developer's) — confirm the `std::ncmp.cpp`-style Polygon checker ids against the real
  instance.
- Whether ElectiCode's difficulty field accepts exactly `easy|medium|hard`, and whether its
  category/tag store accepts the developer's closed 42-tag vocabulary. Two vocabularies were just
  frozen on the authoring side against an unverified target.
