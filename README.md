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
- **`docs/prompts/`** — the briefs originally sent to each actor

## Next steps

Design questions are settled. Everything below is build work.

1. One preview-mode test upload of a hand-shaped folder, to close the three server-side
   unknowns in `seam-verdict.md` §4 (statement images, `solutions/main.cpp` naming, whether the
   extra `statements/` subdirectories are tolerated).
2. Phase 2 in both apps, correctness items first — exact-match slug resolution and the
   `--char` audit ahead of `--json`; async job model and per-error codes on the Middleman.
3. Build order: job store → Polygon lane → shaping stage → upload → **stage 6.5 reconcile** →
   post-upload chores → audit gate → dashboard.

Secrets (Polygon key/secret, ElectiCode session cookies, Anthropic API key) stay local and
gitignored. Nothing here is exposed publicly — remote access is over a mesh VPN.
