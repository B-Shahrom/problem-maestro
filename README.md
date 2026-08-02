# Maestro

Master orchestrator for the problem → Polygon → ElectiCode pipeline.

Maestro runs the full problem lifecycle end to end across three actors. It owns no domain
logic — it sequences the other systems, gates on their results, holds durable run state,
and exposes one dashboard.

| Actor | Role | Integration |
|---|---|---|
| Problem-developer | Authors problems (statement, checker, solution, tests, editorial) + `characteristics.md` | Folder handoff (v1) → Anthropic API (v2) |
| Polygon Middleman | Everything on Polygon — import, build, verify, package | HTTP (`:8000`) |
| Platform Scraper | Everything on ElectiCode — upload + post-upload processing | subprocess (Playwright CLIs) |

## Status

Build phase. Both integration lanes are written and tested; the cross-actor analysis that
shaped them is in `docs/analysis/phase1-findings.md`.

Headline: **the hand-off works unshaped.** A preview upload confirmed ElectiCode accepts
Polygon's raw extracted package with the folder named for the slug — no file moves, no
renaming, no pruning beyond Windows binaries. The shaping stage that looked like a subsystem
is three lines. What remains is real: a slug-addressing hole that lets metadata land on the
wrong problem undetected, three CLI tools that cannot express failure, and a tag field that
overwrites rather than appends. Those reorder Phase 2 toward correctness before observability.

## Contents

- **`docs/plan/maestro-plan.md`** — the original plan, written from the Polygon Middleman side
- **`docs/analysis/maestro-understanding.md`** — analysis of that plan: the two-level state
  machine, the hand-off seam risk, identity mapping, concurrency constraints
- **`docs/analysis/phase1-findings.md`** — what the three Phase 1 reports say when read
  against each other: the identity hole and four cross-actor contradictions no single actor
  could see
- **`docs/analysis/seam-verdict.md`** — the Polygon → ElectiCode hand-off, diffed from both
  repos directly, plus the slug-derivation finding that adds a reconcile stage
- **`docs/analysis/resolved-questions.md`** — the two gating questions answered empirically
  (both clear), and the retry trap and fail-silent tag path that answering them exposed
- **`docs/analysis/electicode-fields.md`** — the admin edit modal read from a live DOM capture:
  difficulty closed and verified, categories unconstrained and carrying two taxonomies, and the
  tag-overwrite risk that reset-vs-add resolves
- **`docs/analysis/characteristics-diff.md`** — the author's spec against the real parser. They
  are compatible; the six silent-failure edges are what Maestro has to check itself
- **`docs/analysis/author-lane.md`** — the half of the pipeline before the watch directory:
  what "talking to the developer" decomposes into, why the authoring actor needs Managed
  Agents rather than the Claude API alone, and why the gate must stay deterministic under it
- **`docs/analysis/the-mind.md`** — where a model belongs *inside* the orchestrator, what
  it is allowed to do, and why "deal with problems on its own" turned out to be two
  different requests with very different answers
- **`docs/contracts/`** — the problem-developer's authoring contract (output contract,
  characteristics spec, manifest spec, preflight checklist, system prompt, tool spec)
- **`docs/prompts/`** — the briefs sent to each actor, plus the Phase 2 corrections
- **`docs/procedures/`** — operator-run procedures needing a live session

## Next steps

Design questions are settled. Everything below is build work.

1. ~~Run 0~~ — **done, passed.** ElectiCode accepts Polygon's raw package; the shaping stage
   collapses to extract + rename folder to slug + delete `*.exe`. See the banner at the top of
   `docs/analysis/seam-verdict.md`.
2. Phase 2 in both apps — **in progress**. Task channel is `docs/maestro/FROM_MAESTRO.md` /
   `TO_MAESTRO.md` in each dev's repo.
3. Build order: ~~job store~~ → ~~ingest + validators~~ → ~~Polygon lane~~ → ~~upload~~ →
   ~~stage 6.5 reconcile~~ → ~~post-upload chores~~ → ~~audit gate~~ → ~~scheduler~~ →
   ~~dashboard~~. **Done.**

The build is complete: a set folder dropped in the watch directory goes through validation,
Polygon import/build/download, ElectiCode upload, reconcile, chores and audit without
intervention, and parks for a human at every point where it should.

