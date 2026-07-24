# CHARACTERISTICS_SPEC.md

**Spec version:** 1.0
**Status:** sole authority for `characteristics.md`. Supersedes playbook §11 and the
`characteristics-template.md` usage guide wherever they conflict.
**Expected to be superseded:** you said the authoritative format is coming from the consuming
tool. This spec is versioned so that swap is a version bump, not a rewrite — §2 (the file
layout) is the replaceable part; §3 (difficulty) and §4 (tags) are the parts worth keeping
regardless of what the consumer wants the file to look like.

---

## 1. Current state — answering your questions directly

### 1.1 Do I emit one today?

Yes, at the end of every batch — but the format is genuinely unsettled, and I want to be
precise about how unsettled.

There are two incompatible specifications live in the project right now:

| | playbook §11 | `characteristics-template.md` |
|---|---|---|
| filename | `characteristics.md` | `characteristics.md` |
| section headers | uppercase plain text (`GENERAL`, `ALL SLUGS`) | `## Title Case` markdown |
| separators | blank lines, **explicitly no `---` rules** | `---` rules between groups |
| list markers | bare lines, no bullets | `- ` bullets / `1.` numbers |
| General columns | idx, slug, title, group, tests | idx, slug, title, languages, group, tests, subtasks, checker, TL, ML |
| tags section | absent | present, numbered, positional |
| checkers section | absent | present, numbered, positional |
| notes section | absent | present |

The playbook declares itself the winner on conflict, and in the same section instructs me to
follow the template. Which one a given batch got has depended on when it was built. Earlier
batches also shipped it as `characteristics.txt`.

**Your Deliverable-2 description (General table with slug, title, difficulty group, subtasks,
languages, plus a numbered positional tags section) matches the template, not the playbook.**
This spec therefore resolves the conflict in favour of the template.

### 1.2 A real one, verbatim

I can't give you one honestly. I searched the project history and could retrieve the
*template's* authoring and its usage guide verbatim, but not a shipped instance from a recent
set — those live in chat transcripts whose full text I can't reconstruct reliably, and the
container that held the files has been reset. Rather than paste something plausible and let you
treat it as evidence, here is what I know for certain about shipped instances:

- Older batches (binary search 23-problem set, greedy Week 3/4) shipped `characteristics.txt`
  in the playbook §11 shape: uppercase section titles, blank-line separated, bare lists, a
  five-column General table, no tags section, no checkers section, no notes section.
- The 10-column / tags / checkers / notes shape exists as a template authored on 2026-07-24 and
  is the intended target; I have low confidence it has been used in a full shipped batch yet.

**So: assume no historical `characteristics.md` matches this spec.** §6 gives a worked example
that does, clearly marked as illustrative.

### 1.3 What metadata I already have per problem

Available today with no extra decisions, straight out of the build:

- slug, English title (and translated titles where translations ship)
- language set actually written into `problem_statement.mdx`
- subtask structure and point split (or `none`)
- test counts split s0 / s1..sN, read from `testset/` after dedup
- checker kind (standard-verbatim vs custom) and, if standard, which one
- reference solution source, its line count, its measured worst-case runtime
- whether overflow forced `long long` / `__int128`, and any pedagogical trap I noticed

Needed as a deliberate decision, and currently made by feel:

- **difficulty group** — §3 fixes this
- **tags** — §4 fixes this
- **target languages** — a per-set input, not something I should infer; default `EN, RU`
- **TL / ML** — defaults 1 s / 256 MB, bumped from measured runtime; the *justification* was
  never recorded, so §5 requires it

### 1.4 How I choose difficulty today

Honestly: intuition, calibrated within the batch rather than absolutely. I look at a set,
decide which problem is the gentlest, which is the nastiest, and spread the rest between them.

That has two consequences you should assume are true of every set delivered so far:

1. **It is batch-relative, not absolute.** The same problem could be `easy` in a hard set and
   `medium` in a gentle one. Cross-set comparison of the group column is not meaningful.
