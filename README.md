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

Headline: **the filesystem hand-off does not match.** Polygon emits a flat, slug-less
package (one problem per zip, identified only by numeric id); the ElectiCode uploader wants
a parent directory of slug-named problem folders. A shaping stage between stages 5 and 6 is
mandatory, not optional. Two further findings — a slug-addressing hole that lets metadata
land on the wrong problem undetected, and three CLI tools that cannot express failure —
reorder the Scraper's Phase 2 work toward correctness before observability.

## Contents

- **`docs/plan/maestro-plan.md`** — the original plan, written from the Polygon Middleman side
- **`docs/analysis/maestro-understanding.md`** — analysis of that plan: the two-level state
  machine, the hand-off seam risk, identity mapping, concurrency constraints
- **`docs/analysis/phase1-findings.md`** — what the three Phase 1 reports say when read
  against each other: the seam verdict, the identity hole, and four cross-actor contradictions
  no single actor could see
- **`docs/contracts/`** — the problem-developer's authoring contract (output contract,
  characteristics spec, manifest spec, preflight checklist, system prompt, tool spec)
- **`docs/prompts/`** — the briefs originally sent to each actor

## Next steps

1. Answer the two empirical questions that gate everything: does a package with one solution
   and no validator pass Polygon's `buildPackage(verify=true)`, and does the import pipeline
   mark `s0` tests as samples.
2. Collect the outstanding Scraper and Middleman documents (`docs/analysis/phase1-findings.md`
   §7) and run the seam diff and the `characteristics.md` format diff.
3. Reconcile the two independent `characteristics.md` specs — consumer wins on layout,
   author wins on semantics.
4. Phase 2 in both apps, correctness items first.
5. Build the job store and the Polygon lane, then the shaping stage, then the ElectiCode
   lane, then the dashboard.

Secrets (Polygon key/secret, ElectiCode session cookies, Anthropic API key) stay local and
gitignored. Nothing here is exposed publicly — remote access is over a mesh VPN.
