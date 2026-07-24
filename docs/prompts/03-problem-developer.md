# Prompt → the Problem-developer (the Claude project that authors problems)

This one goes into your existing problem-developer Claude *project* — the chat you already
use to author problem sets. Copy everything below the line.

If that project is backed by a repo, ask it to commit the deliverables there. If it's a
pure chat project, have it output the files and save them into
`problem-maestro/docs/contracts/` yourself.

---

## Context

I'm building an orchestrator called **Maestro** that takes everything you produce and runs
it the rest of the way automatically: imports the archives into Polygon, builds and
verifies packages, uploads the results to ElectiCode, then translates, tags, assigns
difficulty, grants division access, and orders the problems into a contest list — with no
human in the loop.

Right now your output is consumed by a human who fixes up anything inconsistent. Once
Maestro is driving, **anything ambiguous in your output becomes a pipeline failure at 2am**,
or worse, metadata silently applied to the wrong problem.

So I need your output turned into a **contract**: precisely specified, self-validated, and
identical every single time.

I'm not asking you to change how you author problems. I'm asking you to pin down and
document exactly what you emit, and then make it deterministic.

## Ground rules

- **Describe what you actually do today first, before proposing changes.** If your naming
  or structure has drifted between problem sets, say so — that drift is exactly what I need
  to find.
- If you're unsure whether something is guaranteed or incidental, say so explicitly.
- Don't invent a format you think sounds good. Document the real one, then propose the fix.

## Deliverable 1 — `OUTPUT_CONTRACT.md`

Specify exactly what one completed problem set looks like on disk:

- The directory layout you produce, as a literal tree.
- **Archive naming.** The exact rule for naming each ZIP. What is the slug derived from,
  what characters are allowed, is it lowercase, what separator, is there ever a prefix or
  suffix?
- **The `<slug>-tests` convention.** When do you produce a separate tests archive, what is
  the exact naming rule, and what's inside it versus the main archive?
- What each archive contains: statement, checker, solution(s), tests, generators,
  validators, editorial — which are mandatory, which are optional, and their paths inside
  the ZIP.
- Whether one problem set is one folder or several, and whether anything else lands in
  that folder.
- Whether file/folder names ever contain spaces, non-ASCII, or uppercase.

## Deliverable 2 — `characteristics.md` generation spec

`characteristics.md` is the single artifact that drives *all* post-upload processing
downstream. It has a **General** table (slug, title, difficulty group, subtasks, languages)
plus a numbered **Suggested tags** section that maps positionally onto the General rows.

I'm getting the authoritative format spec from the tool that consumes it, and I'll send it
to you. For now:

- Do you currently emit a `characteristics.md` at all? If yes, show me a real one from a
  recent set, verbatim.
- If not, what metadata do you already have per problem that would populate it, and what
  would you need to start deciding deliberately (difficulty group, tags, target languages)?
- **How do you choose difficulty?** Is it consistent across sets, or does it drift?
  Anything that makes it reproducible matters here.
- **Tags:** do you draw from a fixed vocabulary, or free-text them per problem? Free-text
  will break the downstream assign step.

Then write the spec: given a finished problem set, the deterministic procedure you follow
to produce `characteristics.md`, such that the same set always yields the same file.

## Deliverable 3 — a completion sentinel

Maestro watches a folder. If it starts ingesting while you're still writing files, it will
import a half-finished set.

Propose and adopt a "this set is complete" signal — the simplest good option is a
`MANIFEST.json` written **last**, after every other file is final, containing:

- set name, problem count, and generation timestamp
- one entry per problem: slug, title, archive filename, whether a `-tests` archive exists
- a checksum per archive
- a schema/format version

Maestro will treat the appearance of that file as "safe to ingest", and will cross-check
its contents against both the archives on disk and `characteristics.md`. Any mismatch
halts the run before anything touches Polygon.

## Deliverable 4 — a pre-handoff self-check

Before declaring a set done, run through an explicit checklist and report pass/fail:

- every slug in `characteristics.md` has a matching archive, and vice versa
- slugs are unique within the set, and match the naming rule from Deliverable 1
- every `<slug>-tests` archive has a corresponding base problem
- the tags section has exactly one numbered entry per General row, in the same order
- every required field is populated — no blanks, no `TBD`, no placeholders
- every problem has at least one accepted solution and a checker
- statements are complete: no `TODO`, no unresolved LaTeX, no lorem text

Write this as `PREFLIGHT.md` and actually run it at the end of every set from now on.
Report the checklist result explicitly rather than just saying the set is done.

## Deliverable 5 — export your own instructions

Later, Maestro may run you via the Anthropic API instead of me starting a chat each time.
For that, your project instructions need to become a standalone system prompt.

Produce `SYSTEM_PROMPT.md`: your full authoring instructions, self-contained, written for
a fresh model with no chat history and no project context. Include everything currently
implicit — house style, statement conventions, test-design policy, difficulty calibration,
the output contract above.

Then produce `TOOLS.md`: the capabilities you'd need if you were running headlessly instead
of talking to me — e.g. write a file, run a solution against a test, run a generator,
validate a statement, package a ZIP. For each: name, purpose, what it takes, what it
returns. Just the specification; I'll implement and host them.

*(Implementation note for me, not for you: this becomes an Anthropic Python SDK tool-runner
loop — `client.beta.messages.tool_runner` on `claude-opus-5` with adaptive thinking, which
is that model's default. Not `budget_tokens`, which is removed on this tier.)*

## What to send back

1. The five files above.
2. **A candid list of everything in your current output that is inconsistent, ambiguous, or
   decided ad-hoc per set.** This is the most valuable thing you can give me — every item on
   that list is a pipeline failure I get to prevent instead of debug.
