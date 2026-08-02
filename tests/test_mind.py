"""The mind — a reading of a stopped run, and the three ways it must not lie.

Most of these are about the seam rather than the model. A diagnostician that is
sometimes wrong is fine; this project's whole shape says a wrong *opinion* is
survivable and a wrong *verdict* is not. What is not fine is a mind that is off
and looks like a mind with nothing to say, an unbounded action, or evidence with
a credential in it — and none of those need a live API to test.
"""

from __future__ import annotations

import json

import pytest

from maestro import mind
from maestro.mind import Action, Evidence, Mind, Reading, TransportError, Unavailable
from maestro.model import ProblemSeed, RunStage, RunStatus
from maestro.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "m.db") as s:
        yield s


@pytest.fixture
def run_id(store):
    return store.create_run("edu-arrays", "/sets/edu-arrays",
                            [ProblemSeed(slug="edu-arrays-max", idx=1,
                                         title="Running Max", archive="a.zip")])


ANSWER = {
    "summary": "The chore chain stopped at the division step.",
    "cause": "`division set` exited 1 on an unknown name — see the last log line.",
    "action": "escalate",
    "reason": "The name has to be corrected by a human before anything can retry.",
    "confidence": "high",
    "checks": ["the division names on the run"],
    "unknowns": [],
}


def transport(answer=None, *, raises=None):
    """A fake `Complete`. Records what it was sent so the prompt is inspectable."""
    sent: list[tuple[str, str]] = []

    def complete(system, user):
        sent.append((system, user))
        if raises is not None:
            raise raises
        return answer if answer is not None else dict(ANSWER)

    complete.sent = sent
    return complete


# ------------------------------------------------------ off is not "nothing wrong"


def test_a_mind_with_no_transport_says_why_rather_than_returning_nothing():
    """The whole reason `Unavailable` is a type and not `None`.

    A caller that gets `None` writes "no issues found"; a caller that gets an
    `Unavailable` cannot, because there is nothing in it to print but the reason.
    """
    r = Mind(None, why="$ANTHROPIC_API_KEY is not set").diagnose(Evidence(1, "s", "chores", "failed"))
    assert isinstance(r, Unavailable)
    assert "ANTHROPIC_API_KEY" in r.why


def test_a_transport_error_is_unavailable_and_not_a_reading():
    r = Mind(transport(raises=TransportError("connection reset"))).diagnose(
        Evidence(1, "s", "chores", "failed"))
    assert isinstance(r, Unavailable)
    assert "connection reset" in r.why


def test_a_transport_that_raises_something_unexpected_is_still_only_unavailable():
    """A transport is someone else's code and may raise anything at all.

    It must not take the tick down with it: the pipeline does not depend on the
    mind, so the mind must not be able to stop the pipeline.
    """
    r = Mind(transport(raises=RuntimeError("boom"))).diagnose(
        Evidence(1, "s", "chores", "failed"))
    assert isinstance(r, Unavailable)
    assert "RuntimeError" in r.why


@pytest.mark.parametrize("answer,expect", [
    ("not an object", "not an object"),
    ({**ANSWER, "action": "delete_everything"}, "not one of"),
    ({**ANSWER, "confidence": "quite sure"}, "not a confidence"),
    ({k: v for k, v in ANSWER.items() if k != "cause"}, "missing cause"),
])
def test_a_malformed_answer_is_unavailable_rather_than_an_empty_reading(answer, expect):
    """The API enforces the schema, so this is for the day it does not.

    A `Reading` with blank fields would render as a confident nothing, which is
    the one output shape this module exists to make impossible.
    """
    r = Mind(transport(answer)).diagnose(Evidence(1, "s", "chores", "failed"))
    assert isinstance(r, Unavailable), r
    assert expect in r.why


def test_a_well_formed_answer_becomes_a_reading():
    r = Mind(transport()).diagnose(Evidence(1, "s", "chores", "failed"))
    assert isinstance(r, Reading)
    assert r.action is Action.ESCALATE
    assert r.confidence == "high"
    assert "division" in r.cause


# ------------------------------------------------------------------ permission


def test_nothing_is_allowed_by_default():
    """`mind_may` defaults to `[]`, so a fresh install proposes and never acts."""
    reading = Reading("s", "c", Action.RESUME, "r", "high")
    ok, why = Mind(transport()).may(reading)
    assert not ok
    assert "not in `mind_may`" in why


