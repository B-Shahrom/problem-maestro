# The second live run

The first live run got a set from `watch_dir` to tagged, fixmdx'd, difficulty-set problems on
ElectiCode. Two things it did not do: grant division access, and reach a passing stage 8.

Everything built since then was built to close those and the holes they exposed. **None of it
has run against a live service.** That is eight commits of gate — the limits round trip, the
two-source scrape, the division verdict, the contest-list read-back, per-batch settings — all
of it proven only against fakes and against the Scraper's own argument parsers.

This procedure exists to convert that into evidence, and it is written around one hazard the
first run did not have: **every new check is a no-op when its setting is empty.** A batch that
chooses no divisions, no list and default limits will run clean through all eight stages and
prove nothing at all. Read §1 before dropping anything.

---

## 0 · Preconditions

Three, and the first two are new since run 1.

```
git -C <platform-scraper> pull          # T7's `limits` stage, and list_editor.py
python -m maestro check                 # refuses to start against an older checkout
python -m maestro forget 1 --yes        # run 1 is dead weight; frees its set name
```

`maestro check` now requires `batch.py`'s `limits` stage, `problem_scraper --from-catalog`,
and `list_editor.py show`. It names whichever is missing rather than letting the run discover
it minutes in. Run 1 stopped on exactly this shape — `contest_scraper.py session` did not
exist yet — which is why the check refuses rather than warns.

Then the rest of `first-live-run.md` §0: Middleman up, its contract suite green, an ElectiCode
session that exits 0, and Maestro's own suite with `MAESTRO_SCRAPER_REPO` and
`MAESTRO_MIDDLEMAN_REPO` set so the four round-trip modules actually run rather than skip.

## 1 · Choose the set so the new checks can bite

Two problems again, one existing and one fresh, for the same reason as run 1. Beyond that:

| the set must have | or else |
|---|---|
| a **non-default** time limit — not 1s / 256MB — with a `limits_rationale` and a `measured_worst_s` | L-1 compares the authored limit against the platform's, and passes. But a limit that equals what the import would have produced anyway passes whether or not Maestro's send arrived. The check is only evidence when the two differ. |
| **divisions** chosen on the run | `divisions_landed` returns nothing, the paged scrape never happens, and D-1/D-2, the two-source split and `report audit --divisions` are all untested |
| a **contest list URL** chosen on the run | the list stage is absent from the chore plan, so LI-0…LI-3 have no events to read and LM-1/LM-2 have nothing to read back |
| **translation targets** chosen on the run | the translate stage is absent |

Choose all four **on the run, not in the config** — that is the mechanism under test:

```
python -m maestro settings <run>                        # what this batch chose
python -m maestro settings <run> --divisions "Electi" --targets ru,tg,uz \
                                 --list-url "https://…/contest/…/manage"
```

or the same four controls in the dashboard. `maestro settings <run>` with no flags prints the
effective value of each and whether it was chosen or inherited; check it before approving,
because "inherited an empty config default" and "deliberately chose none" read identically in
the run's behaviour and only this command tells them apart.

The M-15 / M-16 pair fires at **ingest**, before any of this. A non-default limit without a
rationale is an error, and a `measured_worst_s` at or above the time limit is an error — the
intended solution TLEs on the author's own machine, and the judge is slower. If the set is
rejected there, that is the gate working on a real defect; fix the manifest and re-drop.

## 2 · Through Polygon — one new thing to watch

Nothing about stages 3–5 changed except one check, at import: Maestro compares not only *what*
limits the import applied but *where they came from*. The Middleman reports `limitsSource` as
`form`, `manifest` or its own default. Maestro sends both form fields whenever the manifest
has them, so anything but `form` means the send did not arrive and the right number was
reached by luck.

If you see that message, the values on Polygon are still correct — this run would go on to
pass L-1 at stage 8. It is worth stopping for anyway: the luck runs out on the first set where
the form field and the packaged manifest disagree.

## 3 · Upload, reconcile — as run 1

Unchanged. Approve writes, read the preview, confirm stage 6.5 sees every slug in the catalog.

## 4 · Chores — the list step, and its unreliable self-report

The chore chain now runs up to six steps. The new one to watch is `list add`, because its own
reporting is wrong in **both** directions and Maestro no longer believes it:

- it under-counts successes — run 1's log said "Added 13/20" beside "the list now has 15"
  because the confirmation reads before the page settles
