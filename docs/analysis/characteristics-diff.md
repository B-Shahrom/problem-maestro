# `characteristics.md` — the two specs, diffed

*The last major open question. The problem-developer wrote `CHARACTERISTICS_SPEC.md` v1.0 as an
explicit placeholder ("expected to be superseded… the authoritative format is coming from the
consuming tool"). The Scraper documented the real parser in `characteristics.schema.md`, read
from `batch.py:131`. This diffs them.*

**Headline: they are compatible.** The developer guessed the format correctly without having
seen the parser. Every field the parser reads, the developer emits, in a shape the parser
accepts. No format change is required on either side.

That is the good result. The rest of this document is the sharp edges — because the parser is
**lenient and never raises**, every incompatibility in this area fails silently rather than
loudly, and five of them are one typo away.

---

## 1. What the parser reads, and whether the author supplies it

| Parser expects | Author emits | Verdict |
|---|---|---|
| `^#\s+Characteristics\s*[—:-]\s*(.+)$` (optional) | `# Characteristics — {SET NAME}`, em-dash | ✅ |
| `^##\s+General\s*$` — **case-sensitive** | `## General` | ✅ |
| ≥3 pipe-table lines, headers matched by lower-cased name, **order irrelevant** | 10-column GitHub table | ✅ |
| `slug` — **required**; row skipped without it | present | ✅ |
| `title` — gates `list reorder` (needs *every* row non-empty) | present, always | ✅ |
| `group` — lower-cased, `easy/medium/hard` → `Easy/Medium/Hard` | lowercase `easy\|medium\|hard`, only those three | ✅ |
| `subtasks` — none-token → no subtasks; anything else → has subtasks | `none`, or `S0(0), S1(15), …` | ✅ |
| `languages` — captured, **never used** | present | ⚠️ see §2.5 |
| `^##\s+Suggested tags\s*$` (case-**in**sensitive), ends at next heading or `---` | `## Suggested tags`, followed by `---` | ✅ |
| `^\s*\d+\.\s*(.*\S)\s*$`, positional onto General rows | `1. implementation, arrays` | ✅ |
| `[none]` sentinel for an empty tags section | `[none]` convention throughout | ✅ |

Extra columns the author emits — `idx`, `tests`, `checker`, `TL`, `ML` — are **silently
ignored** (headers are looked up by name). Extra sections — `## All slugs`, `## All titles`,
the `Easy`/`Medium`/`Hard` buckets, the per-prefix sections, `## Checkers used`,
`## Notes worth flagging` — are **all ignored**; `_bullets()` exists at `batch.py:71` but is
never called. They are documentation for humans, which is a legitimate purpose, but nothing
downstream consumes them.

---

## 2. The sharp edges

Ordered by how quietly each one fails.

### 2.1 · `idx` and the tag numbers are decorative. Only **file order** binds.

The parser aligns tag line *k* to General data row *k* **by position in the file**. It never
reads the `idx` column, and the regex `^\s*\d+\.\s*(.*\S)\s*$` **captures everything after the
digit without checking the digit**. So `1. / 3. / 2.` aligns 1→row1, 3→row2, 2→row3.

Any edit that reorders rows without moving their tag lines — or renumbers without reordering —
silently pairs the wrong tags to the wrong problems. Combined with the fail-silent `#category`
field (`electicode-fields.md` §2) and the presence-only audit, **that mis-tag is undetectable
at every subsequent stage.**

The author's PF-06 and PF-12 do assert alignment, so this is covered *if the file is generated
and never hand-edited*. Maestro must treat a hand-touched characteristics file as untrusted and
re-derive the pairing from `MANIFEST.json`.

### 2.2 · A tag/row count mismatch drops **all** tags and exits 0

`if len(tags) != len(slugs)` → tags skipped entirely with a warning (`batch.py:195-197`).
Difficulty still applies. The run **exits 0**.

So 25 rows against 24 tag lines yields 25 problems with difficulty set and **zero tags**, reported
as success. Maestro sees exit 0 and proceeds. This is the single most likely way a real batch
silently under-applies, and it is exactly the class of thing the presence-only audit cannot catch
(difficulty is present, so the row looks fine).

Maestro must assert the counts itself before invoking `batch.py` — it cannot learn this from the
exit code.

### 2.3 · `## General` is case-sensitive; `## Suggested tags` is not

`^##\s+General\s*$` has no `re.I` (`batch.py:86`); the tags heading does (`batch.py:57, 120`).

Write `## general` and the table is never found → zero problems → `build_plan` warns "no problems
found in the General table" → **exit 0, nothing happens**. A whole batch no-ops and reports
success.

The author's spec pins `## General`, so this is safe today. It is a one-character cliff with a
silent landing, and worth a Maestro pre-check regardless.

### 2.4 · An unrecognised `group` silently means "no difficulty"

`_DIFF` maps `easy/medium/hard`; anything else yields `""` — no difficulty set, no error
(`batch.py:161`). The cell is lower-cased first, so casing is safe.

This is not hypothetical: `OUTPUT_CONTRACT.md` Appendix A item 9 records **four** difficulty
schemes across past sets, including `Very Easy` and raw Codeforces ratings (`800`–`1900`). Any
of those in a General table produces a silently un-graded problem. The v1.0 contract fixes the
authoring side; Maestro should still reject non-`easy|medium|hard` values rather than trust it.

### 2.5 · `languages` is captured but never used — and the models genuinely differ

`build_plan` reads the column onto the problem dict and does nothing with it. Translate
source/targets come from **CLI flags** (`--targets ru,tg,uz`), which are **global to the batch**.

The authoring contract, meanwhile, treats language as **per problem**: `MANIFEST.json` carries a
`languages` array on every problem, and D-02 makes it authoritative.

**A set with mixed per-problem language sets cannot be expressed in one `batch.py run`.** The
per-problem column is inert; the global flag wins. So Maestro must either:

- verify every problem in the set shares one language set, and pass that as `--targets`; or
- split the batch into one run per distinct language set.

The first is a check; the second is real orchestration logic. Either way, **passing `--targets`
blindly while a set has mixed languages will translate problems into languages they were never
authored for, or skip ones that needed it.** This is the one place where the two models actually
disagree rather than merely differing in emphasis.

### 2.6 · Nothing validates slug shape, duplicates, or existence

Explicitly NOT IMPLEMENTED: slug charset, duplicate slugs, existence on ElectiCode, `languages`
values, required-column presence. Any string passes.

The author's PREFLIGHT covers shape (PF-04) and bijection (PF-03) on their side; Maestro's M-5
covers uniqueness and regex. Existence on ElectiCode is covered by nothing today except stage
6.5's reconcile — which is now the only thing standing between a mistyped slug and a near-miss
row being edited instead.

---

## 3. Resolution

**Layout: the parser's spec governs** — it is the consumer, and the author's spec already
conforms.

**Semantics: the author's spec governs** — the closed vocabulary, the difficulty rubric, the
determinism amendments, `[none]` handling, positional alignment. None of these are enforced by
the parser and none should be; they are authoring discipline, and the parser's leniency is what
makes that discipline load-bearing rather than redundant.

**Validation: Maestro's job, before invoking anything.** The parser will not reject a bad file;
it will quietly do less. The pre-`batch.py` check must cover, at minimum:

1. `## General` spelled exactly, table present, ≥1 data row
2. tag-line count **equals** General row count *(2.2 — highest value)*
3. every `group` ∈ {`easy`,`medium`,`hard`}
4. slug set matches `MANIFEST.json` exactly, both directions
5. tag pairing re-derived from the manifest, not trusted from file order *(2.1)*
6. one language set across the batch, or split the run *(2.5)*

Items 2, 3 and 5 have no failure signal anywhere downstream — no exit code, no audit finding, no
log line. If Maestro does not check them, nothing will.

`batch.py validate --char` (Scraper Phase 2 item 6) should implement 1–3 at the consumer
boundary as a second, independent check. Items 4–6 need the manifest and stay Maestro's.