2. **It is not reproducible**, including by me. Re-run the same batch and some middle problems
   would move a bucket.

The bucket vocabulary also drifted: `easy/medium/hard`, `Easy/Mid/Hard`,
`Very Easy/Easy/Mid/Hard`, and raw Codeforces-style ratings (800–1900) have all been shipped.

§3 replaces all of that with a scored rubric whose inputs are mostly mechanical, and requires
the score vector to be emitted so a bucket is arithmetic you can audit rather than a judgement
you have to trust.

### 1.5 Tags

Free-text in practice. The template offers the Codeforces vocabulary but explicitly calls it
"a recommendation, not a hard whitelist", and separately the Notion sync uses a different
spelling convention (lowercase-dashed: `frequency-array`, `custom-comparators`) that includes
tags outside the Codeforces set entirely.

So today there are two vocabularies with different spellings and different membership, and
nothing stops a new tag appearing. **This will break an automated assign step.** §4 closes it.

---

## 2. File layout (normative)

`characteristics.md`, UTF-8, LF, one per set, in the set folder.

Section order is fixed. Every section is present, always, even when empty — an empty list
section contains the single line `[none]` with no bullet or number.

```
# Characteristics — {SET NAME}

_{one-line set description}_

_Ordering within every list is easiest → hardest, matching the General table row order._

---

## General

| idx | slug | title | languages | group | tests | subtasks | checker | TL | ML |

**TOTAL problems:** N
**TOTAL tests:** N

---

## All slugs
## All titles

---

## Easy
## Medium
## Hard

---

## {prefix} slugs
## {prefix} titles          ← one pair per distinct prefix, in first-appearance order

---

## Suggested tags

---

## Checkers used

---

## Notes worth flagging
```

Column rules for **General**:

| column | rule |
|---|---|
| `idx` | 1-based, assigned by the §3 total order. The only numeric column. |
| `slug` | exact slug, matching the archive filename stem. |
| `title` | English display title, verbatim, unescaped. |
| `languages` | subset of `EN, RU, TJ, UZ` in exactly that order, comma-space separated. Read from the shipped statement, cross-checked against `MANIFEST.json`. |
| `group` | exactly one of `easy`, `medium`, `hard` — lowercase, no other value permitted. |
| `tests` | `N (a+b)` where `a` = s0 count, `b` = sum of all s1..sN counts, `N = a+b`. Read from the built `testset/`. |
| `subtasks` | `none`, or `S0(0), S1(p1), S2(p2), …` with points summing to 100 across non-sample subtasks. |
| `checker` | `{name} (native)` for a verbatim standard testlib checker, or `custom`. |
| `TL` | `{n} s`. Default `1 s`. |
| `ML` | `{n} MB`. Default `256 MB`. |

`Suggested tags` and `Checkers used` are **numbered positional lists**: line *k* describes
General row *k*, and the number equals that row's `idx`. Never partially filled — if a row
exists in General, it has exactly one line in each.

`Notes worth flagging` keeps its `- **{slug}:** {note}` bulleted, slug-referenced form. It is
the only section where the item is keyed by slug rather than by position, because it is free
prose and may have zero or several entries per problem.

---

## 3. Difficulty — the reproducible rubric

Difficulty is a **score**, and the group is a **lookup on that score**. Five axes, each scored
0–3, summed to 0–15.

### 3.1 Axes

**A. Prerequisite distance** — how far past the lesson's own material the solution reaches.
- 0 — uses only the current lesson's construct, applied directly
- 1 — current lesson plus one earlier construct
- 2 — combines two or more techniques, or a technique from a later lesson
- 3 — needs something not taught anywhere in the course track

**B. Insight** — how far the solution sits from a literal reading of the statement.
- 0 — the statement describes the algorithm; implement it as written
- 1 — one routine transformation (sort it, count it, prefix it)
- 2 — one non-obvious observation is required before any code is correct
- 3 — a chain of two or more observations, or a proof obligation

