# The author project's instructions, against the gate

The Electicode project's Playbook and Memory, read against `docs/contracts/` and the checks
Maestro actually runs. Source: the two documents exported from the project on 2026-08-28.

This is not a rewrite. The project's instructions are good and most of them are already
reflected in the contracts — the slug rules, inputs-only tests, no validator, binaries
compiled outside the slug folder, the Note-must-not-leak-strategy rule. What follows is only
the delta: what the author, working faithfully from its own instructions, would produce that
the gate rejects, plus the one thing the gate wants that the project already does and simply
does not write down.

Ordered by how badly each breaks.

---

## 1. `MANIFEST.json` is not mentioned. Anywhere. — **blocking**

Both documents, zero occurrences. So an author working from its instructions does not produce
one, and `MANIFEST.json` is Maestro's **completion sentinel**: `ingest.candidates()` looks for
it, and until it appears the folder is reported INCOMPLETE and re-checked forever. Nothing
about the rest of the delivery being perfect changes that.

It is also where every cross-check gets its expected values — the per-archive `sha256` and
`bytes` (M-2), the problem list (M-3, M-4), the limits (M-8, M-15, M-16) and the author's own
preflight result (M-13).

**Fix:** add `MANIFEST_SPEC.md` to the project files and one step to §9: write it last, by
atomic rename, after the preflight passes.

## 2. The delivery is loose files, not a folder — **blocking**

`OUTPUT_CONTRACT.md` §A.1 already says this and calls it "the single biggest structural gap":
a 25-problem batch is 25 loose `.zip` files plus a loose characteristics file in a flat
`/mnt/user-data/outputs/`, in an ephemeral container, surfaced as chat downloads.

Maestro's sweep looks at *immediate subdirectories* of the watch folder. A loose zip is not a
set and is never seen.

Under today's relay this is the operator's job — you make the folder when you download. Any
automated lane has to make the author do it, which means the author needs somewhere durable to
put it. That is the transport question, and it is the only part of this list that is not a
documentation fix.

## 3. Three different language defaults, and one of them collides with the pipeline

| source | default |
|---|---|
| Memory Part I, Statement rules | "Language-only by default (English)" |
| Playbook §2 | "**Default language: English and Russian versions.**" |
| `CHARACTERISTICS_SPEC.md` §1.3 | "default `EN, RU`" |

The Playbook declares itself the winner, so today's effective default is EN + RU.

That collides with the platform. `problem_editor translate` runs EN → `targets` as a
post-upload chore, and `targets` normally includes `ru`. The translate step fills the target
language field whether or not something is already there — so an authored Russian statement is
overwritten by a machine translation of the English one, and **nothing anywhere reports the
loss**. The authored work is simply gone.

Maestro now refuses this at the brief (B-5), but the brief is only consulted if it is used.

**Fix:** make EN the authored default in all three places, and say why — the platform
translates, so a hand-written RU statement is work that is about to be replaced. If a
hand-written RU is wanted for a particular set, that set must not list `ru` in its `targets`.

## 4. `characteristics.md` follows §11, which omits half the columns the gate reads

`CHARACTERISTICS_SPEC.md` §1.1 already documented two incompatible specifications live in the
project — Playbook §11 versus `characteristics-template.md` — and resolved it in favour of the
template. **The project's own instructions still say §11.**

| | Playbook §11 | what the gate reads |
|---|---|---|
| General columns | idx, slug, title, group, tests | + languages, subtasks, checker, TL, ML |
| tags section | absent | required, numbered, positionally aligned |
| checkers section | absent | required |

Two of those are not cosmetic:

- **No tags section** → the chore runner has nothing to apply. A count mismatch between tag
  lines and General rows makes it drop *every* tag in the set and still exit 0 (C-2).
- **No TL/ML columns** → C-7 cannot cross-check the limits against the manifest, which is the
  one place a wrong limit is catchable before it becomes an unexplained TLE weeks later.

**Fix:** replace §11's section list with the template's, or delete §11 and point at
`CHARACTERISTICS_SPEC.md`. Two specs for one file is how the file ended up different per batch.

