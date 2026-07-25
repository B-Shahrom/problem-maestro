# The author lane — stages 0–2, and where a model belongs

Maestro today starts at a finished set folder. Everything before that — deciding
what to author, authoring it, and getting a rejected set corrected — is a human
relaying messages between a chat window and a watch directory. This is the design
for closing that, and the reasoning about which parts should be a model at all.

**Recommendation up front:** build the correction loop first (done — see
`maestro/feedback.py`), then the brief; use **Managed Agents** rather than the
Claude API with a sandbox of our own when the authoring step is automated; and do
**not** move Maestro's gate into a model under any of it.

---

## 1. What "talking to the developer" actually decomposes into

Three exchanges, not one. They have different shapes and very different risk.

| # | Exchange | Direction | Today | Hard part |
|---|---|---|---|---|
| 0 | **Brief** — topic, count, difficulty mix, language targets, tag vocabulary | Maestro → author | Operator writes it by hand | Almost none; the contracts already exist as documents |
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

**B. `maestro brief` — next, still no API key.** Render the set brief and the four
contracts into one payload the operator pastes into the Claude project. The
contracts are already written; this is assembly plus a `set.name`, a slug prefix,
a count and a difficulty mix. It removes the step where a human re-explains the
output contract from memory, which is where deliveries drift.

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
