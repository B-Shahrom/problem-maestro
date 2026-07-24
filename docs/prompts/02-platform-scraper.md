# Prompt → Platform Scraper's AI developer

Copy everything below the line into that project's Claude session.

---

## Context

A separate orchestrator called **Maestro** is being built. It runs the full problem
lifecycle end to end: an AI problem-developer authors problems, a FastAPI service called
Polygon Middleman imports/builds/verifies them on Polygon and produces a folder of
extracted problem directories, and then **this toolkit** uploads them to ElectiCode and
does all post-upload processing.

Maestro will invoke your CLIs as **subprocesses**, exactly the way `gui.build_command`
already does — no GUI, no human watching. It will gate the pipeline on your exit codes
and needs to parse your results programmatically.

I need two things from you, in order. **Do Phase 1 first and stop.** Do not start
Phase 2 until I've read Phase 1 and replied.

## Ground rules — read these before answering anything

1. **Verify every claim against the actual code.** Cite `file.py:123` for each answer. An
   answer with no citation will be treated as a guess.
2. **If something does not exist, write `NOT IMPLEMENTED` and stop there.** Do not
   describe intended or planned behavior as if it exists. In particular: do not invent
   flags, exit codes, or output formats that aren't in the argparse definitions.
3. **If you are unsure, write `UNVERIFIED — <what you'd need to check>`.**
4. **Phase 1 changes no behavior.** Documentation only.
5. Never include cookies, credentials, or the contents of `session_state.json`. Structure
   and field names only.
6. Do not run any tool with `--apply` while answering this. Read the code; if you must
   execute something, preview mode only.

## Phase 1 — deliverables

Produce these files and commit them:

- `docs/MAESTRO_CLI_CONTRACT.md` — the prose manual, structured as the sections below
- `docs/maestro/tools.json` — a machine-readable manifest, one entry per tool:
  name, path, every subcommand, every flag (name, type, required, default, repeatable),
  which flags mutate, and the documented exit codes. Generate it from the argparse
  definitions rather than by hand where possible.
- `docs/maestro/characteristics.schema.md` — the formal specification of
  `characteristics.md` (see §9)
- `docs/maestro/characteristics.example.md` — a real, valid example file, at least 3
  problems, exercising every optional column and the numbered-tags section
- `docs/maestro/expected-input-tree.txt` — literal `tree` output of a folder layout that
  `problem_uploader.py upload --folder <parent>` accepts today (see §5)

### §1 Invocation contract, per tool

Cover: `problem_uploader.py`, `batch.py`, `problem_editor.py`, `division_access.py`,
`list_editor.py`, `contest_creator.py`, `verify.py`, `report.py`, `contest_scraper.py`.

For each: the exact argv shape (`gui.build_command` is the source of truth — reconcile it
against the argparse definitions and flag any drift), what each flag does, which are
required, and which combinations are invalid.

### §2 Exit codes — the most important section

For every tool: the complete set of exit codes it can return, and what each means.

I specifically need to distinguish these cases from the exit code alone, without parsing
any text:

- success, everything applied
- success, nothing to do (already in desired state)
- **partial success** — some items applied, some failed
- validation failure (bad `characteristics.md`, bad folder, bad args)
- **auth/session expired**
- ElectiCode returned an error
- transient/network failure worth retrying
- unexpected crash

If the current code just returns 0 or 1 — or worse, returns 0 on partial failure — say so
plainly. That's a Phase 2 item, not something to paper over.

### §3 Output format

- Exactly what each tool writes to **stdout** vs **stderr** today.
- Is there any `--json` / `--quiet` / machine-readable mode? If not, say `NOT IMPLEMENTED`.
- Does any tool write a result file or report artifact to disk? Path and format?
- Is progress reported incrementally (so Maestro can stream it to a dashboard) or only at
  the end?

### §4 Session and authentication lifecycle

- Where `session_state.json` lives, how `contest_scraper.py login` creates it, and which
  tools read it.
- **How can Maestro cheaply check whether the session is still valid, without mutating
  anything?** If there's no such command today, say so — it's a Phase 2 ask.
- What actually happens when the session has expired mid-run: does the tool detect it and
  exit, hang on a login page until timeout, or silently do nothing? Trace the real code path.
- Approximate session lifetime, if known.
- If a batch dies halfway because auth expired, **what state is left behind on ElectiCode**,
  and is re-running the same command safe?

### §5 Input folder layout — the hand-off seam (highest priority)

`problem_uploader.py upload --folder <parent>` will receive a directory produced by a
*different* tool (extracted Polygon packages). I need to know exactly what it accepts.

- The precise directory structure the uploader expects: how deep, what each level means,
  what files must be present in each problem folder.
- Is `--folder` the parent of many problem folders, or one problem folder?
- How does it decide what a "problem" is — directory name, a manifest file, a marker file?
- **Where does the slug come from** — the folder name, a file inside, something else? State
  the exact rule.
- What happens with unexpected extra files or subdirectories in a problem folder — ignored,
  uploaded, or a hard error?
