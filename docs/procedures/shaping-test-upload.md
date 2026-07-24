# Procedure — settling the shaping stage with preview uploads

*The last empirical unknown. Runs on the operator's machine (needs a live ElectiCode admin
session). Entirely non-mutating: `problem_uploader.py` without `--apply` lists what it sees,
reports what the platform detected, then **cancels** (`:214-217, 251-260`).*

> **Never pass `--apply` during this procedure.** On `--apply` the tool submits **regardless of
> EXISTS** (`:158-161`) — it does not skip, overwrite, or error on a match. A stray `--apply`
> here writes real problems into the catalogue.

---

## What this answers

`seam-verdict.md` §4 left three questions unresolved, all server-side and therefore unanswerable
by reading code. A fourth emerged while writing this procedure and is more fundamental than the
other three:

| # | Question |
|---|---|
| Q0 | **Does Polygon's raw, unshaped package upload as-is?** |
| Q1 | With `problem.xml` pruned, can ElectiCode still identify the problem — and what slug does it assign? |
| Q2 | Is `solutions/main.cpp` required, or is any file under `solutions/` accepted? |
| Q3 | Are Polygon's extra `statements/` subdirectories (`.html/`, `.pdf/`, `english/`, `russian/`) tolerated? |

**Q0 goes first, and may make the rest moot.** If ElectiCode's importer was built to eat Polygon
packages — which the "Upload Polygon Folder" button in the admin UI suggests — then the raw
extracted package already works, the shaping stage collapses to *extract, rename the folder,
prune binaries*, and Q1–Q3 never need asking.

That would be the single largest simplification available to this project, and it costs one
preview run to find out.

---

## Why one run per variant, not all variants in one parent

The tempting design is three sibling folders under one parent, resolved in a single run. **It
does not work here.** Every variant is derived from the *same* Polygon package, so all three
carry the same problem identity. `problem_uploader` does not derive or transform a slug — "the
authoring side must ensure the Polygon package's own identity yields the intended slug; this
uploader has no say" (§5). Three folders claiming one identity gives an uninterpretable result:
a detection count of 1 tells you nothing about which shape was recognised.

Using three *different* source packages instead removes the collision but confounds the variable —
a difference in detection could be the package rather than the shape.

So: **one variant per run, same source package throughout.** Each run is a cheap, safe preview.

---

## Setup

Get one real package from the Middleman (read-only):

```
GET /api/import/package/563392
```

Extract it to `pkg/`. Its layout is recorded in
`polygon-middleman/docs/maestro/package-tree.txt` — flat root, one problem, 276 entries:
`problem.xml`, `check.cpp`, `files/`, `scripts/`, `solutions/`, `statement-sections/`,
`statements/`, `tests/` (195 test files, inputs only), plus `doall.*` / `wipe.*` and four
Windows `.exe` binaries.

Each run uses a fresh parent directory containing exactly one problem folder.

---

## Run 0 — baseline (do this first)

The raw package, unshaped, in a folder named for the intended slug. Binaries pruned, because
uploading four Windows executables per problem is not something to normalise even in a test.

```
mkdir -p run0/edu-tree-applications-equal-population-regions
cp -r pkg/* run0/edu-tree-applications-equal-population-regions/
find run0 -name '*.exe' -delete

python problem_uploader.py upload --folder run0 --output run0.json
```

**Read from `run0.json`:** `candidates` (what the tool saw locally), `detected` (what the platform
recognised), `created`. Plus the stderr `EXISTS` count.

| Outcome | Meaning |
|---|---|
| Detected, slug matches the folder name | **Best case.** Shaping ≈ extract + rename + prune. Q1–Q3 moot. Skip to "If Run 0 succeeds". |
| Detected, slug differs from the folder name | Works, but identity comes from package contents. Record the slug it *did* assign — that is the answer to "where does the slug come from", and stage 6.5's reconcile becomes mandatory rather than precautionary. |
| Not detected | Polygon's native layout is not accepted. Proceed to Run 1. |

---

## Run 1 — minimal shaped

The layout `expected-input-tree.txt` documents, and nothing else.

```
S=edu-tree-applications-equal-population-regions
mkdir -p run1/$S/solutions
cp    pkg/check.cpp                      run1/$S/check.cpp
cp -r pkg/tests                          run1/$S/tests
cp    pkg/solutions/solution.cpp         run1/$S/solutions/main.cpp
cp    pkg/statements/english/problem.tex run1/$S/statement.tex

python problem_uploader.py upload --folder run1 --output run1.json
```

- **Detected** → the minimal shape works. Run 2 and 3 then narrow *how* minimal it can be.
- **Not detected** → identity was probably in `problem.xml`. Go to Run 1b.

### Run 1b — minimal + `problem.xml`

Only if Run 1 fails.

```
cp -r run1 run1b && cp pkg/problem.xml run1b/$S/problem.xml
python problem_uploader.py upload --folder run1b --output run1b.json
```

Detected here but not in Run 1 ⇒ **`problem.xml` carries the identity and must be retained** by
the shaping stage. That is a useful, specific answer.

---

## Run 2 — is `main.cpp` required? *(only if Run 1 or 1b succeeded)*

Identical to the successful variant, except the solution keeps its Polygon filename:

```
cp -r run1 run2 && mv run2/$S/solutions/main.cpp run2/$S/solutions/solution.cpp
python problem_uploader.py upload --folder run2 --output run2.json
```

Detected ⇒ any file under `solutions/` is accepted, and the rename comes out of the shaping
stage. Not detected ⇒ `main.cpp` is required; keep the rename.

## Run 3 — are the extra `statements/` subdirectories tolerated? *(same precondition)*

```
cp -r run1 run3 && cp -r pkg/statements run3/$S/statements
python problem_uploader.py upload --folder run3 --output run3.json
```

This is the `statements/` **name collision** from `seam-verdict.md` §1 — ElectiCode expects
`statements/images/`, Polygon fills the directory with `.html/`, `.pdf/`, `english/`, `russian/`.

Detected ⇒ tolerated, no pruning needed. Not detected ⇒ the shaping stage must strip
`statements/` down to just `images/`.

---

## Not covered: statement images

Package #563392 contains no figures, so no run here exercises `statements/images/`. Answering it
needs a package from a problem that **has** images — worth identifying one and repeating Run 1
against it. Until then the image path stays unknown, and the shaping stage should treat it as
unimplemented rather than assume it works.

---

## Recording the result

Each run's `--output` JSON plus its stderr goes in `docs/evidence/`. Then update:

- `seam-verdict.md` §4 — replace the speculative recipe with the verified one
- `phase1-findings.md` §1 — the seam becomes fully resolved
- If Run 0 succeeds: **cut the shaping stage from the build order** in `README.md` and say so
  loudly. It is currently scoped as a component with its own state and failure modes; collapsing
  it to a rename is worth a lot.

## If Run 0 succeeds

Stop after Run 0. Do not run 1–3 for their own sake — they exist to narrow a shape that would no
longer need narrowing. Record the outcome, delete the speculative recipe, and move on.