What remains on this half is not code — it's live evidence. The first supervised run
(`docs/procedures/first-live-run.md`) reached tagged, fixmdx'd, difficulty-set problems on
ElectiCode; it did not grant division access and never reached a passing stage 8. Everything
built since to close those — the limits round trip, the two-source scrape, the division
verdict, the contest-list read-back, per-batch settings — has run only against fakes and
against the Scraper's own argument parsers. `docs/procedures/second-live-run.md` is the
procedure for converting that into evidence, written around the hazard that every one of those
checks is a no-op when its setting is empty.

The half that is *not* built is everything before the watch directory: briefing the author,
receiving the delivery, and getting a rejected set corrected. Two of those three now exist and
work with the human relay — `maestro brief` writes the instruction, and a rejected folder gets
a correction request naming the contract clause behind each finding. What is left is the
delivery itself, which needs an actor that can run code. That case, and why the gate must stay
deterministic under any of it, is `docs/analysis/author-lane.md`.

## Running it

```
python -m maestro brief NAME --mix 2:2:1 --prefix edu-arrays   # the instruction for a new set
python -m maestro init                 # write a starter config.json
python -m maestro check                # validate paths AND that the Scraper checkout is current
python -m maestro inspect              # what Maestro makes of each folder in watch_dir
python -m maestro inspect --report     # …and write a correction request beside each rejection
python -m maestro run                  # scheduler + dashboard on :8787
python -m maestro run -v               # …echoing every line the Scraper tools write
python -m maestro status               # one-shot listing; non-zero if a run wants a human
python -m maestro settings 12          # what this batch chose for itself
python -m maestro settings 12 --divisions "Electi, Division A+" --targets ru,tg,uz
python -m maestro divisions 12 --set "Electi"   # the one touched every batch
python -m maestro diagnose 12          # ask the mind why run 12 stopped
python -m maestro diagnose 12 --dry-run  # …or just see what it would be sent
python -m maestro forget 12 --yes      # delete Maestro's record of run 12
```

`apply` is **off** by default, so a fresh install previews and parks each run at its first
write. Approve individual runs in the dashboard, or set `apply: true` once you trust it.
`config.json` is gitignored — it points at the session file and the Middleman.

## The mind

Optional, off by default, and an install without it is a complete install:
`pip install 'maestro[mind]'`, set `mind: true`, and point `mind_key_env` at the
environment variable holding an Anthropic key. The config names the *variable*,
never the value — same rule as every other secret here.

What it does is read the runs that have **stopped**. A parked run's cause is
usually spread across a log, a set of findings and an artefact, and reading all
of it at once is the thing a human does and no check can. The reading lands in
the run's own log marked `mind`, saying what it thinks happened, what to look at
first, and what evidence it wanted and did not have.

What it does **not** do is accept anything. The gate is unchanged and stays
deterministic — M-2 compares a sha256, C-4 compares two sets of slugs, and a
model asked those questions would be right most of the time, which on a checksum
is indistinguishable from not checking it. The rule the whole design rests on:
*a model may propose; only a check may accept.*

It can also act, within bounds you set:

| | |
|---|---|
| the action vocabulary | four verbs — `wait`, `resume`, `approve`, `escalate` — and every one is something the dashboard already offers a human |
| `mind_may` | which of them it may take unattended. Defaults to `[]`: propose only |
| confidence | nothing below `high` is ever acted on, however permissive `mind_may` is |
| `escalate` | never automated. It is the word for "a human is needed" |

`approve` is the one to think about. It is narrower than `apply: true` — which
approves every run unread and already exists — but it is not nothing, and
`maestro check` says so when it is enabled.

A run with no reading always says *why* there is none. "The mind is off", "the
key is unset", "the transport failed" and "the model declined" are five
different facts, and none of them may look like a run the mind found nothing
wrong with. That is the same fail-silent shape as a skipped check reporting
clean, and it is what most of `test_mind.py` is about.

A stage that drives a browser reports as it goes: each of the Scraper's own progress events
becomes a line in the run log, and a tool that has gone quiet for 90 seconds says so rather
than looking identical to one that is working. The Scraper runs in its own process group, so
Ctrl-C on Maestro no longer kills the browser mid-upload — and a process that *is* killed is
reported as killed rather than as a tool that failed.

## Code