def test_an_allowed_action_at_high_confidence_is_permitted():
    reading = Reading("s", "c", Action.RESUME, "r", "high")
    ok, why = Mind(transport(), allowed={Action.RESUME}).may(reading)
    assert (ok, why) == (True, "")


@pytest.mark.parametrize("confidence", ["medium", "low"])
def test_confidence_below_high_is_never_acted_on_however_permissive_the_config(confidence):
    """The allowlist is what an operator permits; confidence is whether the mind
    is sure enough to use the permission. Both, never either."""
    reading = Reading("s", "c", Action.RESUME, "r", confidence)
    ok, why = Mind(transport(), allowed=set(Action)).may(reading)
    assert not ok
    assert confidence in why


def test_escalate_is_never_automated_even_when_it_is_allowed():
    """It is the word for "a human is needed"; acting on it would mean the
    opposite of what it says."""
    reading = Reading("s", "c", Action.ESCALATE, "r", "high")
    ok, why = Mind(transport(), allowed=set(Action)).may(reading)
    assert not ok
    assert "human" in why


def test_every_action_the_schema_offers_is_one_the_permission_check_knows():
    """A verb in the enum that `may` does not handle would fall through to
    permitted, which is the wrong direction for a fall-through."""
    for action in Action:
        ok, _ = Mind(transport(), allowed=set(Action)).may(
            Reading("s", "c", action, "r", "high"))
        assert ok is (action in (Action.WAIT, Action.RESUME, Action.APPROVE))


# ---------------------------------------------------------------- from_config


def test_the_config_names_the_variable_and_never_holds_the_key(monkeypatch):
    """Same rule as every other secret here. `config.json` is gitignored, but a
    key in it is still a key in a file that gets copied around."""
    monkeypatch.setenv("MY_KEY", "sk-ant-not-a-real-key")
    m = mind.from_config({"mind": True, "mind_key_env": "MY_KEY", "mind_may": []})
    assert m.on
    assert "MY_KEY" not in json.dumps(mind.SYSTEM)


def test_a_missing_key_switches_the_mind_off_rather_than_failing_to_start(monkeypatch):
    monkeypatch.delenv("NOPE", raising=False)
    m = mind.from_config({"mind": True, "mind_key_env": "NOPE"})
    assert not m.on
    assert "$NOPE" in m.why


def test_mind_off_reports_the_config_key_that_turned_it_off():
    m = mind.from_config({"mind": False})
    assert not m.on
    assert "config.json" in m.why


def test_an_unknown_action_in_the_allowlist_grants_nothing(monkeypatch):
    """Fails closed. `check_warnings` is what makes sure it is not also silent."""
    monkeypatch.setenv("K", "x")
    m = mind.from_config({"mind": True, "mind_key_env": "K",
                          "mind_may": ["resume", "rm -rf"]})
    assert m.allowed == {Action.RESUME}
    assert mind.unknown_actions({"mind_may": ["resume", "rm -rf"]}) == ["rm -rf"]


# ------------------------------------------------------------------- evidence


def test_evidence_is_assembled_from_named_fields_and_a_named_artefact_list(store, run_id, tmp_path):
    store.log(run_id, "error", "division set exited 1")
    work = tmp_path / "runs" / str(run_id)
    (work / "electicode").mkdir(parents=True)
    (work / "electicode" / "audit.json").write_text('{"gaps": 1}', encoding="utf-8")
    # Nothing should reach this: the run directory also holds the packages.
    (work / "secret-notes.txt").write_text("the polygon key is hunter2", encoding="utf-8")

    ev = mind.gather(store, run_id, work, artefacts=mind.ARTEFACTS)
    body = ev.render()
    assert "division set exited 1" in body
    assert '{"gaps": 1}' in body
    assert "hunter2" not in body
    assert "secret-notes" not in body


def test_the_evidence_names_the_batch_s_own_settings_only_once_chosen(store, run_id):
    ev = mind.gather(store, run_id)
    assert "divisions" not in ev.settings          # never chosen — the config applies
    store.set_setting(run_id, "divisions", "Electi")
    assert mind.gather(store, run_id).settings["divisions"] == "Electi"


def test_a_deliberate_empty_setting_is_shown_as_chosen(store, run_id):
    """`""` and "never chosen" are the distinction the whole settings module is
    built on, and a diagnosis of "no division step ran" turns on it."""
    store.set_setting(run_id, "divisions", "")
    assert mind.gather(store, run_id).settings == {"divisions": ""}


