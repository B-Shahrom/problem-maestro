# Maestro — what is actually being built

*Analysis of `maestro_plan_from_polygon_middleman.md`. Written before any code exists, to
name the decisions and risks the plan leaves open.*

---

## 1. One-sentence definition

Maestro is a **durable, resumable job engine** that drives a problem set from "authored
archives on disk" to "published, translated, tagged, ordered problems in an ElectiCode
contest" by calling one HTTP service (Polygon Middleman) and supervising a set of
Playwright CLIs (Platform Scraper), with a mobile-reachable dashboard over a private
network.

It owns **no domain logic**. Everything it knows how to do is: sequence, gate, retry,
report, and hold state.

## 2. What it decomposes into

| Component | Responsibility | Notes |
|---|---|---|
| **Job store** | Durable run/problem state, resumable across process restarts | SQLite is sufficient and correct here |
| **HTTP client** | Polygon Middleman (`:8000`) | Blocked on the backend import-endpoint port (plan §8.1) |
| **Process supervisor** | Spawn/monitor Platform Scraper CLIs | subprocess, sync Playwright — no event-loop conflict |
| **Queue / lock manager** | Serialize per external system | Non-negotiable, see §6 |
| **Dashboard** | Trigger runs, watch the unified log, approve apply-gates | Mobile-first, Tailscale-only |
| **AI author (v2, optional)** | Stage 1 via Anthropic SDK | Deliberately last |

## 3. The structural insight the plan doesn't make explicit: it's a two-level state machine

The plan draws stages 1→8 as one linear pipeline. It isn't. The **granularity changes
halfway through**:

- **Stages 3–5 (Polygon) are per-problem.** Each ZIP imports, builds, verifies, and
  downloads independently. Problem 7 can FAIL verify while 1–6 are READY.
- **Stages 6–8 (ElectiCode) are per-batch.** `problem_uploader.py upload --folder <parent>`
  takes the *parent* directory and creates N problems in one modal action.
  `batch.py --char characteristics.md` drives the whole set. `report.py audit` gates
  the whole run.

So Maestro needs `Run(id, status)` → `Problem(run_id, slug, stage, status)`, and a
**convergence point** between stage 5 and stage 6 where per-problem outcomes collapse
into one batch decision.

That convergence point is exactly the unanswered question in plan §9
("idempotency & partial-failure policy"). It resolves to a concrete choice:

> **When k of n problems fail Polygon verify, does Maestro (a) halt the whole batch,
> (b) quarantine the failures out of the extracted parent folder and upload the
> remaining n−k, or (c) ask?**

My recommendation: **(b) with an explicit confirmation on the dashboard**, because
`characteristics.md` is positional and per-slug — dropping a problem means the
characteristics file must be filtered to match, or `batch.py` will act on a slug that
was never uploaded. That filtering is Maestro's job and must be built, not assumed.

## 4. The highest-risk unknown: the filesystem seam

```
Polygon package (ZIP)  ──extract──▶  <parent>/  ──▶  problem_uploader.py --folder <parent>
                                     ├── <slug-a>/
                                     ├── <slug-b>/
                                     └── ...
```