| Module | What it does |
|---|---|
| `maestro/model.py` | The two-level state vocabulary — run stages advance the batch, problem stages advance individually through the Polygon half |
| `maestro/store.py` | Durable SQLite job store: resume across restarts, quarantine, identity map, cursor-tailed events |
| `maestro/manifest.py` | `MANIFEST.json` cross-checks M-1…M-16, including opening every archive and checking each limit against the measurement that justified it |
| `maestro/characteristics.py` | Parses exactly as `batch.py` does, then checks the six things it cannot report |
| `maestro/ingest.py` | Stage 1→2: sentinel detection, and telling "still copying" from "invalid" |
| `maestro/divisions.py` | The nine division names, mirrored from the Scraper and pinned to it by test |
| `maestro/settings.py` | The three values a batch chooses for itself — divisions, translation targets, contest list — with the config as the default each falls back to |
| `maestro/brief.py` | Stage 0: the instruction sent to the author, generated from the constants the gate enforces so the two cannot drift |
| `maestro/preflight.py` | P-1…P-6: the manifest against what the Middleman's own parser says will import — optional, so ingest still works with the service down. Also the far end of the same chain: L-1/L-2 and D-1/D-2, the authored limits and divisions against what the platform ended up with |
| `maestro/feedback.py` | The author's half of a rejection: every check paired with the contract clause it enforces, and an explicit list of the checks that never ran |
| `maestro/polygon.py` | Middleman client plus the decision policy layered over its error taxonomy |
| `maestro/polygon_lane.py` | Stages 3–5: one job per problem, quarantine on verify failure, extract into the upload parent |
| `maestro/scraper.py` | The Scraper's CLIs as a typed surface: output streams out live, exit codes carry the decision, a killed process is told apart from a failed one, and nothing mutates without `apply=True` |
| `maestro/electicode_lane.py` | Stages 6–8: preview before apply, reconcile before chores, retry only what is idempotent — and read the contest list back rather than believe the step that filled it |
| `maestro/scheduler.py` | The loop: Polygon runs fan out, ElectiCode runs strictly one at a time on a worker thread so a tens-of-minutes chore chain can't block a tick |
| `maestro/mind.py` | The one place a model belongs on this half: a reading of a stopped run, a closed vocabulary of four actions, and no path by which any of it can accept a delivery |
| `maestro/dashboard.py` | Stdlib HTTP over the event log, plus the only four mutations in the system: approve a run's writes, resume a stopped one, delete one, set its own divisions/targets/list |
| `maestro/__main__.py` | `brief`, `check`, `inspect`, `run`, `status`, `init` — config is a JSON file, not flags |

`python -m pytest` — 566 tests, no external services required.

Five of the test modules check Maestro against something outside itself, and skip when it is
absent. They exist because most of Maestro's risk is not in its own logic but in its *model* of
the systems around it, and a test that only checks Maestro against itself cannot see that model
drifting — the dashboard's delete button once shipped inert with every server-side test green:

- `test_dashboard_browser.py` — the page driven by a real Chromium: the buttons are clicked,
  not inspected. Needs `pip install playwright`; it uses an already-installed browser and
  never downloads one
- `test_scraper_roundtrip.py` — every command line Maestro builds, fed to the Scraper's own
  argument parsers, plus stage 8 run end to end against the real `report.py`. It found
  `--state` being sent to `report.py`, which does not take one, so stage 8 could never have
  succeeded — and stage 8 is the only stage no live run has yet reached

- `test_mind_roundtrip.py` — the one request that leaves the machine, checked against the
  real SDK's own signature. Every other mind test injects a transport, so none of them can
  see that call being wrong — and its parameter shapes have moved twice: `output_format`
  gave way to `output_config.format`, and `thinking.budget_tokens` is now rejected outright
  by the model this uses. Needs `pip install 'maestro[mind]'`
- `test_characteristics_roundtrip.py` — the characteristics Maestro renders, parsed back by
  the real `batch.py`, down to the positional tag alignment and the flag that protects
  existing tags
- `test_preflight_roundtrip.py` — the manifest cross-check against the real `zip_parser`,
  including that each field it reads means what it is assumed to mean

Secrets (Polygon key/secret, ElectiCode session cookies, Anthropic API key) stay local and
gitignored. Nothing here is exposed publicly — remote access is over a mesh VPN.