## 5. The tag vocabulary matches exactly. Its *licence* does not.

Good news first: the template's 42 tags and `manifest.VOCABULARY` are the **same 42 tags**,
character for character, space-separated (`dfs and similar`, `two pointers`,
`string suffix structures`). `test_characteristics_template.py` now pins that. There is no
drift to fix.

What differs is permission. The template says its list is

> "a recommendation, not a hard whitelist — if your platform supports additional tags that fit
> better, use them."

M-9 treats it as **closed** and errors on anything else. The gate is right and the template is
wrong, for a reason the template cannot see: an unknown tag is not rejected by the platform. It
is *created* in the tag store, silently, and then exists for everyone, forever. There is no
undo and no one is notified.

Separately, the Memory's Notion convention — "Topic tags: lowercase-dashed" — is a **different
field on a different system**, and correct there. It only becomes a bug if the dashed form is
written into `characteristics.md`, where `dfs-and-similar` would be a new tag rather than
`dfs and similar`.

**Fix:** change one sentence in the template — the vocabulary is closed; a tag outside it needs
the operator to widen it deliberately. And name the Notion field apart from it, so the dashed
form is never written into the characteristics file.

## 6. The measurement already happens. It is just never written down. — **the cheap one**

This is the item worth reading twice, because it reverses an assumption.

The project **does** compile and run code. Playbook §8 makes verification mandatory: "C++
solution vs. an independent Python reference on every test — zero mismatches." §6 requires
empirical feasibility checks and profiling the worst case. The build environment is real —
`/home/claude/_helpers.py` doing "compile, cross-validate, dedup, zip", binaries to `/tmp/sol`
so they cannot leak into the package.

So the number M-16 wants — the reference solution's measured worst case — is already being
produced. It is simply not recorded anywhere machine-readable. §10's Characteristic Table has
"Suggested Time Limit {x}s" with no measurement behind it, and no `limits_rationale`.

(§10 also says "Suggested Memory Limit {x}s". The unit is MB.)

**Fix:** record the profiling result. `"measured_worst_s": 0.74` and `"limits_rationale":
"TL 2s: reference worst case 0.74s on n=2e5 adversarial input, ~2.7x margin"` in the manifest,
and the TL/ML columns in `characteristics.md`.

That single change turns M-16 from a consistency check into a real one, and it costs the
author nothing it is not already doing.

## 7. Two smaller things, for whoever edits the playbook next

- **§10's Characteristic Table says "Suggested Memory Limit {x}s".** The unit is MB. It is
  obviously a typo and it has presumably never misled anyone, but it sits two lines from the
  time limit and the two are the pair M-8 cross-checks.
- **§8 generates tests with `random.seed()` and no argument**, i.e. deliberately
  irreproducible. Standard contest practice — and testlib's own reasoning, in the generators
  blog — is the opposite: a generator must produce the same test on any platform, which is why
  `rnd` exists instead of `rand()`. This matters less here than it looks, because the tests
  ship as *files* rather than being regenerated on Polygon, so what shipped is what runs. Worth
  knowing rather than worth changing: a set cannot be reproduced from its generator, only from
  its archive.

---

## What this changes about the transport

`author-lane.md` §2 ranked the options on "can the authoring actor run code", assuming a
project chat cannot. **It can** — §6 above is the evidence. That moves the ranking:

- The Project chat and a bare Messages API call are **not** the same capability. The project
  has a sandbox; a Messages call does not.
- The API-path equivalent of what the project does today is **Managed Agents**, which is the
  same conclusion §2 reached, now for a better-evidenced reason.
- The Middleman per-test-timing question (§2.1) drops from *unblocks the author lane* to
  *nice cross-check*. It is still worth having — a judge-hardware measurement beats a
  container one — but it is no longer the thing standing in the way.

Items 1, 3, 4, 5 and 6 are all documentation edits to the project, worth making under **any**
transport including the relay you use today. Item 2 is the only one that needs a decision.
