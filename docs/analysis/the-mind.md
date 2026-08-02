# The mind — where a model belongs inside the orchestrator

`author-lane.md` covers the model *before* the watch directory: the actor that
writes problems. This is the other one — a model inside Maestro itself, so the
app "has a mind and can deal with problems on its own."

That phrase turns out to be two requests with very different answers, and the
first job was separating them.

---

## 1. Maestro already deals with most problems on its own

Worth saying first, because it changes what is actually missing.

The Middleman's error taxonomy tells Maestro what to retry and what not to;
`polygon.py` layers a decision policy over it with a cap, because `STEP_FAILED`
covers both a transient Polygon blip and a non-compiling `solution.cpp`
indistinguishably. The chore chain resumes at the stage that failed and refuses
to replay the stages that are not idempotent. A killed process is told apart
from a failed one. Ingest tells "still copying" from "invalid" by content rather
than arrival order.

None of that wanted a model, and none of it should get one. It is all
derivable, and derivable beats probable every time.

What is left is the set of stops where the correct action is genuinely **not
derivable from the state** — where a human currently reads a log and forms a
view. Those, and only those, are what a mind is for.

## 2. The two requests

**"Explain what happened."** A parked run's cause is spread across an event log,
a set of findings with check codes, and an artefact or two. Reading all of it at
once and noticing that the timeout at 14:32 and the session warning at 14:29 are
the same event is exactly what a model is good at and no assertion can do. This
is unambiguously useful, costs one request per stop, and cannot break anything —
it produces text.

**"Act without me."** Materially different. Every action worth taking touches
either the run's state or a live platform, and the failure mode is not a wrong
sentence in a log but a write to ElectiCode nobody asked for.

Both are built. They are separate switches, and the second defaults off.

## 3. What the mind may do

Four verbs, and every one of them is something the dashboard already offers a
human:

| | what it does | if the reading behind it is wrong |
|---|---|---|
| `wait` | nothing — this clears on its own | a delay |
| `resume` | makes the run eligible again | one more attempt at a stage that already failed once |
| `approve` | opens the run's own write gate | writes to live ElectiCode against a run a human has not read |
| `escalate` | says a human is needed | nothing; this is the fallback |

Three independent conditions gate every action, and they are separate because
they fail for different reasons:

1. **`mind_may`** — the operator's allowlist, per action. Defaults to `[]`.
   Trusting `resume` says nothing about writing to ElectiCode, so one
   "autonomous" flag would have been the wrong shape.
2. **Confidence** — nothing below `high` is acted on, whatever the allowlist
   says. The allowlist is what an operator permits; confidence is whether the
   mind is sure enough to use the permission.
3. **`escalate` is never automated.** It is the word for "a human is needed";
   acting on it would mean the opposite of what it says.

There is deliberately no verb for *write to Polygon*, *edit a set folder*, or
*mark a problem verified*. Those belong to the pipeline, under its own gates.

### On `approve`

It is the one to think about, and the argument for allowing it is narrower than
it looks: `apply: true` already exists and approves **every** run unread. A mind
allowed to approve is strictly more selective than that — it approves one run,
at high confidence, with a recorded reason. If `apply: true` is acceptable for
an installation, `mind_may: ["approve"]` is a smaller commitment, not a larger
one. `maestro check` says it is on either way.

## 4. What the mind must never do

The rule this project has followed since before there was a model in it:

> a model may propose; only a check may accept.

M-2 compares a sha256. M-4 compares two integers. C-4 compares two sets of
slugs. L-1 compares an authored limit against the platform's. Each is exact,
free, already written, and already tested by mutation. A model asked the same
questions returns a judgement that is right most of the time — and *most of the
time* on a checksum is indistinguishable from not checking it.

So the gate is untouched. `ingest.inspect()` is still the grader. Nothing in
`mind.py` can turn a failing check into a passing one, and the system prompt
says so outright: never suggest a failing check be bypassed, waived or
re-interpreted. If a check failed, the finding is the fact; the mind's job is to
say *why* and what to do about the cause.

A test asserts that sentence is still in the prompt, because the day it is
quietly dropped is the day the mind starts being asked to grade deliveries.

## 5. The failure mode this is mostly built against

Not a wrong diagnosis. A **missing** one that does not look missing.

A stopped run with no reading beside it reads as a run the mind looked at and
had nothing to say about. It is the same shape as `report audit --char` exiting
0 on a check it skipped, and as the division check that skips itself in exactly
the state that most needs it — the two bugs that cost this project the most
work. So:

- no key, mind off, transport failed, model declined, and answer malformed are
  five distinct facts, each of which says which it is
- an unavailable reading is written to the run's log as a warning, not swallowed
- `Unavailable` is its own type rather than `None`, so a caller cannot
  accidentally print "no issues found"
- a malformed answer is *unavailable*, never a `Reading` with empty fields

## 6. Cost, and why it is bounded

A parked run stays parked for hours and the tick comes round every minute. Left
alone, a mind would diagnose the same unchanged run sixty times an hour and say
the same thing each time.

So a reading is taken once per **state stamp** — stage, status, block reason and
the id of the last thing the run said. Anything new earns a fresh reading;
nothing new earns nothing.

The first version of that had a real bug worth recording: the reading is itself
written to the event log, so the stamp taken *before* the call was already stale
when it was stored, and every parked run was re-read on every tick forever. A
stamp that never matches is no stamp at all. It is re-taken after the write now,
and the test that caught it asserts a count, not a behaviour.

## 7. What is not built

- **The mind cannot look anything up.** It reads the evidence Maestro assembles
  and nothing else — no tools, no re-scraping, no Polygon queries. If a reading
  says "I would need X", X appears in `unknowns`, and that list is the input to
  deciding what evidence to start gathering. Giving it tools is a real next
  step; it is also the point at which it stops being unable to break anything,
  so it should follow a few dozen readings, not precede them.
- **It is not the author lane.** Writing problems needs a sandbox that can
  compile and run code — see `author-lane.md`, which lands on Managed Agents for
  exactly that reason. This module is the Claude API, no sandbox, no tools,
  because a diagnosis is pure text and does not need one.
- **It has never run against the live API from here.** The request shape is
  pinned against the real SDK's own signature by `test_mind_roundtrip.py`, which
  is what catches a wrong keyword; whether a given key and model answer usefully
  is the operator's first `maestro diagnose`.