- What does the "EXISTS" detection actually compare against, and what does it do on a match?
- `docs/maestro/expected-input-tree.txt` must show a real accepted layout.

### §6 Identity mapping

Every stage-7 operation targets individual problems. Maestro must be certain each one hits
the right record.

- After upload, how does Maestro learn ElectiCode's identifier for each uploaded problem?
  Is it returned, printed, discoverable via a scrape, or not obtainable at all?
- Do the downstream tools (`assign`, `division_access`, `list_editor`) address problems by
  slug, by ElectiCode id, by title, or by list position?
- Is the slug from the input folder preserved verbatim on ElectiCode, or transformed?
- **What happens if a slug in `characteristics.md` doesn't match anything on ElectiCode** —
  hard error, skip with a warning, or silently apply to the wrong problem?

### §7 Idempotency and re-runs

For each mutating tool: if Maestro runs it twice with identical arguments, what happens the
second time? Which operations are safe to repeat, and which duplicate/corrupt state
(especially `list_editor add` and `contest_creator create`)?

### §8 Preview vs apply

- Confirm every mutating tool previews by default and only writes with `--apply`. Any
  exceptions?
- **Is the preview a faithful predictor of apply?** Does it perform the same reads and
  compute the same decisions, or is it a rough approximation? Trace one tool to answer.
- What exactly does preview output contain — is it enough for a human to approve from, and
  is it parseable?
- Is there any tool where preview and apply can diverge (state changed in between,
  non-deterministic ordering)?

### §9 `characteristics.md` — the metadata contract

This file is the linchpin: the AI problem-developer will generate it, and it drives your
entire post-upload chain. I need it specified precisely enough that a generator can be
written against it and validated **before** a 40-minute run.

- The **General** table: every column, exact header spelling, required vs optional,
  accepted value formats (slug charset, title constraints, difficulty group values,
  subtasks encoding, languages encoding).
- The **Suggested tags** section: exact format, how the numbering maps positionally onto
  General rows, what happens if counts mismatch, whether tags must come from a fixed
  vocabulary (if so, where that vocabulary lives).
- Any other sections the parser reads.
- All validation the parser performs today, and — separately — what it *fails to* validate
  but should.
- Is parsing strict or lenient? Does an unknown column error out or get ignored?
- Point me at the parser's entry point (`file.py:line`).

### §10 `batch.py` stage graph

- The exact ordered stages (`fixmdx` → `translate` → `assign` → custom assign → division →
  list add + reorder) and which are conditional on flags or on `characteristics.md` content.
- Are there `--only` / `--skip` / `--from-stage` flags? If not, say `NOT IMPLEMENTED`.
- **If stage 4 of 6 fails, what happens to stages 5 and 6** — abort, continue, prompt?
- Can a partially-completed batch be resumed, or must it be re-run from the start? If
  re-run, is that safe (see §7)?
- Per-stage timeouts and retries, if any.

### §11 Concurrency and headless operation

- Can two of these tools run at the same time? What breaks — `session_state.json` writes,
  the browser instance, ElectiCode admin UI state?
- **Your explicit recommendation**: what must Maestro serialize, and what may it parallelize?
- Do the tools run headless (no display), and is that the default? Anything needed on the
  host (display server, browser install, Playwright deps)?
- Per-tool wall-clock expectations for a 10-problem set, so Maestro can size timeouts.

### §12 `verify.py` and `report.py audit` — the gate

- Exactly what each checks.
- `report.py audit` exits 1 on gaps — enumerate every condition that triggers it.
- Is the audit result available as structured data, or only as printed text?
- Does the audit verify that metadata landed on the **correct** problem, or only that
  *some* value is present? (This matters: a mis-targeted assign can pass a
  presence-only audit.)

---

## Phase 2 — small additive changes (do not start until I reply to Phase 1)

Everything here is additive. **Do not change existing default behavior** — current
invocations must keep working identically.

1. **`--json` on every tool.** NDJSON on stdout, one event per line; human logs move to
   stderr. Include at minimum: a start event, per-item progress events, per-item results
   (with slug and outcome), and a final summary event with counts. Document the event
   schemas in `docs/maestro/events.md`.
2. **Stable exit codes** per §2, applied consistently across all tools and documented in
   `docs/maestro/tools.json`. Partial failure must be distinguishable from full success.
3. **A non-mutating session check** — e.g. `contest_scraper.py session --check --json`
   exiting 0 for valid, a dedicated code for expired, and printing an expiry hint if
   obtainable. Maestro will run this before every batch.
4. **A `characteristics.md` validator** — e.g. `batch.py validate --char <file> --json`
   that checks the file (and optionally cross-checks slugs against what's on ElectiCode)
   and exits non-zero on problems, without touching anything.
5. **`--only` / `--skip` stage selection on `batch.py`**, so Maestro can resume after a
   mid-chain failure instead of re-running the whole chain.

Do them in that order, one commit each. After each, tell me what you changed and how you
verified it — including whether you ran it against real ElectiCode in preview mode.
