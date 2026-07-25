# The first live run

The build is finished and every stage is tested against fakes. Nothing has run against live
Polygon and live ElectiCode end to end. This is the procedure for that, written to fail
cheaply: each step either proves something or stops before it can do damage.

**Use a set you can afford to get wrong.** Two problems is enough — one that already exists on
ElectiCode and one that doesn't, because that combination is the only way to exercise the
exists-split that decides whether stage 7 overwrites or appends tags.

---

## 0 · Before anything

Three services, three checks. All are read-only.

```
# Middleman is up and its contract suite passes
curl -s http://127.0.0.1:8000/api/ping || echo "start the middleman"
python backend/test_import_contract.py           # in polygon-middleman

# ElectiCode session is valid — exit 0
python contest_scraper.py session --check        # in platform-scraper

# Maestro's own suite, including the two that talk to the real repos
MAESTRO_SCRAPER_REPO=… MAESTRO_MIDDLEMAN_REPO=… python -m pytest -q
```

If the session check exits `3`, log in first (`contest_scraper.py login --url …`). Maestro
would park the run for exactly this, but finding out now costs nothing.

## 1 · Configure

```
python -m maestro init
```

Then edit `config.json`. The three that matter:

| key | value |
|---|---|
| `scraper_repo` | path to the platform-scraper checkout |
| `scraper_state` | path to `session_state.json` |
| `watch_dir` | the folder you will drop set folders into |

**Leave `apply` as `false`.** That is the whole point of the first run: every stage that would
write parks and waits for you.

## 2 · Ingest only — prove the gate works before trusting it

Drop the set folder into `watch_dir`, then:

```
python -m maestro run --max-ticks 1
python -m maestro status
```

Expect one run at stage `polygon`. If it did not appear, the manifest failed validation — the
reason is in the log, and it is a finding about the set, not about Maestro.

This step also runs the `/api/parse` pre-flight, so a manifest that disagrees with what will
actually import is caught here rather than three stages later.

## 3 · The Polygon half — unattended, but watch it once

```
python -m maestro run
```

Open the dashboard (`http://127.0.0.1:8787`) and watch the log. Per problem you should see
import → build → package extracted. Roughly a minute each, slower if Polygon is having a bad
day; the Middleman rides over transient blips by design.

**What to check when it reaches `upload`:** the run has parked itself as `blocked /
awaiting_approval`, and *nothing has been written to ElectiCode*. That is the gate working.
Confirm the extracted folders exist under `runs/<id>/upload/` — one per problem, named for the
slug.

## 4 · Approve the upload, and read the preview first

Click **approve writes** on the run. On the next tick Maestro runs the upload preview, checks
each detected row against what it expects, then submits.

The preview is where a collision would be caught: if an EXISTS row names a title Maestro
doesn't expect for that slug, the run fails *before* submitting. If that happens, it is
telling you a real thing — someone else's problem occupies that slug.

After the upload, stage 6.5 reads the catalog back and refuses to continue unless every slug
landed.

## 5 · Chores, and the one thing to look at

The chore stage splits the batch by `exists` and runs `batch.py` once per group. Check the two
rendered files under `runs/<id>/electicode/`:

- `characteristics-fresh.md` — the problems that did not exist, run with `--tags-mode reset`
- `characteristics-existing.md` — the ones that did, run with `--tags-mode add`

**Verify by hand, once:** open a problem from the `existing` group on ElectiCode and confirm
its pre-existing curricular tags (`academy exam` or similar) are still there alongside the
authored ones. That is the single behaviour the whole exists-split exists to protect, and this
run is the only cheap chance to confirm it end to end.

## 6 · The audit gate

Stage 8 scrapes the catalog and compares it against the characteristics. Two outcomes:

- **clean** → the run goes to `done`. That is the whole pipeline proven.
- **gaps** → the run parks as `blocked`, and the log names the slug and both values.

A gap here is not a Maestro failure — it is the gate doing its job. Read `audit.json` under
`runs/<id>/electicode/` before changing anything.

Maestro also checks the limits separately, twice: at import against what the Middleman applied,
and here against what the platform shows. Neither is visible anywhere else, because ElectiCode
renders TL/ML read-only.

---

## If something stops

Every stop is deliberate and every one is legible in the dashboard.

| state | what it means | what to do |
|---|---|---|
| `blocked / session_expired` | the ElectiCode session died mid-run | log in on the host, then **resume** |
| `blocked / awaiting_approval` | a write stage, or the audit found gaps | read the log, then **approve** or fix the set |
| `failed` | something the lane could not fold into a status | the error names it; **resume** after fixing |

**Resume never retries anything by itself.** It makes the run eligible for the next tick, and
the idempotency rules still apply — a chore chain resumes at the stage that failed, and a stage
that cannot be safely replayed still refuses.

## What this run does not prove

Worth being explicit, so nobody reads a green first run as more than it is:

- **One batch, two problems.** Concurrency between runs, and the fairness rule when several
  are queued, are tested but not exercised live.
- **The quarantine path** only runs if a problem genuinely fails Polygon verification. If
  everything passes, that code is still unproven in production.
- **The 28 colliding titles** on ElectiCode (`A + B` appears three times) are a resolver
  hazard. Unless your set happens to collide, this run says nothing about it.
