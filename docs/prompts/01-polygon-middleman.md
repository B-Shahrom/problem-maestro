# Prompt → Polygon Middleman's AI developer

Copy everything below the line into that project's Claude session.

---

## Context

A separate orchestrator called **Maestro** is being built. It runs the full problem
lifecycle end to end across three systems: an AI problem-developer, **this app**
(Polygon Middleman), and a Playwright CLI toolkit called Platform Scraper that publishes
to ElectiCode.

Maestro will drive this app **headlessly over HTTP** — no browser, no human clicking the
React UI. It needs to import problem archives, build+verify packages, poll for
completion, download the READY package, and hand the extracted folders to another tool.

I need two things from you, in order. **Do Phase 1 first and stop.** Do not start
Phase 2 until I've read Phase 1 and replied.

## Ground rules — read these before answering anything

1. **Verify every claim against the actual code.** Cite `file.ts:123` / `file.py:45` for
   each answer. An answer with no citation will be treated as a guess.
2. **If something does not exist, write `NOT IMPLEMENTED` and stop there.** Do not
   describe intended, planned, or "should be" behavior as if it exists. A confident wrong
   answer here costs far more than a blank.
3. **If you are unsure, write `UNVERIFIED — <what you'd need to check>`.** Uncertainty is
   a useful answer. Invention is not.
4. **Phase 1 changes no behavior.** Documentation only. No refactors, no cleanups, no
   "while I was in there" fixes.
5. Do not include credentials, API keys, tokens, or the contents of `config.json` in
   anything you write. Field *names* only, never values.

## Phase 1 — deliverables

Produce these files and commit them:

- `docs/MAESTRO_INTEGRATION.md` — the prose manual, structured as the sections below
- `docs/maestro/openapi.json` — a real OpenAPI 3.x document for every endpoint that
  exists **today**, generated from or checked against the actual route definitions
- `docs/maestro/package-tree.txt` — literal `tree` (or `find . | sort`) output of one real
  extracted Polygon package, from the extraction root down, at least 3 levels deep, for a
  multi-problem package if one exists. Redact nothing structural.
- `docs/maestro/errors.md` — the error taxonomy table from §9 below

### §1 Runtime and configuration

- Exact command to start the backend, and the frontend if it's needed at all.
- Bind address, port, and whether that is configurable.
- Auth: confirm there is none. If there is anything (a token, a header, CORS
  restrictions), describe it exactly.
- Every key the config file expects — **names, types, and which are required**. No values.
- A health/readiness endpoint if one exists. If not, say so — Maestro needs to know when
  the service is up before starting a run.
- Python version, dependency manager, and anything that must be installed on the host.

### §2 Endpoint inventory

For every endpoint that exists today: method, path, request schema, response schema, all
status codes it can return, and the shape of an error body. This is what
`docs/maestro/openapi.json` must capture; the prose file can summarize.

Flag explicitly which endpoints are **only** usable from the browser (session state,
CORS, multi-step flows the UI holds together).

### §3 The import pipeline — trace it step by step

`runImportPipeline` currently lives in the frontend TypeScript. Walk it and document:

- The exact ordered sequence of Polygon API calls it makes
  (create → statement → checker → solution → tests → groups/points → commit).
- For each step: what it sends, what it checks in the response, and what it does on failure.
- **Where state lives between steps.** Is anything held in browser memory that a backend
  port would need to reconstruct?
- How multi-archive test packs work, and the `<slug>-tests` append convention — what
  triggers it, what the naming rule is exactly, what happens if the base problem is absent.
- Is the pipeline **idempotent**? If Maestro re-runs an import for a slug that already
  exists on Polygon, what happens — overwrite, error, duplicate, silent no-op?
- Is there any rollback if step 5 of 7 fails? What state is left on Polygon?

### §4 Build and verify

- The exact `buildPackage` call and its parameters.
- How package status is polled: which endpoint, what the response looks like, what the
  full set of status values is (not just READY/FAILED).
- Typical and worst-case duration for a build+verify. Is there a timeout? Should Maestro
  impose one?
- On FAILED: where does the reason live, is it machine-readable, and can Maestro fetch
  the verify log programmatically?
- What distinguishes "the problem is broken" from "Polygon had a transient error"? Maestro
  needs to know what is worth retrying.

### §5 Package download and extraction — the hand-off (highest priority)

This is where this app hands off to the ElectiCode side, and it's the riskiest unknown in
the whole design. Be precise.

- How the latest READY package is located and downloaded.
- Does this app extract it, or only download the archive?
- **The exact on-disk layout after extraction.** This is what
  `docs/maestro/package-tree.txt` is for. I need the real thing, not a description.
- Are per-problem directories named by slug? By Polygon problem id? By something else? Is
  there a nesting level above them?
- Does one package contain one problem or many? If it can contain many, show that case.
- Are there files in there that are Polygon-specific and would need pruning before another
  tool consumes the folder?

### §6 Identity

- What is the canonical identifier for a problem in this app — the slug from the archive,
  a Polygon-assigned id, or both?
- Is the slug from the input ZIP preserved verbatim through to the extracted folder name?
  If it's transformed (case, separators, prefixes), state the exact transformation.
- Given a slug, how does Maestro look up the corresponding Polygon problem/package?

### §7 Concurrency and rate limits

- Can two imports run simultaneously? Is there shared state that would break?
- Any Polygon-side API rate limits you've hit or know about, and the recommended
  concurrency ceiling.
- Is there any global/singleton state in the backend that assumes one operation at a time?

### §8 Observability

The plan mentions a "transparent live activity log" on `GET /`. I need to surface that in
Maestro's unified dashboard.

- How is that log produced and stored today?
- Is there any streaming interface (SSE, WebSocket)? If not, is there a poll endpoint with
  a cursor/offset so Maestro can tail without re-reading everything?
- Do long-running operations have a job/operation id Maestro can use to correlate?

### §9 Error taxonomy

A table: error condition → how it surfaces (status code, body shape, log line) →
**retryable: yes/no** → recommended handling. Cover at minimum: bad ZIP, Polygon auth
failure, Polygon rate limit, verify failure, network timeout, package-not-ready.

### §10 Headless blockers

What breaks, today, if there is no browser? List every operation that currently requires
the React UI and cannot be performed via HTTP.

---

## Phase 2 — the backend port (do not start until I reply to Phase 1)

Port `runImportPipeline` from the frontend into the backend so the same pipeline is
reachable over HTTP:

- `POST /api/import-problem` — accept a ZIP, run create → … → commit server-side, kick off
  build+verify, return a job id immediately (do not block for minutes)
- `GET  /api/verify-status/{id}` — poll: status, progress, failure reason
- `GET  /api/download-package/{id}` — fetch the READY package

Requirements:

- **The React UI must be migrated to call these same endpoints.** One implementation, two
  callers. Do not leave the pipeline duplicated in TypeScript and Python.
- Preserve the existing "don't commit if a step errored" discipline exactly.
- Errors must be machine-distinguishable per the §9 taxonomy — Maestro decides
  retry-vs-halt from the response, not from log text.
- Update `docs/maestro/openapi.json` in the same change.
- Tell me plainly what you did **not** port and why.

When Phase 2 is done, report: what changed, how you verified it (an actual end-to-end
import against Polygon, not just "it compiles"), and anything that behaves differently
from the frontend version.