The plan asserts this hand-off works ("extract into a parent folder of per-problem
folders" → "`upload --folder <parent>`"). **Nothing in the plan verifies that the
directory shape Polygon produces is the directory shape ElectiCode's uploader expects.**

If it isn't, Maestro needs a *shaping* step between 5 and 6 — rename folders to slugs,
flatten a nesting level, prune files the uploader chokes on. That is real work that
appears nowhere in the roadmap.

This is the one question that must be answered by **both** sides independently so the
answers can be diffed. Both actor prompts below ask for a literal `tree` output.

## 5. The join key: slug identity across three systems

`slug` is the primary key threading everything:

- the problem-developer names archives and `characteristics.md` rows by slug
- Polygon has its own problem name/id
- ElectiCode assigns its own id/slug on upload

Every stage-7 operation (`assign`, `division`, `list add/reorder`) is *per-problem* and
must target the right ElectiCode record. If any system mangles or reassigns the
identifier, the chore chain silently applies metadata to the wrong problem — a failure
mode that passes `report.py audit` if the audit only checks "is difficulty set?" rather
than "is *this* difficulty set on *this* problem".

Maestro must persist an identity map: `slug → polygon_problem_id → electicode_id`.
Both prompts ask how to obtain the third column.

## 6. Concurrency is more constrained than the plan implies

The Platform Scraper holds **one** admin session in a shared `session_state.json` and
drives a real browser. The Scraper's own GUI already models this correctly — an
"operation queue, drained one at a time". That's not a UI convenience, it's a
correctness constraint.

Maestro therefore needs **per-resource lanes**, not a global worker pool:

- ElectiCode lane: strictly serial (shared cookie jar, shared browser, admin UI state)
- Polygon lane: bounded (API rate limits + Polygon-side build queue)
- Local lane: extraction/shaping can parallelize freely

Getting this wrong doesn't fail loudly. It produces interleaved Playwright sessions
fighting over one login and half-applied metadata.

## 7. Machine-readable output is a prerequisite, not a nicety

Maestro is being asked to gate a pipeline on tool outcomes. If the Scraper CLIs only
emit human prose, Maestro must regex-scrape logs — brittle, and it silently breaks on
any wording change.

The right ask, and it is small and additive on the Scraper side:

- `--json` emitting **NDJSON on stdout**, one event per line, human logs on stderr
- **stable documented exit codes** (0 = success, and distinct non-zero codes for
  auth-expired / validation-failed / partial-failure / hard-error)
- a cheap **non-mutating session check** so Maestro can pre-flight before a 40-minute batch

Without these three, everything downstream is guesswork. Prompt B asks for them explicitly.

## 8. Session liveness is a first-class pipeline state

`session_state.json` cookies expire. The plan correctly says Maestro should "surface a
'log in' prompt rather than trying to automate the login" — but that implies a real
state: a run can be **BLOCKED_ON_AUTH**, parked, and resumed after a human logs in on the
host machine. That has to be modelled, not treated as an error.

Worst case if it isn't: an expired session makes Playwright land on a login page, the tool
times out mid-batch, and half the problems have divisions granted and half don't.

## 9. Preview-then-apply maps cleanly onto the mobile UX

Both apps already have the discipline (`--apply` on the Scraper; "don't commit if a step
errored" on the Middleman). Maestro should compose it into a single gate:

1. run the whole batch dry
2. render one consolidated diff on the phone
3. one **Apply** tap re-runs the identical plan with `--apply`

For that to be trustworthy, preview output must be machine-readable *and* a faithful
predictor of what apply does. Prompt B asks whether that's actually true today, per tool.

## 10. Correction to the plan's AI-layer section (§6)

The plan specifies `claude-opus-4-8` with `thinking={"type": "adaptive"}`.

- **Model:** use **`claude-opus-5`** — it's the current Opus and the right default for the
  reasoning-heavy authoring step. `claude-opus-4-8` is still valid, just a generation behind.
- **Adaptive thinking:** correct, and on Opus 5 it is the *default* — omitting `thinking`
  runs adaptive. `budget_tokens` is removed (400 error). Depth is controlled via
  `output_config={"effort": ...}` instead.
- **Tool Runner:** `client.beta.messages.tool_runner` is correct for the Python SDK — it
  drives the request → execute → loop cycle over tools you define and host.
- Sampling params (`temperature`, `top_p`, `top_k`) are rejected on this model tier.

## 11. Recommended build order (a re-cut of plan §8)

The plan's order is right; two things move:

1. **Backend import endpoint (Middleman).** Unchanged — hard prerequisite.
2. **Contracts before code.** Get the three manuals (prompts below) and *diff the seam
   answers* before writing the orchestrator. A wrong assumption here is a rewrite, not a patch.
3. **Job store + Polygon lane.** Watch folder → import → poll → download → extract → shape.
4. **ElectiCode lane.** Session preflight → upload → batch → audit gate.
5. **Dashboard + Tailscale.**
6. **AI authoring.** Last, optional.

## 12. Open decisions to make explicitly (not assume)

| # | Decision | Recommendation |
|---|---|---|
| 1 | Partial-failure policy at the 5→6 convergence | Quarantine + filter `characteristics.md` + confirm |
| 2 | Where the identity map lives and how ElectiCode ids are read back | Maestro's job store; needs a Scraper answer first |
| 3 | Scraper: subprocess vs HTTP wrapper | Subprocess (plan is right); revisit only if a uniform surface is wanted |
| 4 | ElectiCode contest ownership (`contest_creator.py` vs manual) | Decide before stage-7 wiring; affects list-url plumbing |
| 5 | Is the "done" signal from the author a sentinel file or a folder-stable timer? | Sentinel file — avoids ingesting a half-written folder |
| 6 | Retry semantics per stage (which failures are retryable) | Needs the error taxonomies from both prompts |
