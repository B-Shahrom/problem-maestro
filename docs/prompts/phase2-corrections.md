# Phase 2 — corrections to send with the go-ahead

*Both teams are cleared to start. Neither Phase 2 depends on the outstanding shaping test, so
waiting costs time and buys nothing. But three findings landed after the Phase 1 prompts were
written, and each one changes something a developer would otherwise build wrong. Send these
with the command, not after it.*

---

## To the Polygon Middleman

> **Green light on Phase 2 — async job model, per-error codes, React migration, delete the TS
> duplication. Your read of the scope was right.**
>
> One finding from your own C-2 investigation has to go into the error taxonomy before you build
> it, because it inverts a status.
>
> When you triggered a fresh build on an already-verified revision, Polygon returned:
>
> ```json
> {"status": "FAILED",
>  "comment": "problemId: There is already non-failed package for this revision with verification."}
> ```
>
> **That is a `FAILED` that means success.** The package exists, is `READY`, and is usable.
>
> Maestro's stage 4 polls `READY`/`FAILED` and halts the batch on `FAILED`. Retry, resume, and
> idempotent re-run all hit this path — they are the paths a resumable orchestrator exercises
> most, so this is the *first* failure it would encounter, not an edge case. Today the only way
> to tell it apart from a real build failure is string-matching free-text prose, which is exactly
> what your §9 gap is about.
>
> Two things this implies for Phase 2:
>
> 1. **`already-verified` needs its own error code**, distinct from a genuine verify failure, and
>    it should carry enough for a client to act: the existing package is fetchable, proceed. Do
>    not fold it into a generic `BUILD_FAILED`.
> 2. **Retry semantics are per-stage, not global.** Re-running *import* is idempotent (it fills
>    and overwrites the same problem, `pipeline.ts:145-146`). Re-running *build* is **refused**.
>    A single "retry the stage" policy is wrong for one of them. Please make the taxonomy say,
>    per error, whether the correct client action is retry, halt, or **treat as success and
>    continue** — that third category currently has no representation and it is the one that
>    matters here.
>
> Also worth recording explicitly, since Maestro depends on it: **Polygon `FAILED` arrives as
> HTTP 200** on the raw proxy endpoints (`main.py:160-165`). Read the body `status`, never the
> HTTP code. Your `/api/import/*` endpoints already fold this correctly — please keep that
> property in the async version, and state it in the OpenAPI description so it survives.

---

## To the Platform Scraper

> **Green light on Phase 2.** Six of the seven items need nothing new from me — start with
> exact-match slug resolution, it is the highest-value change in either codebase. Three
> corrections before you get to the rest.
>
> **1. `EXISTS` needs to be per-problem, and this is new scope.**
>
> Your §5 documents `EXISTS` as `re.findall(r"\bEXISTS\b", body, re.I)` (`:67`) — a **count** of
> the literal word in the modal text, reported to stderr and otherwise unused.
>
> A count is not enough. We confirmed from a live DOM capture that `#category` is applied with
> `fill()`, which **replaces** the field rather than appending — and that production problems
> already carry tags that were not authored by this pipeline (a real problem in your catalogue
> has `academy exam, sliding window, bitmasks, two pointers`, where `academy exam` is
> curricular, not algorithmic). So writing authored tags onto an existing problem **destroys**
> its curricular tags, silently, and the presence-only audit passes.
>
> The fix is configuration, not code — you already expose `--category-add`, `--category-prepend`,
> and `batch.py run --tags-mode`. But the reset-vs-add choice has to be made **per problem**:
> reset for a freshly created problem, add for one that already existed. A batch mixing both with
> a single global mode is wrong for half of it.
>
> Maestro can only make that call if it knows *which* problems existed. So please add to Phase 2:
> **`problem_uploader upload --output` should report `exists: true|false` per detected problem**,
> not just an aggregate count. You are already scraping per-problem IDs from the modal in
> `_read_detection` (`:146-147`), so this should be a parsing change rather than a new
> capability — but please confirm that, because if the modal does not associate the `EXISTS`
> marker with a row, this becomes a real piece of work and I would rather know now.
>
> **Do this before you freeze the `--json` event schema** (item 5) — per-problem `exists` belongs
> in the upload events, and retrofitting a schema is worse than designing it in.
>
> **2. The tag vocabulary is two taxonomies, not one — do not build a single whitelist.**
>
> The 42-tag list I sent covers **algorithmic topics only**. Your catalogue also uses the same
> `#category` field for **curricular/organisational** labels like `academy exam`. A validator that
> rejects anything outside the 42 would reject legitimate production data.
>
> So for `batch.py validate --char` (item 6): validate authored tags against the vocabulary, but
> treat unknown tags as a **warning with the offending value named**, not a hard failure — and
> ideally let the caller pass an additional allow-list for curricular tags. Your original caveat
> stands and should be stated in the docs: this enforces an **authoring convention, not a
> platform constraint.** The live DOM confirms `#category` has no `maxlength`, no `pattern`, no
> `datalist`, and no `required` — the platform constrains nothing.
>
> **3. Revised priority order**, given that a bad difficulty throws at apply time while a bad tag
> lands silently:
>
> 1. exact-match slug resolution, fail on ambiguity
> 2. stable exit codes — `list_editor reorder`, `problem_editor detail`, `problem_uploader upload` first
> 3. per-problem `exists` in `--output` *(new, see 1)*
> 4. non-mutating session check + dedicated expired exit code
> 5. `report.py audit --char` — compare actual against expected, not presence
> 6. `--json` NDJSON *(after 3, so the schema carries `exists`)*
> 7. `batch.py validate --char` *(see 2)*
> 8. `--only` / `--skip` stage selection
>
> Items 1–5 are correctness. 6–8 are ergonomics. The original list had ergonomics too early.
>
> **Not blocking, for your awareness:** `#difficulty` is fully confirmed — the live options are
> `""`/`Easy`/`Medium`/`Hard` with `value` equal to label, matching `_VALID_DIFFICULTY` exactly.
> That chain is closed; no work needed. And TL/ML are **read-only** in the edit modal, so nothing
> in stage 7 should attempt to set them.

---

## What runs in parallel

The shaping test (`docs/procedures/shaping-test-upload.md`) is Maestro-side and operator-run. It
cannot invalidate either Phase 2 — the uploader hands the folder tree over untouched and does not
transform it, so whatever the test reveals changes Maestro's shaping stage, not the toolkit.

The one way it could reach back into the Scraper's scope is if ElectiCode turns out to reject the
shaped folder outright and the fix belongs in `problem_uploader`. That would be **additive** to
Phase 2, not a revision of it. Not a reason to hold.
