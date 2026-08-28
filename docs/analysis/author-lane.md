# The author lane — stages 0–2, and where a model belongs

Maestro today starts at a finished set folder. Everything before that — deciding
what to author, authoring it, and getting a rejected set corrected — is a human
relaying messages between a chat window and a watch directory. This is the design
for closing that, and the reasoning about which parts should be a model at all.

**Recommendation up front:** build the correction loop first (done —
`maestro/feedback.py`), then the brief (done — `maestro/brief.py`); use **Managed
Agents** rather than the Claude API with a sandbox of our own when the authoring
step is automated; and do **not** move Maestro's gate into a model under any of
it.

Both built pieces work today with the human relay and need no API key, which is
the point of the ordering — the exchange is closed by hand before it is closed by
a model, so the model is replacing a working loop rather than defining one.

---

## 0. Who "the author" is

The **Problem-developer** actor — today a Claude project, reached through a chat window. Not
the operator. The operator briefs it, carries its output into the watch directory, carries
rejections back, and approves what Maestro does with the result; every exchange below is
Maestro ↔ *author*, with the operator currently standing in the middle of each one as a
courier. What §3 automates is that courier role. The operator keeps the brief and keeps the
approval gates under every option here.

## 1. What "talking to the developer" actually decomposes into

Three exchanges, not one. They have different shapes and very different risk.

| # | Exchange | Direction | Today | Hard part |
|---|---|---|---|---|
| 0 | **Brief** — topic, count, difficulty mix, language targets, tag vocabulary | Maestro → author | `maestro brief` | Almost none; the contracts already exist as documents |
| 1 | **Delivery** — statements, checkers, solutions, tests, editorial, `characteristics.md`, `MANIFEST.json` | author → Maestro | Human downloads archives out of a chat and drops them in `watch_dir` | Authoring requires **running code** |
| 2 | **Correction** — a rejected set, and what to change | Maestro → author | One line in a tick report; the author never sees it | Turning an observation into an instruction |

Exchange 2 was the cheapest and the most obviously broken, so it is the one that
now exists. `feedback.py` writes a correction request beside every rejected
folder: each finding paired with the contract clause it breaks, what it would
have broken downstream, and what to change — plus an explicit list of the checks
that **did not run**, because two of the three families are gated behind a clean
manifest and a report that omits that reads as a complete account when it is not.

It is useful immediately with the human relay, and it is the exact payload
exchange 2 would send over an API later. That is why it was built first: it is
the piece that is correct under every architecture below.

---

## 2. Managed Agents vs. Claude API + our own sandbox

The decision hinges on one fact about the authoring work: **it cannot be done
without executing code.**

`CHARACTERISTICS_SPEC.md` §5 requires the time limit to be justified from a
measured run — its own worked example reads *"TL 2 s: reference worst case 0.81 s
on the slowest sample, 2.5× margin"*. `PREFLIGHT.md` requires the reference
solution to pass every test before delivery. Neither is expressible as text
generation. A model that writes `0.81 s` without having run anything produces a
number that is plausible, unverifiable, and wrong in exactly the way the rest of
this project is built to catch.

So the authoring actor needs a filesystem, a compiler and a shell. That is the
whole comparison:

| | **Managed Agents** | **Claude API + Tool Runner** |
|---|---|---|
| Agent loop | Anthropic hosts it | SDK helper, we host |
| Sandbox for `g++`, `run tests`, file writes | **Included** — per-session container with bash, file ops, code execution | We build it: container image, resource limits, timeouts, cleanup |
| Running author-generated code | Anthropic's isolation boundary | Our isolation boundary, on our host, next to the Polygon and ElectiCode credentials |
| Session state across a correction round | Per-session container persists | We persist it |
| Scheduled/unattended authoring | Deployments | We schedule it |
| What we own | Agent config + the gate | A sandbox subsystem + the gate |

The second column is a subsystem, and it is the one part of this pipeline with a
real security surface — arbitrary generated C++ compiled and executed on the
machine that holds the Polygon key and the ElectiCode session cookie. Managed
Agents removes that from our surface entirely. Everything else in the table is
convenience; that row is not.

**Verdict: Managed Agents**, model `claude-opus-5`, when exchange 1 is automated.
The Claude API alone would be the right answer if the authoring step were pure
text — it is not.

### 2.1 The caveat that changes stage 4

A sandbox measurement is a measurement *of the sandbox*. Wall-clock in a shared
cloud container under unknown contention does not transfer to the judge, so a TL
derived from it is a guess with a decimal point on it.

The authoritative measurement already exists inside the pipeline: Polygon runs
the reference solution against every test during `buildPackage(verify=true)`, on
the judge's own hardware, at stage 4. If the Middleman can surface those
per-test timings, Maestro can check the authored TL against the machine that will
actually enforce it — which is strictly better than any number the author could
produce, sandboxed or not.

That is a question for the Middleman, not an assumption: **does the verify result
carry per-test execution times, and can `/api/` expose them?** It belongs in
`FROM_MAESTRO.md` before any of §3 is built. If the answer is yes, the author's
measured runtime stops being the source of truth and becomes a sanity check
against it, and the sandbox's imprecision stops mattering.

---

## 2.2 Four transports, and the one question that ranks them

The operator proposed a fifth option not in the table above: drive the **existing
claude.ai Project** in a browser — new chat, paste the brief, collect the
archives — the same Playwright technique the Platform Scraper uses on
ElectiCode. It is worth setting beside the others rather than dismissed, because
the thing it protects is real: that project carries instructions and accumulated
memory that no fresh API call has.

