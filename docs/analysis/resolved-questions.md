# Resolved: the two gating questions, plus what answering them exposed

*Resolves `phase1-findings.md` §5 C-2 and C-3. Both came back favorably — neither is the
pipeline-killer it could have been. The value is in what turned up alongside them.*

---

## C-2 — Validator / second solution vs. Polygon verify: **NOT a blocker**

`buildPackage(full=false, verify=true)` **passes** for a problem with exactly one `MA` solution
and no validator.

Evidence, from the Middleman running it against the live account:

- Problem #563392 (`edu-tree-applications-equal-population-regions`) is exactly this config —
  one solution tagged `MA`, no validator. Its verified package is **`READY`**, with
  `comment: "Package created in 61593 ms with verification"`. So the config verified in ~62s.
- A scan of **220 imported problems** found **zero** without a `READY` verified package, and
  **zero** `FAILED`. Across the entire existing corpus this configuration verifies reliably.

The developer correctly declined to force a from-scratch build by bumping a revision on a real
problem — the evidence was already conclusive and mutating production to prove a settled point
is the wrong trade.

**Consequence:** the problem-developer does **not** need to start producing `validator.cpp` or a
second solution. Their `OUTPUT_CONTRACT.md` §B.3 (`validator.cpp` — *not produced*) stands
unchanged. This was the finding with the largest potential blast radius and it lands clean.

### F-1 — But the attempt exposed a retry trap

The fresh build trigger was **refused**, and the refusal arrives as:

```json
{"status": "FAILED",
 "comment": "problemId: There is already non-failed package for this revision with verification."}
```

**That is a `FAILED` status that is not a failure.** Polygon guards against rebuilding a revision
that already has a verified package.

Maestro's stage 4 polls for `READY`/`FAILED` and halts on `FAILED`. So on any **retry, resume, or
idempotent re-run** of a problem already built — precisely the paths a resumable job engine
exercises most — Maestro reads `FAILED`, halts the batch, and reports a build failure for a
problem whose package is sitting there `READY` and usable.

Today this is distinguishable **only by string-matching the free-text `comment`**, which is
exactly the gap the Middleman flagged in §9 (no per-error codes). So:

- **Error taxonomy entry:** `FAILED` + comment matching *"already non-failed package for this
  revision"* → **not an error**. Treat as success; fetch the existing `READY` package.
- **Retry semantics differ by stage.** Re-running *import* is idempotent (fills/overwrites the
  same problem, per `pipeline.ts:145-146`). Re-running *build* is **refused**. A single
  "retry the stage" policy is wrong; each stage needs its own.
- This is now the concrete motivating case for the Middleman's Phase 2 machine-distinguishable
  errors. Not a hypothetical — it is the failure a resumable orchestrator hits first.

---

## C-3 — Sample marking: **confirmed, works**

`input_s0_*.txt` tests **are** marked `useInStatements` in Polygon. Statements render their
examples. The feared silent-quality failure does not occur.

Verified twice over:

- **Code**, in both implementations: `testUseInStatements: t.group === '0'`
  (`frontend/src/wizard/zipImport/helpers.ts:23`) and `"testUseInStatements": t["group"] == "0"`
  (`backend/import_pipeline.py:94`). Group is parsed from `_s(\d+)` in the filename
  (`testParser.ts:2`), so `input_s0_*.txt` → group `"0"` → marked.
- **Empirically in Polygon**: on #563392, both group-0 tests show `useInStatements: true`.

**The caveat is already covered by the other contract.** Only tests named `input_s0_*.txt` get
marked; an archive shipping samples under a different group, or shipping no `s0` at all, would
produce an example-less statement. That is an authoring concern — and the problem-developer's
`OUTPUT_CONTRACT.md` §B.3 makes `testset/` mandatory with **≥1 `s0` file and ≥1 `s1` file**,
with `PREFLIGHT.md` PF-13 asserting it and PF-16 enforcing the `input_s\d+_idx\d+\.txt` naming.

