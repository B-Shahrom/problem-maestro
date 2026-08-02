"""A reading of a stopped run — the one place a model belongs in this half.

Maestro already deals with most problems on its own. The Middleman's error
taxonomy tells it what to retry, the idempotency rules tell it what may be
replayed, and every stage that could write parks itself rather than guess. What
it cannot do is the thing a human does when a run parks: *look at everything at
once* — the event log, the findings and their check codes, the artefacts the
stage wrote — and say what probably happened.

That is the job here, and it is deliberately the whole job. The rule this
project has followed throughout still holds, and holds hardest at the point
where a model enters:

    a model may propose; only a check may accept.

So the mind never accepts anything. It reads evidence Maestro already gathered
and returns a `Reading`: what it thinks happened, what to look at first, and
which of four bounded actions it would take. Nothing here writes to Polygon or
ElectiCode, nothing here marks a problem good, and nothing here can turn a
failing check into a passing one. The gate is unchanged and stays deterministic.

Three properties make that safe rather than merely stated:

**The action vocabulary is closed and small.** Four verbs, and only the ones an
operator has explicitly allowed are ever taken. The default is `[]` — propose
only. `escalate` is always available and the prompt says so, because a model
that must pick an action will always pick one, and "I don't know" has to be
expressible or it will be expressed as a guess.

**Unavailability is loud.** No key, no SDK, a transport error, a refusal and a
malformed answer are five different facts and each says which. A run with no
reading must never look like a run the mind found nothing wrong with — that is
the same fail-silent shape as a skipped check reporting clean, and it is the one
this codebase spends most of its tests on.

**The transport is injected.** Like `polygon.Parser` and `scraper.Runner`, so
every test here runs without a key and without a network. The default transport
is the official SDK, imported lazily so `anthropic` stays an optional extra and
the pipeline keeps working — mind off, and saying so — when it is absent.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .model import Run, RunStatus

#: Default model. Opus rather than a smaller one because the input is a wall of
#: heterogeneous evidence and the useful output is a *diagnosis* — the failure
#: mode of a weaker read here is a confident wrong cause, which costs an
#: operator more than no reading at all.
MODEL = "claude-opus-5"

#: Enough for a diagnosis plus adaptive thinking. The answer itself is a small
#: JSON object; the headroom is for the reasoning that produces it.
MAX_TOKENS = 4096

#: How many of the run's most recent events to show. The tail is what matters —
#: a stopped run's cause is almost always in the last few lines — and an
#: unbounded log would be mostly upload progress.
EVENT_TAIL = 60

#: How much of an artefact to include. Artefacts are JSON dumps of whole
#: catalogs; the interesting part is the shape and the first few records.
ARTEFACT_CAP = 4000

#: What a stopped run's directory is read for, by name. Named rather than
#: globbed, and this is the security boundary rather than `scrub` below: the run
#: directory also holds the extracted packages and the shaped upload tree, and a
#: prompt has no business anywhere near those.
ARTEFACTS = ("electicode/audit.json", "electicode/upload.json",
             "electicode/chores.json", "electicode/list-rows.json")


class Action(StrEnum):
    """What the mind would do. Closed, and every member is reversible.

    Deliberately missing: anything that writes to Polygon or ElectiCode
    directly, anything that edits a set folder, and anything that marks a
    problem verified. Those are the pipeline's own to do, under its own gates.
    """

    WAIT = "wait"
    """Nothing to do — this clears on its own, or the next tick handles it."""

    RESUME = "resume"
    """Make the run eligible again. Safe by construction: resume retries nothing
    by itself, and a stage that cannot be replayed still refuses."""

    APPROVE = "approve"
    """Let the run's writes through. The one action that touches live services,
    and the only one an operator should think twice about allowing — though it
    is strictly *less* permissive than `apply: true`, which approves everything
    unread and already exists as a switch."""

    ESCALATE = "escalate"
    """A human is needed. Always available, never wrong to choose."""


#: What each action costs when the reading behind it is wrong. Shown to the
#: operator on the `--allow` prompt and included in the system prompt, because a
#: model choosing between actions should know what it is spending.
COST = {
    Action.WAIT: "a delay, if the run actually needed attention",
    Action.RESUME: "one more attempt at a stage that already failed once",
    Action.APPROVE: "writes to live ElectiCode against a run a human has not read",
    Action.ESCALATE: "nothing — this is the fallback",
}

#: Confidence below which no action is ever taken, whatever the allowlist says.
#: A low-confidence proposal is a question, not an instruction.
ACTIONABLE = ("high",)

_ACTION_VALUES = {a.value for a in Action}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string",
                    "description": "One sentence: what state this run is in."},
        "cause": {"type": "string",
                  "description": "The most likely cause, and what in the evidence "
                                 "says so. Name the specific line or check code."},
        "action": {"type": "string", "enum": [a.value for a in Action]},
        "reason": {"type": "string",
                   "description": "Why that action, in one or two sentences."},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "checks": {"type": "array", "items": {"type": "string"},
                   "description": "What a human should look at first, most "
                                  "useful first. Empty if there is nothing to look at."},
        "unknowns": {"type": "array", "items": {"type": "string"},
                     "description": "Evidence you would need and do not have. "
                                    "Empty only if genuinely nothing is missing."},
    },
    "required": ["summary", "cause", "action", "reason", "confidence",
                 "checks", "unknowns"],
    "additionalProperties": False,
}

SYSTEM = f"""\
You are the diagnostic half of Maestro, an orchestrator for a competitive-programming
problem pipeline. A run has stopped and an operator wants to know why.

