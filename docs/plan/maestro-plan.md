# Maestro — Master Orchestrator Plan

*(written from the Polygon Middleman side; the master program that conducts the full problem → Polygon → ElectiCode pipeline)*

---

## 0. Name

Working title: **Maestro** — it means "master", it's the word for the person who *conducts an orchestra* (an orchestrator), and it fits exactly what this is: it does none of the work itself, it directs the other apps. Full title e.g. **Polygon Maestro**; handle `maestro` / repo `problem-maestro`.

Alternates you liked the shape of: **Conductor**, **Overseer**, **Foreman**, **Quartermaster**, **Setmaster** / **Problemsmith** (a CP author is a "problem setter"), **Problem Forge**. Recommendation stands at **Maestro**; swap freely — this doc uses it as a placeholder.

---

## 1. What Maestro is

A single orchestrator that runs the entire problem lifecycle end-to-end, across **three actors**:

| Actor | Role | Surface today |
|---|---|---|
| **Problem-developer** | Authors problems (statement, checker, solution, tests, editorial) | A Claude chat *project* (one new chat per problem set) |
| **Polygon Middleman** | Everything on **Polygon** — import, build, verify, package | FastAPI backend (`:8000`) + React UI |
| **Platform Scraper** | Everything on **ElectiCode** — upload + all post-upload processing | Python/Playwright CLI toolkit + Tkinter GUI |

Maestro is the conductor over all three. It owns no domain logic itself — it sequences the two apps and (optionally) drives the AI author, exposes one dashboard, and handles remote access.

---

## 2. The end-to-end pipeline

```
                                 ┌──────────────────────────┐
   (1) Problem-developer  ──────▶│ archives (ZIPs) +        │
       (Claude API / manual)     │ characteristics.md       │
                                 └────────────┬─────────────┘
                                              │
   ┌───────────────────── POLYGON SIDE (Polygon Middleman) ─────────────────────┐
   │ (2) collect archives                                                        │
   │ (3) import ZIP(s)  → create → statement → checker → solution → tests →      │
   │                      groups/points → commit                                 │
   │ (4) build + verify → buildPackage(verify) → poll problem.packages READY/FAIL│
   │ (5) download + extract the READY Polygon package  → extracted problem folders│
   └────────────────────────────────┬───────────────────────────────────────────┘
                                     │ folders on disk
   ┌──────────────────── ELECTICODE SIDE (Platform Scraper) ─────────────────────┐
   │ (6) upload to ElectiCode      → problem_uploader.py upload --folder <parent> │
   │ (7) post-upload processing    → batch.py run --char characteristics.md:      │
   │        • fixmdx      (repair leftover LaTeX from the Polygon import)         │
   │        • translate  (EN → RU / TJK / UZ)                                     │
   │        • assign     (difficulty + categories/tags, per-problem)              │
   │        • division   (grant division access)                                 │
   │        • list add + reorder  (into a contest / lesson list, easy→hard)       │
   │        • (optional) contest_creator.py  (create the ElectiCode contest)     │
   │ (8) verify / audit            → verify.py + report.py  (gate the run)        │
   └─────────────────────────────────────────────────────────────────────────────┘
```

Stage 6 and 7 are both **the Platform Scraper** — `problem_uploader.py` is the ElectiCode counterpart to Polygon Middleman's Import-ZIP, and `batch.py` chains the "assign values / fix / handle cases" chores you referred to.

---

## 3. Component responsibilities

| Stage | Owner | Concrete mechanism |
|---|---|---|
| 1. Author problems | Problem-developer | Claude chat project → archives + a `characteristics.md`. Automatable later via the Claude API (§6). |
| 2. Collect archives | Maestro | Watch a folder / pull from the developer's output. |
| 3. Import to Polygon | Polygon Middleman | Import-ZIP pipeline (create → … → commit). Today lives in the **frontend**; port to a backend endpoint (§5). Supports multi-archive test packs + `<slug>-tests` append. |
| 4. Build + verify | Polygon Middleman | `buildPackage(full=false, verify=true)` then poll `problem.packages` for `READY`/`FAILED`. |
| 5. Download + extract package | Polygon Middleman | Latest `READY` package → download → extract into a parent folder of per-problem folders. |
| 6. Upload to ElectiCode | Platform Scraper | `problem_uploader.py upload --folder <parent> --apply` — sets the folder onto the admin Upload modal's hidden `webkitdirectory` input, waits for detection, reports EXISTS, clicks *Upload & Create N Problems*. |
| 7. Post-upload chores | Platform Scraper | `batch.py run --char characteristics.md --apply` → fixmdx, translate, assign (difficulty+tags), division grant, add+reorder into a list; `contest_creator.py` for the ElectiCode contest. |
| 8. Verify / audit | Platform Scraper | `verify.py` (scrape re-audit) + `report.py audit` (flags missing difficulty/category/division; exit 1 gates the pipeline). |