**C. Implementation weight** — line count of `solution.cpp` excluding blanks, comments and
includes. **Mechanical, measured, not judged.**
- 0 — ≤ 25 lines
- 1 — 26–60
- 2 — 61–120
- 3 — > 120

**D. Constraint pressure** — how much the limits punish a naive approach.
- 0 — the obvious brute force passes comfortably
- 1 — brute force fails, the standard approach passes with room
- 2 — the intended complexity is required and margin is under ~4×
- 3 — constant factors matter (I/O choice, memory layout, `long double` avoidance)

**E. Trap density** — how many correct-looking submissions die on something other than the
main idea.
- 0 — none
- 1 — one (an overflow, or one boundary)
- 2 — two or three (overflow *and* a sentinel init *and* an empty-output edge)
- 3 — four or more, or one genuinely subtle tie-break/precision contract

### 3.2 Buckets

| total | group |
|---|---|
| 0–4 | `easy` |
| 5–9 | `medium` |
| 10–15 | `hard` |

### 3.3 Total order for `idx`

Sort ascending by:

1. total score
2. then axis **C** (implementation weight raw LOC, ascending)
3. then axis **B** (insight, ascending)
4. then slug, ASCII lexicographic ascending

Four keys, the last of which is unique within a set → the order is total and stable. Two runs
over the same built packages produce the same `idx` assignment.

### 3.4 Auditability

The full score vector is written to `MANIFEST.json` per problem:

```json
"difficulty": { "prereq": 1, "insight": 2, "impl": 1, "pressure": 0, "traps": 1,
                "total": 5, "group": "medium", "impl_loc": 47 }
```

so the bucket is checkable arithmetic. Axis C is measured from the file; A, B, D, E are my
judgement, but they are *recorded* judgement — you can disagree with a 2 on insight, which you
could never do with a bare "medium".

**Honest limitation:** A, B, D and E are still model judgement and could shift by ±1 between
sessions on a borderline problem. That moves a total by at most ±2, which only changes the
bucket for a problem sitting on 4/5 or 9/10. I do not claim more determinism than that. What
this rubric guarantees is that the *scheme* is fixed, the vocabulary is fixed, the ordering is
total, and every call is inspectable.

---

## 4. Tags — closed vocabulary

**Rules:**

- Draw only from the list below. It is a **whitelist**, not a suggestion.
- 1–4 tags per problem. Primary (dominant technique) first, then decreasing relevance.
- Lowercase, spaces as written. Never invent a tag; if nothing fits, use the closest and add a
  line in `Notes worth flagging` proposing the addition. Vocabulary changes are a spec version
  bump, not a per-set decision.

**Vocabulary (frozen at v1.0):**

```
implementation      math                brute force         greedy
observation         arrays              strings             data structures
bitmasks            hashing             sortings            binary search
ternary search      two pointers        sliding window      prefix sums
dp                  divide and conquer  meet-in-the-middle  constructive algorithms
simulation          combinatorics       number theory       probabilities
games               graphs              trees               dfs and similar
shortest paths      dsu                 graph matchings     flows
interactive         geometry            matrices            string suffix structures
expression parsing  fft                 2-sat               chinese remainder theorem
schedules           backtracking
```

**Notion mapping.** Notion `Topic Tags` uses lowercase-dashed spelling. The mapping is
mechanical: replace each space with a dash. `dfs and similar` → `dfs-and-similar`,
`brute force` → `brute-force`. Nothing else changes; order is preserved (primary first).

This retires the ad-hoc dashed tags that had no Codeforces equivalent (`frequency-array`,
`custom-comparators`). Where such a tag carried real teaching information, that information
belongs in `Notes worth flagging`, not in the tag field an automated step reads.

---

## 5. Time and memory limits

- Default `TL = 1 s`, `ML = 256 MB`.
- A limit deviates from default only when the reference solution's **measured** worst case
  warrants it. Measurement, not estimate.
- Any deviation requires `limits_rationale` in `MANIFEST.json`, e.g.
  `"TL 2s: reference worst case 0.81s on n=2·10^5 adversarial, 2.5× margin"`.
