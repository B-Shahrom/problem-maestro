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

Design phase. No implementation yet — the integration contracts with the two existing
apps are being pinned down first, because the filesystem hand-off between them
(Polygon package extraction → ElectiCode uploader input) is unverified and a wrong
assumption there is a rewrite rather than a patch.

## Contents

- **`docs/plan/maestro-plan.md`** — the original plan, written from the Polygon Middleman side
- **`docs/analysis/maestro-understanding.md`** — analysis of that plan: what's being built,
  the two-level state machine, the hand-off seam risk, identity mapping, concurrency
  constraints, and the decisions that need making explicitly
- **`docs/prompts/`** — briefs to hand to each actor's AI developer, asking for the
  integration manuals Maestro needs before orchestration code is written

## Next steps

1. Send the three prompts in `docs/prompts/` to their respective actors.
2. **Diff the two seam answers** — `expected-input-tree.txt` (Scraper) against
   `package-tree.txt` (Middleman). If they don't match, a shaping step goes into the
   pipeline between stages 5 and 6.
3. Port the Middleman's import pipeline into its backend (prerequisite for headless runs).
4. Build the job store and the Polygon lane, then the ElectiCode lane, then the dashboard.

Secrets (Polygon key/secret, ElectiCode session cookies, Anthropic API key) stay local and
gitignored. Nothing here is exposed publicly — remote access is over a mesh VPN.