Maestro sequences three actors and owns no domain logic of its own:

  * a **Polygon Middleman** (HTTP, :8000) — imports each problem's archive, builds
    and verifies the package, returns it for download
  * a **Platform Scraper** (Playwright CLIs) — uploads packages to ElectiCode and
    then does post-upload chores: fixmdx, translate, metadata, custom, limits,
    division access, contest-list add and reorder
  * a **problem-developer** — authors the set that arrives in the watch directory

Runs advance through stages: ingest → polygon → upload → reconcile → chores →
audit → done. The Polygon half fans out per problem; the ElectiCode half is
strictly one batch at a time.

Findings carry check codes, and the code tells you which family a failure is in:
M-* manifest cross-checks, C-* characteristics, P-* importer pre-flight, L-*
limits landed on the platform, D-* division access landed, B-* the authoring
brief, LI-*/LM-* contest-list membership.

## What you are for

Read the evidence and say what happened. You are good at the thing the
deterministic checks cannot do: reading a whole log at once and noticing that
the timeout at 14:32 and the session warning at 14:29 are the same event.

## What you are not for

You do not decide whether a set is acceptable. Every check that grades a
delivery is exact, free and already written — M-2 compares a sha256, C-4
compares two sets of slugs. Your judgement on those questions would be right
most of the time, and "most of the time" on a checksum is indistinguishable from
not checking it. The rule is: *a model may propose; only a check may accept.*

So never suggest that a failing check be bypassed, waived, or re-interpreted. If
a check failed, the finding is the fact. Your job is to say *why* it failed and
what to do about the cause.

## Choosing an action

{chr(10).join(f"  * `{a.value}` — {COST[a]}" for a in Action)}

`escalate` is always available and is never the wrong answer. Choose it whenever
the evidence does not actually determine what to do. Do not reach for a
plausible action to avoid saying you are unsure — an operator can act on "I
don't know, look at X"; they cannot recover from a confident wrong cause.

Set `confidence` to `high` only when the evidence names the cause outright.
Something an operator has to verify before acting on is `medium` at best.