**Note on two "contest create" tools — they are not duplicates:** Polygon Middleman's contest feature creates a contest **on Polygon**; Platform Scraper's `contest_creator.py` creates one **on ElectiCode**. Different platforms, both legitimately in the pipeline.

---

## 4. Architecture — how Maestro talks to each app

Two different integration styles, because the two apps are built differently.

### 4a. Polygon Middleman → HTTP API
It's already an HTTP service. Maestro calls its endpoints.

- **Prerequisite (highest-leverage change):** the full import pipeline currently lives in the **frontend** (`runImportPipeline` in TypeScript), so only a browser can drive a whole import. **Port it into the backend** as:
  - `POST /api/import-problem` (accept a ZIP → create → … → commit → build+verify server-side)
  - `GET /api/verify-status/{id}` (poll READY/FAILED)
  - `GET /api/download-package/{id}` (fetch the extracted-ready package)

  After that, both the UI and Maestro drive imports through the same endpoints.
- Binds `127.0.0.1`, no auth (deliberate) → reach it over the private network only (§7).

### 4b. Platform Scraper → subprocess (its native shape)
It's a set of self-contained Python CLIs (`argparse` + **Playwright sync**), and its own GUI already drives them **as subprocesses** (`gui.build_command` → argv → subprocess). Maestro does the same:

- Invoke each tool as a subprocess: `problem_uploader.py`, `batch.py`, `verify.py`, `report.py`, `division_access.py`, `list_editor.py`, `contest_creator.py`.
- **This sidesteps the Windows asyncio-subprocess trap** we hit with Polygon-contest automation: Playwright runs in a *separate process* with the **sync** API, so there's no shared event loop to conflict with. No Proactor-loop shim needed — just spawn the CLI.
- **Login is a one-time manual step:** `python contest_scraper.py login --url https://www.electicode.com` opens a real browser; you sign in by hand; cookies save to `session_state.json` (gitignored, shared by all tools). Maestro should check the session is valid and, if not, surface a "log in" prompt rather than trying to automate the login.
- **Preview-then-apply safety:** every write tool previews once and only mutates with `--apply`. Maestro should honor that — dry-run first, then apply on confirmation (mirrors the Middleman's "don't commit if a step errored" discipline).

> Optional later: wrap the Platform Scraper in a thin FastAPI shell so Maestro calls it over HTTP like the Middleman. Not needed to start — subprocess is the path of least resistance and matches how the GUI already works.

### 4c. Topology

```
                 ┌───────────────────────────┐
   Claude API ──▶│ Problem-developer (step 1)│
                 └────────────┬──────────────┘
                              │ archives + characteristics.md
   You (web / mobile)   ┌─────▼──────┐
        via Tailscale ─▶│   MAESTRO  │
                        └──┬──────┬──┘
             HTTP (API)    │      │   subprocess (CLIs)
              ┌────────────▼─┐  ┌─▼───────────────────┐
              │Polygon       │  │Platform Scraper      │
              │Middleman :8000│  │(Playwright→ElectiCode)│
              └──────┬───────┘  └──────────┬───────────┘
                     ▼                     ▼
                  Polygon               ElectiCode
```

---

## 5. The `characteristics.md` contract (the linchpin)

The Platform Scraper's `batch.py` is driven by a **`characteristics.md`**: a **General** table (slug, title, difficulty *group*, *subtasks*, *languages*) plus numbered **Suggested tags** (positional → the General rows). From it, `batch.py` derives, in order: fixmdx → translate → per-problem assign (difficulty+tags) → custom assign → division grant → add+reorder into a list.

**So the metadata contract between the AI author and ElectiCode post-processing already exists.** Make the **problem-developer emit `characteristics.md`** alongside the archives. Then Maestro's stage 7 is essentially:

```bash
python batch.py run --char characteristics.md \
    --divisions "Electi,Division A+" --targets ru,tg,uz \
    --list-url <electicode-contest-manage-url> --fixmdx all --apply
```

This is the single biggest automation win on the ElectiCode side: one artifact from the author drives the entire post-upload chore chain.

---

## 6. The AI layer (problem-developer)

Use the **Claude Developer API via the official Anthropic SDK** (Python — matches both apps). **Not** Claude Code subscription/OAuth tokens (those are for the CLI); "trusted device" is not an API concept. Maestro holds its **own API key**.

- **Model:** `claude-opus-4-8` with **adaptive thinking** (`thinking={"type": "adaptive"}`) — best for the reasoning-heavy authoring step.
- **Auth (simplest first):** `ANTHROPIC_API_KEY` in a gitignored config (exactly like the Polygon creds) → or an `ant auth login` OAuth profile the SDK auto-detects → or Workload Identity Federation if it ever runs in cloud/CI.
- **Altitude — keep it a workflow, not an autonomous agent.** Stages 2–8 are *deterministic orchestration* (call the API, spawn the CLIs, gate on verify) — plain code. Only stage 1 is model-driven:
  - **v1 — manual handoff:** you run your Claude "problem developer" project per set, drop archives + `characteristics.md` in a folder, Maestro takes over. Zero AI code in Maestro to start.
  - **v2 — API-driven author:** replicate the project's instructions as the **system prompt** of a Messages API call. If authoring needs to *run code* (generate/validate tests, produce the package), use the SDK's **Tool Runner** (`client.beta.messages.tool_runner`) so Claude calls your "write test"/"run solution" tools in a loop — still your infra, you host the tools.

---

## 7. Access & security (local + phone, cross-network)

Both apps have **no auth**: Polygon Middleman binds `127.0.0.1`; the Platform Scraper holds a live admin session in `session_state.json`. So Maestro must not be publicly exposed.

- **Mesh VPN — Tailscale (or WireGuard).** Your phone joins the tailnet and reaches Maestro over the private network; nothing public, no auth layer to build. Recommended.
- **Do not** put Maestro (or either app) on a public port without first adding token auth + TLS + a reverse proxy — that's a real project, not a checkbox.
- **Secrets stay local & gitignored:** Middleman `config.json` (Polygon key/secret, CF web login), Scraper `session_state.json` (ElectiCode cookies), Maestro's Anthropic API key. Never commit any of them.
- **Mobile-friendly dashboard:** give Maestro a small web UI to trigger a run and watch progress — reuse the "transparent live activity log" idea already built into the Middleman backend (`GET /`), and the Scraper GUI's **operation queue** concept (queue ops, drain one at a time, retry-failed).

---

## 8. Build roadmap

1. **Backend import endpoint (Middleman).** Port `runImportPipeline` → `POST /api/import-problem` + verify-status + download-package. *Prerequisite for headless orchestration.*
2. **Maestro skeleton.** Small FastAPI/CLI service: watch archive folder → import → poll verify → download+extract package. Deterministic, no AI yet.
3. **Wire the ElectiCode side.** Shell out to `problem_uploader.py upload --folder <extracted-parent> --apply`, then `batch.py run --char characteristics.md --apply`, then `report.py audit` to gate. Ensure `session_state.json` validity is checked first.
4. **`characteristics.md` as the metadata contract.** Have the author emit it; thread it into `batch.py`.
5. **Remote access.** Tailscale + a mobile web dashboard (unified activity log + op queue over both apps).
6. **AI authoring (optional).** Add the Claude API problem-developer, replacing the manual handoff when you want full automation.

---

## 9. Open decisions

- **Stage 1 — manual vs API author?** Start manual (developer project) → automate via the API later.
- **Platform Scraper: subprocess vs HTTP wrapper?** Start subprocess (native, matches its GUI). Add a FastAPI shell only if you want one uniform HTTP surface.
- **Language for Maestro.** Python is the path of least resistance — both apps are Python, and the Anthropic SDK + `subprocess` orchestration are first-class there. (It's just an HTTP client + process runner, so anything works.)
- **ElectiCode contest ownership.** Decide whether contests are created by `contest_creator.py` (ElectiCode) as part of the batch, or managed separately — and how they relate to the Polygon-side contest feature.
- **Idempotency & partial-failure policy across app boundaries.** Both apps are preview-then-apply and self-verify; Maestro should define what happens when stage 4 (Polygon verify) or stage 8 (ElectiCode audit) fails — halt the batch, report, and let you resume from that stage (mirrors the Middleman "don't proceed if a step errored" rule).

---

## 10. One-glance tool map (Platform Scraper)

For stage 6–8, the tools Maestro will invoke:

| Tool | Use in the pipeline |
|---|---|
| `problem_uploader.py upload` | Stage 6 — upload extracted Polygon folders to ElectiCode. |
| `batch.py run --char …` | Stage 7 — the whole post-upload chore chain from `characteristics.md`. |
| `problem_editor.py assign / translate / fixmdx / detail / content` | Stage 7 primitives (metadata, translation, MDX repair). |
| `division_access.py set` | Stage 7 — per-problem division access. |
| `list_editor.py add / reorder` | Stage 7 — populate & order a contest/lesson list. |
| `contest_creator.py create` | Stage 7 (optional) — create + populate the ElectiCode contest. |
| `verify.py`, `report.py audit` | Stage 8 — gate the run (exit 1 on gaps/discrepancies). |
| `contest_scraper.py login` | One-time — establish `session_state.json`. |

`gui.build_command` in the Scraper shows the exact argv shape for each — Maestro can reuse that mapping.