def test_the_event_tail_is_bounded(store, run_id):
    for i in range(200):
        store.log(run_id, "info", f"line {i}")
    ev = mind.gather(store, run_id, events=10)
    assert len(ev.events) == 10
    assert "line 199" in ev.events[-1]
    assert "line 0" not in ev.render()


def test_the_same_state_renders_the_same_bytes(store, run_id):
    """No clock, no ordering by dict iteration. A prompt that varies run to run
    cannot be diffed when a reading is wrong, and cannot be cached."""
    a = mind.gather(store, run_id).render()
    b = mind.gather(store, run_id).render()
    assert a == b


def test_gather_raises_on_a_run_that_does_not_exist(store):
    with pytest.raises(KeyError):
        mind.gather(store, 999)


# ------------------------------------------------------------------- scrubbing


@pytest.mark.parametrize("line", [
    "auth failed: api_key=sk-ant-api03-abcdefghijklmnopqrst",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9",
    "cookie: session_id=9f8e7d6c5b4a3210",
    "config says password = hunter2",
])
def test_credential_shaped_text_in_a_log_line_never_leaves_the_machine(store, run_id, line):
    """The second line, not the first — `gather` only reads fields that cannot
    hold a secret. This catches a tool echoing its own configuration into an
    error message, which then lands in the event log as ordinary text."""
    store.log(run_id, "error", line)
    body = mind.gather(store, run_id).render()
    assert mind.REDACTED in body
    for token in ("sk-ant-api03-abcdefghijklmnopqrst", "eyJhbGciOiJIUzI1NiJ9",
                  "9f8e7d6c5b4a3210", "hunter2"):
        assert token not in body


def test_scrubbing_leaves_ordinary_lines_alone():
    """A redactor that eats the log is worse than no redactor: the operator
    stops reading it."""
    line = "upload failed: unexpected exit code 3221225786 (Ctrl-C)"
    assert mind.scrub(line) == line


# --------------------------------------------------------------- what it is sent


def test_the_system_prompt_states_the_rule_the_gate_depends_on():
    """If this sentence is ever dropped, the mind starts being asked to grade
    deliveries — which is the one thing the whole design says it must not do."""
    assert "only a check may accept" in mind.SYSTEM
    assert "never suggest that a failing check be bypassed" in mind.SYSTEM.lower()


def test_the_prompt_describes_exactly_the_actions_the_schema_allows():
    """A verb described but not in the enum is a proposal the parser rejects; a
    verb in the enum but undescribed is one the model picks blind."""
    for action in Action:
        assert f"`{action.value}`" in mind.SYSTEM
    assert set(mind.SCHEMA["properties"]["action"]["enum"]) == {a.value for a in Action}


def test_the_schema_requires_every_field_the_reading_needs():
    """`_read` reports a missing field as unavailable, so a schema that made one
    optional would turn every answer into a non-reading."""
    assert set(mind.SCHEMA["required"]) == set(mind.SCHEMA["properties"])
    assert mind.SCHEMA["additionalProperties"] is False


def test_the_transport_is_sent_the_system_prompt_and_the_rendered_evidence():
    t = transport()
    ev = Evidence(7, "edu-arrays", "chores", "failed", blocked="", events=["boom"])
    Mind(t).diagnose(ev)
    system, user = t.sent[0]
    assert system == mind.SYSTEM
    assert user == ev.render()
    assert "boom" in user


# --------------------------------------------------------------- state stamping


def test_a_run_is_worth_reading_only_once_it_has_stopped(store, run_id):
    run = store.get_run(run_id)
    assert not mind.wants_reading(run)
    store.set_run(run_id, status=RunStatus.FAILED)
    assert mind.wants_reading(store.get_run(run_id))


def test_the_stamp_changes_when_the_run_says_something_new(store, run_id):
    store.set_run(run_id, stage=RunStage.CHORES, status=RunStatus.FAILED)
    run = store.get_run(run_id)
    first = mind.state_stamp(run, 10)
    assert mind.state_stamp(run, 10) == first
    assert mind.state_stamp(run, 11) != first


def test_the_stamp_changes_when_the_block_reason_changes(store, run_id):
    """Two blocks at the same stage with the same last event are still two
    different questions — session expired and awaiting approval have nothing in
    common but where they happened."""
    from maestro.model import BlockReason
    store.block(run_id, BlockReason.SESSION_EXPIRED, "expired")
    a = mind.state_stamp(store.get_run(run_id), 5)
    store.block(run_id, BlockReason.AWAITING_APPROVAL, "gaps")
    assert mind.state_stamp(store.get_run(run_id), 5) != a
