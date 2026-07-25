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
- **`docs/analysis/author-lane.md`** — the half of the pipeline that isn't built yet: what
  "talking to the developer" decomposes into, why the authoring actor needs Managed Agents
  rather than the Claude API alone, and why the gate must stay deterministic under all of it
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

What remains on this half is not code — it's the first supervised end-to-end run against
live services. The procedure for that is `docs/procedures/first-live-run.md`.

The half that is *not* built is everything before the watch directory: briefing the author,
receiving the delivery, and getting a rejected set corrected. The first of those three now
exists — a rejected folder gets a written correction request naming the contract clause each
finding breaks. The rest, and the case for Managed Agents over the Claude API for the
authoring actor, is `docs/analysis/author-lane.md`.

## Running it

```
python -m maestro init                 # write a starter config.json
python -m maestro check                # validate paths AND that the Scraper checkout is current
python -m maestro inspect              # what Maestro makes of each folder in watch_dir
python -m maestro inspect --report     # …and write a correction request beside each rejection
python -m maestro run                  # scheduler + dashboard on :8787
python -m maestro status               # one-shot listing; non-zero if a run wants a human
```

`apply` is **off** by default, so a fresh install previews and parks each run at its first
write. Approve individual runs in the dashboard, or set `apply: true` once you trust it.
`config.json` is gitignored — it points at the session file and the Middleman.

## Code

| Module | What it does |
|---|---|
| `maestro/model.py` | The two-level state vocabulary — run stages advance the batch, problem stages advance individually through the Polygon half |
| `maestro/store.py` | Durable SQLite job store: resume across restarts, quarantine, identity map, cursor-tailed events |
| `maestro/manifest.py` | `MANIFEST.json` cross-checks M-1…M-14, including opening every archive |
| `maestro/characteristics.py` | Parses exactly as `batch.py` does, then checks the six things it cannot report |
| `maestro/ingest.py` | Stage 1→2: sentinel detection, and telling "still copying" from "invalid" |
| `maestro/preflight.py` | P-1…P-6: the manifest against what the Middleman's own parser says will import — optional, so ingest still works with the service down |
| `maestro/feedback.py` | The author's half of a rejection: every check paired with the contract clause it enforces, and an explicit list of the checks that never ran |
| `maestro/polygon.py` | Middleman client plus the decision policy layered over its error taxonomy |
| `maestro/polygon_lane.py` | Stages 3–5: one job per problem, quarantine on verify failure, extract into the upload parent |
| `maestro/scraper.py` | The Scraper's CLIs as a typed surface: exit codes carry the decision, nothing mutates without `apply=True` |
| `maestro/electicode_lane.py` | Stages 6–8: preview before apply, reconcile before chores, retry only what is idempotent |
| `maestro/scheduler.py` | The loop: Polygon runs fan out, ElectiCode runs strictly one at a time on a worker thread so a tens-of-minutes chore chain can't block a tick |
| `maestro/dashboard.py` | Stdlib HTTP over the event log, plus the only two mutations in the system: approve a run's writes, resume a stopped one |
| `maestro/__main__.py` | `run`, `status`, `init` — config is a JSON file, not flags |

`python -m pytest` — 315 tests, no external services required.

Two of the test modules talk to the other repos' real code when a checkout is present, and
skip otherwise (`MAESTRO_SCRAPER_REPO`, `MAESTRO_MIDDLEMAN_REPO`). They exist because most of
Maestro's risk is not in its own logic but in its *model* of the other two systems, and a
test that only checks Maestro against itself cannot see that model drifting:

- `test_characteristics_roundtrip.py` — the characteristics Maestro renders, parsed back by
  the real `batch.py`, down to the positional tag alignment and the flag that protects
  existing tags
- `test_preflight_roundtrip.py` — the manifest cross-check against the real `zip_parser`,
  including that each field it reads means what it is assumed to mean

Secrets (Polygon key/secret, ElectiCode session cookies, Anthropic API key) stay local and
gitignored. Nothing here is exposed publicly — remote access is over a mesh VPN.