| | who authors | can compile & run code | transport | supported |
|---|---|---|---|---|
| **A. Today** | the Claude project | no | the operator pastes and drops | — |
| **B. Browser automation** | the same project | no | Playwright on claude.ai | no — scripted access to the web UI is outside what the product supports, and it risks the account it runs as |
| **C. API + extracted instructions** | a Messages API call carrying those instructions | no | HTTP | yes |
| **D. Managed Agents** | an agent with a per-session sandbox | **yes** | HTTP | yes |

B and C are the **same capability**. A Project chat has no compiler; neither does
a Messages call. The difference between them is only how the request travels, and
C's route is supported, faster, and not one DOM change away from breaking. What
makes B look better is the instructions and the memory — and instructions are
text. Extracting them into `docs/contracts/` is already question 1 of §5, it is
needed under B, C and D alike, and doing it turns C into a strict improvement on
B rather than a downgrade.

### The measurement gap that B does not close

A and B share a hole that is easy to miss because nothing reports it.
`CHARACTERISTICS_SPEC.md` §5 requires the time limit to be justified from a
*measured* run, and `PREFLIGHT.md` requires the reference solution to pass every
test before delivery. Neither is possible in a chat window. So under A and B,
`measured_worst_s` is either produced by the operator running the tests, or it is
asserted.

Maestro's M-16 checks that the measurement is *consistent* — measured worst case
inside the limit, with margin. It cannot tell a measured number from an invented
one, and no check downstream can either: a wrong limit passes import, build,
verify, upload and audit, and surfaces weeks later as a TLE on a correct
submission. Automating the transport does not touch this. It makes the same
unverified number arrive faster.

### The cheap fix that changes the ranking

**Polygon already does this measurement, on the right hardware.**
`buildPackage(verify=true)` runs the reference solution against every test at
stage 4, on the judge's own machine — strictly better than any sandbox number,
which is measured under unknown contention in a shared container.

So the question in §2.1 is not a detail of option D; it is the thing that decides
how much the transport matters. If the Middleman can surface per-test execution
times, Maestro checks the authored TL against the machine that will enforce it,
the author's own measurement drops from *source of truth* to *sanity check*, and
"the authoring actor must be able to run code" stops being the constraint that
ranks these four. That is one question to one dev, and it is worth asking before
building any transport.

---

## 3. What must not become a model

The temptation with an Outcomes rubric is to let it grade the delivery. It must
not, and the reason is not caution — it is that the deterministic checks are
**better at this specific job**.

M-2 compares a sha256. M-4 compares two integers. C-4 compares two sets of
slugs. P-3 compares a declared test count against the importer's own count. Each
is exact, each is free, and each is already written. A rubric grader asked the
same questions returns a judgement that is right most of the time — and "most of
the time" on a checksum is indistinguishable from not checking it.

So the seam is:

- **Maestro's gate stays deterministic and stays blocking.** `ingest.inspect()`
  is the grader. A set is accepted because it passes M/C/P, never because a model
  thought it looked fine.
- **A rubric, if added, is advisory and covers what checks cannot express** —
  statement clarity, whether the editorial explains the intended solution, whether
  the tests actually cover the stated edge cases. Those are real quality
  questions that no assertion can reach, and they should produce warnings in the
  correction request, not verdicts.

The general rule this project has already been following, stated plainly: *a
model may propose; only a check may accept.*

---

## 4. Build order

Each step is useful on its own and none of them requires the next.

**A. Correction requests — done.** `maestro/feedback.py`, wired into the sweep
and into `maestro inspect --report`. Works with the human relay, needs no API key.

**B. `maestro brief` — done.** `maestro/brief.py`, still no API key. Renders the
per-set instruction from the constants the gate actually enforces — `VOCABULARY`,
`SLUG_RE` and `SUPPORTED_SCHEMA` are read from `manifest.py`, not retyped, so the
brief and the gate are physically the same fact and a test pins that. It refuses
a brief whose set name is already taken (`set_name` is UNIQUE, so authoring
against one delivers into silence) or whose slug prefix cannot begin a legal
slug. `--with-contracts` inlines the four documents verbatim for a session that
lacks them, and announces any it could not read rather than shipping three of
four quietly.

It does **not** summarise the contracts. A summary of a contract is a second copy
of it, and the two diverge — the same reasoning that makes the vocabulary
generated rather than written.

**C. Managed Agents session.** Agent config = the contracts as its system prompt;
sandbox writes the set; Maestro pulls it into `watch_dir` and the existing gate
takes over unchanged. A rejection posts `feedback.render()` back into the same
session as the next turn, and the container still holds the working files, so the
correction round is a genuine continuation rather than a re-explanation.

**D. Advisory rubric.** Only after C has run a few sets, and only for the
qualities checks cannot reach. Warnings, never a verdict.

The gate does not change in any of these. That is the point of the ordering: the
part being automated is the part that produces candidates, and the part that
accepts them is the part that already works.

---

## 5. Open questions for the operator

1. **Does the Claude-project Problem-developer stay?** C replaces it, and its
   authoring conventions live in that project's own instructions, some of which
   are not in `docs/contracts/`. Those need extracting first or the agent starts
   worse than what it replaces.
2. **Budget.** A set of five problems with real test generation and a correction
   round is a long session, and B costs nothing while C costs per set. Worth
   knowing the ceiling before C, not after.
3. **Where does the key live?** Same answer as every other secret here — local,
   gitignored, never in the repo. Managed Agents also means archives leave the
   machine, which the Polygon and ElectiCode legs already do, but it is a
   deliberate choice rather than an implied one.
