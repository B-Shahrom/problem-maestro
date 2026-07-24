# ElectiCode edit modal — verified against the live DOM

*Source: `problem_editor.py dump` capture of the admin edit modal, run by the operator on a
live session. Settles the two items left open in `resolved-questions.md`.*

---

## 1. Difficulty — closed, fully verified

```html
<select id="difficulty">
  <option value="">Not set</option>
  <option value="Easy">Easy</option>
  <option value="Medium">Medium</option>
  <option value="Hard">Hard</option>
</select>
```

`value` equals the label on all four options, so `select_option("Easy")` resolves whether
Playwright matches by value or by label — the one ambiguity that could have broken this is
absent.

**The whole chain is now verified end to end:**

| Link | Value | Source |
|---|---|---|
| Author emits | `easy` / `medium` / `hard` (lowercase) | `CHARACTERISTICS_SPEC.md` §2 |
| Parser normalizes | lower-cases, then `_DIFF` → `Easy`/`Medium`/`Hard` | `batch.py:143, 161` |
| Toolkit constant | `_VALID_DIFFICULTY = ("", "Easy", "Medium", "Hard")` | `problem_editor.py:414` |
| Live DOM | `""`, `Easy`, `Medium`, `Hard` | this capture |

Exact match at every hop, including the empty string for "Not set". Nobody coordinated this.
**Difficulty is closed — no further risk, no action needed.**

## 2. Category — unconstrained, and carrying two taxonomies at once

```html
<input id="category" type="text"
       placeholder="e.g., Arrays, Dynamic Programming, Greedy"
       value="academy exam, sliding window, bitmasks, two pointers">
```

No `maxlength`, no `pattern`, no `list` (no datalist), no `required`. Plain comma-separated free
text, exactly as the code implied. Confirms the fail-silent path in `resolved-questions.md` F-2.

Two things in that one `value` attribute matter more than the absence of validation:

### 2.1 `academy exam` is a real production tag, and it is not in the vocabulary

The closed 42-tag vocabulary covers **algorithmic topics**. This problem's categories are
`academy exam, sliding window, bitmasks, two pointers` — three algorithmic tags plus one
**curricular/organisational** label describing where the problem is used.

So the field carries two orthogonal taxonomies simultaneously, and the vocabulary only models
one of them. Consequences:

- **`MANIFEST_SPEC.md` M-9 as written would halt on a legitimate problem.** "Every `tags[]` entry
  is in the closed vocabulary → unknown tag → halt" is right for *authored* tags and wrong for
  curricular ones. M-9 needs to scope to the algorithmic axis, or the vocabulary needs a second
  namespace.
- The vocabulary is not merely unenforced downstream — it is **already inconsistent with
  production data**. That is a stronger statement than F-2 made.

### 2.2 The platform's own placeholder disagrees with its own data

Placeholder: `Arrays, Dynamic Programming, Greedy` — **Title Case**.
Stored value: `academy exam, sliding window, bitmasks, two pointers` — **lowercase**.

Two casing conventions coexist inside a single rendered form. The authoring vocabulary is
lowercase, which matches the data rather than the hint — the right choice, but by luck rather
than by anyone checking.

### 2.3 The data-loss risk, and why it is already avoidable

`problem_editor.py:347` applies categories with `page.locator("#category").fill(value)`.
**`fill()` clears the field and types** — it is a full replace, not an append. Applying authored
tags to a problem that already carries `academy exam` would **destroy** that tag, silently, and
pass the presence-only audit.

**The toolkit already has the safe path.** `tools.json` shows `problem_editor assign` exposes
`--category`, **`--category-add`**, and **`--category-prepend`**, and `batch.py run` exposes
`--tags-mode` (plus `--extra-category` / `--extra-category-overwrite`).

So this is a **configuration decision, not a code change**:

- **Fresh imports** (problem created by this run) → `--category` reset is correct.
- **Any problem that already exists** → `--tags-mode add` / `--category-add`, or authored
  curricular tags get wiped.

Maestro knows which case applies: `problem_uploader` reports `EXISTS` per problem at upload
time, and stage 6.5's catalog reconcile confirms it. **The reset/add choice must be driven from
that signal per problem, never set globally for a batch.** A batch that mixes new and existing
problems and uses a single global mode will be wrong for one half of it.

## 3. Complete editable field map

Useful for scoping what Maestro can and cannot set through this surface.

| Control | Kind | Notes |
|---|---|---|
| `#displayName` | text | Display Name, per language |
| `#displayDescription` | textarea | per language |
| `#editorial` | textarea | *"Write editorial in MDX format…"* — the developer's optional `editorial.mdx` has a home |
| `#difficulty` | select | closed enum, §1 |
| `#category` | text | unconstrained, §2 |
| `#olympiadName` | text | *"e.g., National Olympiad, IOI"* |
| `#olympiadYear` | number | |
| `#olympiadCountry` | text | *"e.g., Tajikistan, Uzbekistan"* |
| `#input-N` / `#output-N` / `#explanation-N` | textarea | per-sample-test-case, added via **Add Test Case** |

Translation tabs: **English · Russian · Tajik · Uzbek** — exactly the four languages in
`OUTPUT_CONTRACT.md` §4, in the same order. Another independent alignment.

### Read-only S3 metadata

`S3 ID`, `S3 Name`, `Time Limit`, `Memory Limit`, `Test Count`, `Verifier Type` are displayed
**read-only**.

**So TL and ML are not settable here.** They flow Polygon → S3 → ElectiCode and are never
touched by the ElectiCode edit path. The developer's `TL`/`ML` columns and `limits_rationale` are
therefore documentation of a decision applied *upstream in Polygon*, not instructions to this
stage. Nothing in stage 7 should try to enforce them; the place to verify them is Polygon.

## 4. The slug, revisited — the pessimistic reading was probably wrong

The captured problem shows:

```
S3 ID:   edu-sliding-window-emergency-codes
S3 Name: Emergency Codes
```

That S3 ID is **exactly the authoring slug convention** (`edu-{lesson}-{title-part}`), and the S3
Name is the display title. This is strong circumstantial evidence that the slug **is** preserved
verbatim through Polygon → upload → ElectiCode.

`seam-verdict.md` §2 concluded the folder name does not determine the slug. That was drawn from
`problem_uploader.py:146-147` scraping the slug from the rendered modal — but on re-reading, that
describes how *the tool learns* the slug, not how *the platform derives* it. The two are
compatible: the platform most likely derives the S3 ID from the uploaded folder name, and the
tool reads it back rather than assuming.

**Revised position:** slug preservation is *likely* and now has supporting evidence, but is still
**not proven**. Stage 6.5 (upload → scrape → reconcile → halt on mismatch) stays, because it is
cheap and it converts a likely-true assumption into a checked one. It just becomes a guard rather
than an expected-failure path.

Also visible: **2,000 problems** in the database, so any full-catalog scrape in stage 6.5 must
paginate (40 pages × 50).