Two independently written contracts agreeing on the same invariant, from opposite ends of the
pipeline. C-3 is fully closed.

---

## The tag/difficulty question — partly my error, and the answer matters

### What I got wrong

I asked the Scraper to check the difficulty and tag vocabularies against
`CHARACTERISTICS_SPEC.md` §4. **That document lives in this repo** (`docs/contracts/`), written
by the problem-developer — not in the Scraper's repo. And the `[paste the vocabulary…]`
placeholder was never filled in, so they had nothing to check even in principle. They were right
to reject the premise rather than guess at what I meant. The vocabulary is reproduced in §"Reply
to send" below so the question can actually be answered.

### F-2 — Difficulty fails loud; tags fail silent

This asymmetry is the real finding, and it is verified in code:

| Field | Mechanism | On a bad value |
|---|---|---|
| **Difficulty** | `page.locator("#difficulty").select_option(...)` — a real `<select>` (`problem_editor.py:337`) | Playwright **throws at apply time** if the value isn't a live `<option>`. Surfaces as an error. **Detectable.** |
| **Category / tags** | `page.locator("#category").fill(value)` — a plain text `<input>` (`problem_editor.py:347`) | Accepted verbatim. **No enum, no allow-list, no length or charset check anywhere in the toolkit.** **Silent.** |

So a mis-cased difficulty is caught; an out-of-vocabulary tag is not. It lands as-is, creates a
junk category in ElectiCode's tag store, and passes the presence-only audit (`report.py:60-66`).

**The closed 42-tag vocabulary is therefore the only control that exists, and it has zero
enforcement anywhere below the authoring layer.** If the author drifts — which
`OUTPUT_CONTRACT.md` Appendix A item 8 documents as having already happened, twice, with two
incompatible spellings — nothing downstream notices.

### The developer predicted this exactly

`MANIFEST_SPEC.md` §3 cross-check **M-9**, written before any of this was verified:

> Every `tags[]` entry is in the closed vocabulary. Unknown tag → halt (**an unknown tag would
> otherwise be created silently in the tag store**).

That parenthetical is now confirmed by code. The design anticipated the failure mode correctly.

### Where enforcement goes

A silent failure mode with a single point of control warrants two independent checks:

1. **Maestro, pre-ingest** — M-9 as specified, against `MANIFEST.json`, before anything is
   uploaded. Maestro holds the manifest and must do this regardless.
2. **`batch.py validate --char`** (Scraper Phase 2 item 4) — at the consumer boundary, catching
   a hand-edited `characteristics.md` that never passed through a manifest.

With the honest caveat the Scraper developer raised: this enforces an **authoring convention**,
not a platform constraint. Unless the live DOM shows ElectiCode restricting the field, the
vocabulary is a discipline we impose, not one the platform imposes. That is still worth
enforcing — the discipline is the whole point — but it should be labelled as what it is.

### One accidental alignment, confirmed working

The developer's spec emits lowercase `easy|medium|hard`; the Scraper's parser accepts lowercase
and normalizes to capitalized (`batch.py:143`); the toolkit's `_VALID_DIFFICULTY` is
`("", "Easy", "Medium", "Hard")` (`problem_editor.py:414`). The chain lines up end to end
without anyone having coordinated it.

Only the last link — whether the **live** dropdown's options are exactly `Easy`/`Medium`/`Hard`,
rather than integer values with text labels, or different casing — remains unverified.

---

## The one command that settles the remainder

Non-mutating, needs a live admin session, so it has to run on the operator's machine:

```
python problem_editor.py dump --only <an-existing-slug> --wait --out edit_modal
```

Opens headful, waits for Enter, and writes `edit_modal.html` / `.txt` / `.png`
(`problem_editor.py:1012-1019`). That capture settles both open items at once: the actual
`#difficulty` `<option>` list, and confirmation that `#category` is an unconstrained input.

Worth doing before the vocabulary is treated as final in either direction.
