# Seam verdict — resolved

*Supersedes `phase1-findings.md` §1, which was written from chat summaries. This is read
directly from both repos: `polygon-middleman@ec2d370` and `platform-scraper@b8ad778`.*

Sources: `polygon-middleman/docs/maestro/package-tree.txt` (real extracted package,
problem #563392, 276 entries) and `platform-scraper/docs/maestro/expected-input-tree.txt`
(layout `problem_uploader.py upload --folder` accepts today).

---

## 1. The comparison

`--folder` is the **parent**; every immediate subdirectory is a candidate problem. The only
predicate is `p.is_dir()` — one level deep, no recursion, no manifest, no marker file, no name
pattern (`problem_uploader.py:73-77`).

| ElectiCode expects, per problem folder | Polygon standard package produces | Verdict |
|---|---|---|
| `<parent>/<slug>/` wrapper | one flat package per zip, no wrapper, no slug in any path | **must create** |
| `check.cpp` | `check.cpp` at root | ✅ exact |
| `tests/01`, `02`, … | `tests/01` … `195`, **inputs only — zero `.a` answer files** | ✅ exact |
| `solutions/main.cpp` | `solutions/solution.cpp` | ⚠️ rename |
| `statement.tex` at folder root | `statements/{english,russian}/problem.tex` | ⚠️ copy + rename |
| `statements/images/*.png` | `statements/` exists but holds `.html/`, `.pdf/`, `english/`, `russian/` | ⚠️ **name collision, different shape** |
| — | `check.exe`, `files/checker.exe`, `files/towin.exe`, `solutions/solution.exe` | ❌ prune |
| — | `problem.xml`, `scripts/` (12), `files/`, `doall.{bat,sh}`, `wipe.{bat,sh}`, `statement-sections/` | ❌ cruft |

**Verdict: closer than feared, but a genuine transformation, not a rename.**

The two hardest things match exactly — test naming (`01`, `02`, zero-padded, no answer files)
and the checker at root. Those were the parts most likely to require rewriting data. What
remains is file placement and pruning, which is mechanical.

Three things make it more than a `mv`:

1. **`statements/` is a name collision.** Both sides use the directory, for different things.
   ElectiCode looks for `statements/images/`; Polygon fills it with `.html/`, `.pdf/`, and
   per-language `problem.tex`. The importer will find no images and may be confused by the
   rest. This needs a decision, not a rename.
2. **The uploader does not filter.** On `--apply` the *entire* parent tree is handed to the
   browser's directory input recursively; the tool "does not filter, rename, or validate the
   contents of any problem folder" (`problem_uploader.py:238`). So every `.exe`, every `.bat`,
   and all of `scripts/` is uploaded verbatim unless Maestro prunes first. Four Windows
   binaries per problem, times a 25-problem set, is 100 executables pushed into ElectiCode.
3. **Statement language selection becomes Maestro's job.** Polygon emits `english/` and
   `russian/` side by side; ElectiCode wants one `statement.tex`. Which one is a policy
   decision — and it must agree with `MANIFEST.json`'s `languages`.

---

## 2. The finding that matters more than the seam

**The folder name does not determine the slug.**

`problem_uploader.py` does not derive or transform a slug. The slug it reports is **scraped
from the platform's rendered upload modal** (`:146-147`) — i.e. ElectiCode derives it
server-side during import, presumably from `problem.xml` or the statement.

This invalidates the mitigation assumed in `phase1-findings.md` §1 and §2, which was "name the
extracted folder `<slug>` and identity is preserved." It isn't. Maestro cannot control the
resulting slug by controlling the folder name, and the Scraper's own §6 says so plainly:

> **Is the input-folder slug preserved verbatim on ElectiCode?** UNVERIFIED from this repo —
> the transform (if any) happens server-side during import. The toolkit assumes the slug the
> authoring side used is the one it can address later, but nothing here guarantees or checks it.

So the author's slug, the Polygon problem name, and the ElectiCode slug are three identifiers
that are *assumed* equal and never verified. Every stage-7 write depends on that assumption.

### Consequence: a new mandatory stage 6.5

Between upload (6) and post-upload processing (7), Maestro must:

1. scrape the catalog with `problem_scraper.py problems` — it emits `s3_id`, `name`,
   `s3_name`, `edit_url` per problem, which is the identity-map source
2. reconcile the scraped slugs against the expected slugs from `MANIFEST.json`
3. **halt the run on any mismatch** — do not proceed to stage 7 on assumption

Without 6.5, a server-side slug transform silently redirects every subsequent assign, division
grant, and list insertion. Combined with the `.first`-without-verification hazard and the
presence-only audit, nothing downstream would notice.

Note `problem_scraper.py` was not in the original tool map (plan §10) — it is a tenth tool, and
it is the one that makes the identity map possible.

---

## 3. Good news worth recording

- `list add` matches on the modal row's exact `ID: <slug>` text node — a **strong** identity
  match, not a substring search (`_select_in_modal:296-311`). The list-population step is the
  one stage-7 operation that is already safe.
- `division set` and `problem_editor assign` correctly record not-found as an error and fold it
  into **exit 2** rather than silently skipping. The hazard is a *wrong* match, not a *missing*
  one.
- Polygon's test naming matching ElectiCode's expectation exactly means no test-file rewriting
  — the largest data volume in the package passes through untouched.
- No `.a` answer files in `tests/`, so the developer's "inputs only" contract survives the
  Polygon round trip intact.

---

## 4. Revised shaping stage spec

Per problem, between stages 5 and 6:

```
extract {problemId}-r{rev}-standard.zip
  → <parent>/<slug>/                      # slug from Maestro's identity map
      check.cpp                           # copy as-is
      tests/                              # copy as-is
      solutions/main.cpp                  # ← solutions/solution.cpp
      statement.tex                       # ← statements/{lang}/problem.tex, lang per MANIFEST
      statements/images/                  # ← TBD: source unresolved, see §1.1
  prune: *.exe, *.bat, *.sh, problem.xml, scripts/, files/,
         statement-sections/, statements/{.html,.pdf,english,russian}/
```

Open before this can be built:

- **Where do statement images come from?** Polygon puts them somewhere not visible in this
  tree (no images in package #563392). Need a package from a problem that has figures.
- **Is `main.cpp` required, or does ElectiCode accept any file under `solutions/`?** The
  expected-tree doc marks it "(server-side)" — i.e. the uploader doesn't check, ElectiCode
  does. Needs an empirical answer.
- **Does ElectiCode tolerate the extra `statements/` subdirectories** if left in place, or must
  they be pruned? Same — server-side, needs a test upload.

All three are answerable with one preview-mode upload of a single shaped folder.