Put anything you would have wanted to see in `unknowns`. That list is read: it
is how Maestro learns which evidence to start gathering.
"""

#: Fragments that mean the surrounding text should not leave the machine.
#: Evidence is assembled from named fields, not scraped, so this is a second
#: line rather than the first — but event messages carry tool output, and tool
#: output is the one thing here nobody in this repo wrote.
_SECRETS = re.compile(
    r"""(?ix)
    ( (api[_-]?key | apikey | secret | password | passwd | token | cookie
        | authorization | session[_-]?id ) \s* [:=] \s* (?: bearer \s+ )? \S+
    # `Authorization: Bearer <token>` on its own: the keyword branch above stops
    # at the scheme word and leaves the token in the clear, which is how this
    # arrived — the parametrised test caught the first version doing exactly that.
    | \b bearer \s+ [A-Za-z0-9._~+/=-]{12,}
    | \b[A-Za-z0-9_-]{4}-ant-[A-Za-z0-9_-]{16,}
    )""")

REDACTED = "[redacted]"


def scrub(text: str) -> str:
    """Blank anything credential-shaped.

    Not a security boundary — the evidence builder below is, by only reading
    fields that cannot hold a secret. This catches the case that boundary cannot
    see: a Scraper or Middleman line that echoes its own configuration into an
    error message, which then lands in the event log as ordinary text.
    """
    return _SECRETS.sub(REDACTED, text)


@dataclass(frozen=True, slots=True)
class Evidence:
    """Everything the mind is shown, assembled from what Maestro already has.

    A dataclass rather than a formatted string so the same evidence can be
    rendered for a prompt, printed for an operator, and compared in a test —
    and so that what is sent is a reviewable list of fields rather than
    whatever happened to be in scope.
    """

    run_id: int
    set_name: str
    stage: str
    status: str
    blocked: str = ""
    error: str = ""
    settings: dict[str, str] = field(default_factory=dict)
    problems: list[tuple[str, str, str]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    artefacts: dict[str, str] = field(default_factory=dict)

    def render(self) -> str:
        """The prompt body. Deterministic — same run state, same bytes."""
        out = [f"# Run {self.run_id} — {self.set_name}",
               "",
               f"stage: {self.stage}",
               f"status: {self.status}"]
        if self.blocked:
            out.append(f"blocked: {self.blocked}")
        if self.error:
            out.append(f"error: {self.error}")
        if self.settings:
            out += ["", "## This batch's own settings"]
            out += [f"- {k}: {v}" for k, v in sorted(self.settings.items())]
        if self.problems:
            out += ["", "## Problems"]
            out += [f"- {slug}: {stage} / {status}"
                    for slug, stage, status in self.problems]
        out += ["", f"## Event log (last {len(self.events)})"]
        out += self.events or ["(no events)"]
        for name, body in sorted(self.artefacts.items()):
            out += ["", f"## Artefact: {name}", "```", body, "```"]
        return scrub("\n".join(out))


@dataclass(frozen=True, slots=True)
class Reading:
    """What the mind made of it. Advisory, always."""

    summary: str
    cause: str
    action: Action
    reason: str
    confidence: str
    checks: list[str] = field(default_factory=list)
    unknowns: list[str] = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        """Whether this reading is firm enough to act on at all.

        Separate from the allowlist on purpose: the allowlist says what an
        operator permits, this says whether the mind is sure enough to use the
        permission. Both have to agree.
        """
        return self.confidence in ACTIONABLE and self.action is not Action.ESCALATE

    def render(self) -> str:
        out = [self.summary, "", f"cause: {self.cause}",
               f"would: {self.action.value} — {self.reason}",
               f"confidence: {self.confidence}"]
        if self.checks:
            out += ["", "look at:"] + [f"  - {c}" for c in self.checks]
        if self.unknowns:
            out += ["", "wanted and did not have:"] + [f"  - {u}" for u in self.unknowns]
        return "\n".join(out)


@dataclass(frozen=True, slots=True)
class Unavailable:
    """No reading, and *why* there is no reading.

    Its own type rather than `None` so a caller cannot accidentally treat "the
    mind is switched off" as "the mind had nothing to say". Those are the two
    facts this whole module is arranged to keep apart.
    """

    why: str

    def render(self) -> str:
        return f"no reading: {self.why}"


class TransportError(Exception):
    """The transport could not produce an answer. Carries the operator-facing why."""


#: `(system, user) -> the parsed object`. Parsing belongs to the transport
#: because the API can enforce the schema and a string-returning transport would
#: put a second parser here that nothing enforces.
Complete = Callable[[str, str], dict[str, Any]]


def gather(store, run_id: int, run_dir: Path | None = None, *,
           events: int = EVENT_TAIL, artefacts: tuple[str, ...] = ()) -> Evidence:
    """Assemble the evidence for one run from the store and its artefacts.

    Field by field, deliberately. The alternative — handing over the run
    directory and letting the model look — would put `config.json`, the session
    file and the archives themselves within reach of a prompt, and none of those
    are needed to explain why a stage stopped.
    """
    run: Run | None = store.get_run(run_id)
    if run is None:
        raise KeyError(f"no run {run_id}")

    rows = store.events(run_id, limit=10_000)
    tail = rows[-events:] if events else rows
    log = [f"{r['at']}  {r['level']}"
           + (f"  [{r['slug']}]" if r["slug"] else "")
           + f"  {r['message']}" for r in tail]

    chosen = {k: v for k in ("divisions", "targets", "list_url")
              if (v := getattr(run, k, None)) is not None}

    excerpts: dict[str, str] = {}
    for name in artefacts:
        path = (run_dir / name) if run_dir else None
        if path is None or not path.is_file():
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        if len(body) > ARTEFACT_CAP:
            body = body[:ARTEFACT_CAP] + f"\n… truncated at {ARTEFACT_CAP} chars"
        excerpts[name] = body

    return Evidence(
        run_id=run.id,
        set_name=run.set_name,
        stage=str(run.stage),
        status=str(run.status),
        blocked=str(run.block_reason or ""),
        error=str(run.error or ""),
        settings=chosen,
        problems=[(p.slug, str(p.stage), str(p.status))
                  for p in store.problems(run_id)],
        events=log,
        artefacts=excerpts,
    )


def wants_reading(run: Run) -> bool:
    """Is this a run a diagnosis would be about?

    Only the stopped ones. A running run has nothing to explain yet, and
    diagnosing every run on every tick would spend a request per run per minute
    on the answer "it is working".
    """
    return run.status in (RunStatus.BLOCKED, RunStatus.FAILED)


def state_stamp(run: Run, last_event_id: int) -> str:
    """What has to change before a run is worth re-reading.

    A parked run is parked for hours, and the tick that finds it parked comes
    round every minute. Without this the mind would re-diagnose the same
    unchanged state sixty times an hour and say the same thing each time.
    """
    return f"{run.stage}/{run.status}/{run.block_reason or ''}/{last_event_id}"


def anthropic_transport(api_key: str, *, model: str = MODEL,
                        max_tokens: int = MAX_TOKENS) -> Complete:
    """The real transport: the official SDK, imported when first used.

    Lazy because `anthropic` is an optional extra — a Maestro installed without
    it must still run the pipeline, and must say the mind is off rather than
    fail to start.
    """

    def complete(system: str, user: str) -> dict[str, Any]:
        try:
            import anthropic
        except ImportError as e:  # pragma: no cover — exercised by the absent-SDK test
            raise TransportError(
                "the `anthropic` package is not installed — `pip install "
                "'maestro[mind]'`, or leave `mind` off") from e

        client = anthropic.Anthropic(api_key=api_key)
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                # The evidence is heterogeneous and the useful answer is a
                # diagnosis over all of it at once, which is what thinking is for.
                thinking={"type": "adaptive"},
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
            )
        except Exception as e:  # noqa: BLE001 — any SDK or transport failure
            raise TransportError(f"{type(e).__name__}: {e}") from e

        if response.stop_reason == "refusal":
            raise TransportError("the model declined to answer")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise TransportError(f"no text in the response (stop_reason="
                                 f"{response.stop_reason!r})")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise TransportError(f"the response was not JSON: {e}") from e

    return complete


def from_config(cfg: dict) -> Mind:
    """Build the mind an installation asked for, or a switched-off one.

    Never raises. A missing key is a mind that says why it is off, on every
    reading, rather than a Maestro that will not start — the pipeline's
    correctness does not depend on any of this and its availability must not
    either.
    """
    if not cfg.get("mind"):
        return Mind(None, why="`mind` is false in config.json")

    env = cfg.get("mind_key_env") or "ANTHROPIC_API_KEY"
    key = os.environ.get(env, "")
    if not key:
        # The config names the *variable*, never the value — same rule as every
        # other secret here, and it is why `config.json` can name it at all.
        return Mind(None, why=f"${env} is not set")

    # An unrecognised action grants nothing rather than raising — a typo in
    # `mind_may` must fail closed. `check_warnings` is what says it is there.
    allowed = {Action(a) for a in cfg.get("mind_may") or [] if a in _ACTION_VALUES}
    return Mind(anthropic_transport(key, model=cfg.get("mind_model") or MODEL),
                allowed=allowed)


def unknown_actions(cfg: dict) -> list[str]:
    """Anything in `mind_may` that is not an action. Reported, never guessed at."""
    return sorted(set(cfg.get("mind_may") or []) - _ACTION_VALUES)


class Mind:
    """The diagnostician. Holds no state and decides nothing on its own."""

    def __init__(self, complete: Complete | None, *,
                 allowed: set[Action] | None = None,
                 why: str = "no transport configured") -> None:
        self.complete = complete
        self.allowed = allowed or set()
        self._why = why

    @property
    def on(self) -> bool:
        return self.complete is not None

    @property
    def why(self) -> str:
        """Why there would be no reading. Empty when the mind is on."""
        return "" if self.on else self._why

    def diagnose(self, evidence: Evidence) -> Reading | Unavailable:
        """One reading, or one reason there is none."""
        if self.complete is None:
            return Unavailable(self._why)
        try:
            raw = self.complete(SYSTEM, evidence.render())
        except TransportError as e:
            return Unavailable(str(e))
        except Exception as e:  # noqa: BLE001 — a transport is someone else's code
            return Unavailable(f"the transport raised {type(e).__name__}: {e}")
        return self._read(raw)

    @staticmethod
    def _read(raw: Any) -> Reading | Unavailable:
        """Validate the answer rather than trust the schema was honoured.

        The API enforces `SCHEMA`, so this should never fire against the real
        transport. It is here for the injected ones and for the day the schema
        and this dataclass drift — a malformed reading must be *unavailable*,
        not a `Reading` with empty fields that reads as a confident nothing.
        """
        if not isinstance(raw, dict):
            return Unavailable(f"the reading was {type(raw).__name__}, not an object")
        missing = [k for k in SCHEMA["required"] if k not in raw]
        if missing:
            return Unavailable(f"the reading is missing {', '.join(missing)}")
        try:
            action = Action(raw["action"])
        except ValueError:
            return Unavailable(f"{raw['action']!r} is not one of "
                               f"{', '.join(a.value for a in Action)}")
        if raw["confidence"] not in ("high", "medium", "low"):
            return Unavailable(f"{raw['confidence']!r} is not a confidence")
        return Reading(
            summary=str(raw["summary"]).strip(),
            cause=str(raw["cause"]).strip(),
            action=action,
            reason=str(raw["reason"]).strip(),
            confidence=raw["confidence"],
            checks=[str(c) for c in raw["checks"] or []],
            unknowns=[str(u) for u in raw["unknowns"] or []],
        )

    def may(self, reading: Reading) -> tuple[bool, str]:
        """`(take it, why not)` — the only place a proposal becomes permission.

        Three conditions, and all three have to hold. They are separate because
        they fail for different reasons and an operator needs to know which:
        the action has to be allowed, the mind has to be sure, and `escalate`
        is never something to do automatically since it *means* a human.
        """
        if reading.action is Action.ESCALATE:
            return False, "escalate means a human, which is not something to automate"
        if reading.action not in self.allowed:
            allowed = ", ".join(sorted(a.value for a in self.allowed)) or "nothing"
            return False, (f"`{reading.action.value}` is not in `mind_may` "
                           f"(currently: {allowed})")
        if reading.confidence not in ACTIONABLE:
            return False, (f"confidence is {reading.confidence}, and only "
                           f"{'/'.join(ACTIONABLE)} is acted on")
        return True, ""