- it over-reports failures — it plans by comparing row *titles* against caller *slugs*, so
  problems already in the list are planned again and then come back "not found in the modal",
  which is what the Add modal correctly does for a problem already added

LI-0…LI-3 record what the step claimed. They are deliberately not the verdict — LI-0 is a
warning that the events could not be read at all, which is a different fact from a clean add
and must not look like one. The verdict comes at stage 8.

## 5 · Stage 8 — the stage no live run has ever reached

Until `test_scraper_roundtrip.py` was written, Maestro sent `--state` to `report.py`, which
does not accept it; argparse matched the path against the `command` positional and stage 8
could not have succeeded under any circumstances. Every server-side test agreed with the bug.
So treat this stage as entirely unproven, including the parts that look old.

It now does five things, in this order, and stops at the first that fails:

1. **The catalog scrape** — one page load, `--from-catalog`. This is the only source carrying
   `time_limit_ms` / `memory_limit_kb`.
2. **The paged scrape** — ~41 loads, and *only* when the batch granted divisions, because the
   catalog structurally has no `division_access`. The two sources are complementary, not
   ranked; neither alone answers both questions. If you granted no divisions you should see
   exactly one scrape, and that is correct rather than a skipped step.
3. **L-1 / L-2** — the limits round trip, read from the catalog. L-2 is the fail-loud case:
   the scrape carried no limit columns, so nothing was verified.
4. **D-1 / D-2** — the division verdict, read from the paged rows.
5. **LM-1 / LM-2** — the contest list read back with `list_editor show` and compared against
   the authored titles. This is the only check of contest membership anywhere; without it a
   problem can be correctly tagged, correctly limited, in the right divisions, and invisible
   to every student the contest was made for.

Then `report audit --char` itself — and its exit code is not taken as the answer. It exits
**0** when it *skips* the division check, which it must do on a catalog-sourced scrape, so
Maestro reads its `checks_run` / `skipped` and parks the run as incomplete if a check it asked
for did not run.

### What each stop means

| stop | Maestro is saying | first thing to check |
|---|---|---|
| `L-1` | the platform's limit differs from the authored one | whether Polygon has the right value. If it does, the loss is downstream of import — package build, download or upload |
| `L-2` | the limits could not be verified at all | that the scrape was `--from-catalog` and the Scraper carries the T2 correction |
| `D-1` / `D-2` | a problem is missing division access | `division set` is idempotent; re-running the chore converges. Confirm on the platform first |
| `LM-1` | a problem is not in the contest list | the list page itself. `list add`'s own claim is untrustworthy in both directions, so this is the fact and its log line is not |
| `LM-2` | the list could not be read back | the URL. Membership is unverified, not wrong — the run continues |
| `audit incomplete` | the audit passed but skipped a check | which check, in the log. This is the `report audit` self-skip working as designed |

A `failed` run here is not a Maestro bug by default. Each of these fails the run rather than
warning because a run that reaches `done` is a run nobody opens again, and every one of these
defects is invisible from the platform's own UI.

## 6 · Keep the artefacts

This is the run that decides whether Maestro's model of the platform is right, so the evidence
matters more than the verdict. Under `runs/<id>/electicode/`:

```
catalog-after.json          the one-page catalog — limits live here
catalog-paged.json          the paged table — division_access lives here
list-rows.json              what the contest list actually contains
audit.json                  report.py's own verdict
characteristics-audited.md  the subset this run is accountable for
```

If a check misfires, those four files are what tells us whether the platform changed shape or
Maestro read it wrong, and they are not reproducible after the fact. Keep them even on a clean
run — a passing L-1 against a `catalog-after.json` with no limit columns would mean the check
never ran, and only the file shows that.

## What this run still will not prove

- **The limits repair path.** Nothing Maestro drives can set TL/ML after import, so L-1 is
  terminal today — the fix is a corrected re-import. The platform has a way; no Scraper
  command exposes it yet.
- **The title-based division resolver** against the 23 colliding titles, unless your set
  happens to collide. Slugs are unique, so stage 6.5's join is sound; the resolver is the
  thing that has to fail closed, and it is the Scraper's T1.
- **Quarantine**, unless a problem genuinely fails Polygon verification.
- **Concurrency** between runs, and the fairness rule when several are queued.
- **The author lane.** `maestro brief` and the correction requests work through a human relay;
  the delivery half is unbuilt. See `docs/analysis/author-lane.md`.
