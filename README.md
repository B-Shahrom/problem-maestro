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

Design phase. All three actors have returned their Phase 1 integration manuals; the
cross-actor analysis is in `docs/analysis/phase1-findings.md`.

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
- **`docs/contracts/`** — the problem-developer's authoring contract (output contract,
  characteristics spec, manifest spec, preflight checklist, system prompt, tool spec)
- **`docs/prompts/`** — the briefs sent to each actor, plus the Phase 2 corrections
- **`docs/procedures/`** — operator-run procedures needing a live session

## Next steps

Design questions are settled. Everything below is build work.

1. ~~Run 0~~ — **done, passed.** ElectiCode accepts Polygon's raw package; the shaping stage
   collapses to extract + rename folder to slug + delete `*.exe`. See the banner at the top of
   `docs/analysis/seam-verdict.md`.
2. Phase 2 in both apps — **in progress**, corrections sent.
3. Build order: ~~job store~~ → ~~ingest + validators~~ → ~~Polygon lane (incl. shaping)~~ →
   upload → **stage 6.5 reconcile** → post-upload chores → audit gate → dashboard.

## Code

| Module | What it does |
|---|---|
| `maestro/model.py` | The two-level state vocabulary — run stages advance the batch, problem stages advance individually through the Polygon half |
| `maestro/store.py` | Durable SQLite job store: resume across restarts, quarantine, identity map, cursor-tailed events |
| `maestro/manifest.py` | `MANIFEST.json` cross-checks M-1…M-14, including opening every archive |
| `maestro/characteristics.py` | Parses exactly as `batch.py` does, then checks the six things it cannot report |
| `maestro/ingest.py` | Stage 1→2: sentinel detection, and telling "still copying" from "invalid" |
| `maestro/polygon.py` | Middleman client plus the decision policy layered over its error taxonomy |
| `maestro/polygon_lane.py` | Stages 3–5: one job per problem, quarantine on verify failure, extract into the upload parent |

`python -m pytest` — 99 tests, no external services required.

Secrets (Polygon key/secret, ElectiCode session cookies, Anthropic API key) stay local and
gitignored. Nothing here is exposed publicly — remote access is over a mesh VPN.