- Without a rationale field, a non-default limit is a PREFLIGHT failure (PF-14). This makes an
  intentional bump distinguishable from a typo.

---

## 6. Generation procedure (deterministic)

Run only after every archive in the set is final. Inputs: the built package folders and the
per-problem build records. **No input comes from earlier chat summaries.**

1. **Collect.** For each built package: slug, title(s), language set (read the statement),
   subtask structure with points, s0/s1..sN counts from `testset/`, checker kind and name from
   `checker.cpp`, `solution.cpp` LOC, measured worst-case runtime, chosen TL/ML.
2. **Score difficulty.** Apply §3.1 per problem; compute total and group.
3. **Order.** Apply the §3.3 four-key sort → assign `idx` 1..N.
4. **Tag.** Apply §4 per problem, in `idx` order.
5. **Detect prefixes.** For each slug, take the longest prefix shared with at least one other
   slug in the set, cut at a dash boundary, minimum two segments. A slug sharing no ≥2-segment
   prefix with any other forms its own single-member group. Order the prefix sections by the
   `idx` of their lowest-`idx` member.
6. **Emit** §2's sections in order, filling empty ones with `[none]`.
7. **Compute totals.** `TOTAL problems` = row count. `TOTAL tests` = sum of the General `tests`
   N values.
8. **Self-check** against `PREFLIGHT.md` PF-05 … PF-09 before writing `MANIFEST.json`.

Given the same set of built packages, steps 1, 3, 5, 6, 7 are fully mechanical; step 2 carries
the ±1-axis caveat of §3.4 and step 4 is constrained to a closed list. That is as deterministic
as this file gets without you handing me a difficulty oracle.

---

## 7. Worked example — ILLUSTRATIVE ONLY

Not from a shipped set. Three invented problems, shown so the shape is unambiguous.

```markdown
# Characteristics — edu-arrays-20260725

_Course 1, Group A2, Week 5 — one-dimensional arrays: traversal, running aggregates,
adjacent-pair scanning. Built for Electicode._

_Ordering within every list is easiest → hardest, matching the General table row order._

---

## General

| idx | slug | title | languages | group | tests | subtasks | checker | TL | ML |
|-----|------|-------|-----------|-------|-------|----------|---------|-----|------|
| 1 | edu-arrays-running-max | Running Maximum | EN, RU | easy | 41 (2+39) | none | ncmp (native) | 1 s | 256 MB |
| 2 | edu-arrays-largest-gap | Largest Gap | EN, RU | easy | 47 (2+45) | none | ncmp (native) | 1 s | 256 MB |
| 3 | edu-sorting-podium-order | Podium Order | EN, RU | medium | 58 (3+55) | none | custom | 1 s | 256 MB |

**TOTAL problems:** 3
**TOTAL tests:** 146

---

## All slugs

- edu-arrays-running-max
- edu-arrays-largest-gap
- edu-sorting-podium-order

## All titles

- Running Maximum
- Largest Gap
- Podium Order

---

## Easy

- edu-arrays-running-max
- edu-arrays-largest-gap

## Medium

- edu-sorting-podium-order

## Hard

[none]

---

## edu-arrays slugs

- edu-arrays-running-max
- edu-arrays-largest-gap

## edu-arrays titles

- Running Maximum
- Largest Gap

## edu-sorting slugs

- edu-sorting-podium-order

## edu-sorting titles

- Podium Order

---

## Suggested tags

1. implementation, arrays
2. implementation, arrays, observation
3. sortings, greedy, arrays

---

## Checkers used

1. ncmp (native)
2. ncmp (native)
3. custom

---

## Notes worth flagging

- **edu-arrays-largest-gap:** values reach 10^9 and the gap is a difference of two of them —
  a student initialising the answer to 0 rather than a sentinel passes the samples and fails
  on the all-negative tests.
- **edu-sorting-podium-order:** several orderings are valid, hence the custom checker; it
  validates the participant ordering directly rather than comparing against the jury answer.
```
