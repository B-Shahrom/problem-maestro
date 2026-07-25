# Upload modal + division page — read from live DOM captures

*Two operator captures, taken with the run0 folder loaded so an `EXISTS` row actually renders.
Settles the per-problem `exists` question the Scraper flagged as capture-gated, and turns up
three things nobody was looking for.*

---

## 1. `EXISTS` is per-row. The parsing fix works.

The detected-problems list nests one row per problem, and the badge sits inside the same
flex container as the id input:

```html
<div class="flex items-center gap-3 p-2 ...">                     <!-- ROW -->
  <input type="checkbox" checked>
  <div class="flex-1 flex items-center gap-2">
    <span>edu-tree-applications-equal-population-regions</span>   <!-- folder name -->
    <input type="text" value="edu-tree-applications-equal-population-regions">
    <span title="Will overwrite: &quot;Equal Population Regions&quot;">EXISTS</span>
    <span>256 files</span>
  </div>
</div>
```

The climb in the proposed fix terminates correctly: from the text input, `div.flex-1` holds one
text input (the checkbox is `type=checkbox`), the row div holds one, and the list container
holds two — so the walk stops at the row, whose `innerText` contains `EXISTS`.

**Verdict: a parsing change, as hoped — now on evidence rather than inference.**

## 2. Three things the capture reveals that were not being asked about

### 2.1 The badge names its victim

`title="Will overwrite: &quot;Equal Population Regions&quot;"` — the platform states the
**display name** of the problem the upload will replace. That is a free identity cross-check
available *before* submitting: if the title it intends to overwrite is not the title Maestro
expects for that slug, something is mismatched and the run should stop rather than submit.

Worth capturing into `--output` alongside `exists`.

### 2.2 Per-problem selection already exists

Every row carries `<input type="checkbox" checked>`. **Upload is not inherently all-or-nothing.**

This supersedes the workaround in the Phase 2 corrections, which said Maestro must rebuild the
parent folder to control which problems upload. It can instead untick rows — cheaper, and it
avoids copying a multi-hundred-megabyte tree to exclude one problem. Worth a Scraper item:
`problem_uploader upload --only <slug>[,<slug>]`, ticking only the named rows and refusing on
any name it cannot find.

### 2.3 The Problem ID is editable, which answers the slug question

The row's `<input type="text" value="…">` is the **Problem ID**, pre-filled from the folder name
and editable before submit. So:

- the folder name is the **default** slug, not a derived one, and
- the slug is **controllable at upload time** by writing that field.

This closes the open item in `seam-verdict.md` §2 and `electicode-fields.md` §4. Stage 6.5's
reconcile stays as a guard, but the identity chain is now explicit rather than assumed — and if
a mismatch ever appears, Maestro can *fix* it by setting the field rather than only detecting it.

## 3. The platform filters binaries itself

Processing log, verbatim: `Filtered out 4 binary files (.exe, .dll, etc.)`.

So the `delete *.exe` step in the shaping recipe is **not required for correctness**. Keep it
anyway — it avoids pushing four Windows executables per problem through a multipart upload — but
it is an optimisation, not a fix. The shaping stage is now genuinely just: extract, name the
folder, upload.

---

## 4. The division page embeds the entire catalog as JSON

The bigger find. The Division Access page ships a hydration payload containing **every problem**,
roughly 2,000 entries, each:

```json
{"id":"edu-greedy-1-activity-selection","name":"Activity Selection","test_count":31,
 "time_limit_ms":1000,"memory_limit_kb":262144,"difficulty":"","category":""}
```

`id` is the slug. That single page load gives, for the whole catalogue:

- **the identity map** — slug → name, without paginating 40 pages of the problems list. Stage
  6.5's reconcile becomes one fetch.
- **difficulty and category per slug** — which is exactly what `report.py audit --char` needs to
  compare *actual against expected* instead of merely checking presence. The expensive part of
  that item turns out to be nearly free.

### 4.1 But the division table's rows show only the title — and 28 titles are ambiguous

The rendered rows expose the **display name only**, no slug:

```html
<td class="px-6 py-4 text-sm">
  <div class="font-medium ...">Activity Selection</div>
  <div class="flex flex-wrap items-center gap-2 mt-1"></div>
</td>
```

So division exact-match has to go slug → name (from the payload) → row. That works **except**
where names collide, and they do: of ~2,000 titles, **28 are not unique** — `A + B` appears
**three** times (`a-plus-b-in-binary`, `a-plus-b-subtask`, `count-a-plus-b-equal-to-c`), and 27
more appear twice (`Ancestor Queries`, `Beautiful Numbers`, `Circle Handshakes`, `Distance
Queries`, `Divisors`, `Equation`, …).

**The correct resolver:** map slug → name from the embedded payload; if that name is unique
across the payload, match the row by exact name; **if not, refuse** — the DOM carries nothing
that could disambiguate. That is the same fail-on-ambiguity rule already applied in the editor
path, and it keeps ~56 problems from being silently mis-targeted.

A cleaner fix, if the platform team is reachable: render the slug into the division row the way
the problems list does. Then the payload lookup is unnecessary and all 2,000 resolve exactly.
