"""The transport's request, checked against the real SDK's own signature.

The fifth round-trip module, for the same reason as the other four: Maestro's
risk is in its *model* of a system it does not own, and every test in
`test_mind.py` injects a transport, so none of them can see the one call that
actually leaves the machine being wrong.

That call is unusually easy to get wrong. The parameter shapes here have moved
more than once — `output_format` gave way to `output_config.format`, and
`thinking.budget_tokens` is now rejected outright by the models this project
uses. A wrong keyword surfaces as a `TypeError` or a 400 at the exact moment an
operator is trying to find out why a run stopped, which is the worst possible
time for the diagnostician to be the thing that is broken.

Skips when `anthropic` is not installed — it is an optional extra, and a Maestro
without it is a supported install.
"""

from __future__ import annotations

import inspect

import pytest

from maestro import mind

anthropic = pytest.importorskip("anthropic", reason="`pip install 'maestro[mind]'`")


def _params():
    """The keyword parameters `messages.create` really accepts."""
    sig = inspect.signature(anthropic.Anthropic(api_key="x").messages.create)
    return sig.parameters


def test_every_argument_the_transport_sends_is_one_the_sdk_takes():
    sent = {"model", "max_tokens", "system", "thinking", "messages", "output_config"}
    params = _params()
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        # A **kwargs signature would accept anything, including a typo, so it
        # cannot answer the question this test is asking.
        pytest.skip("messages.create takes **kwargs; the signature proves nothing")
    assert sent <= set(params), f"the SDK does not take {sorted(sent - set(params))}"


def test_budget_tokens_is_not_what_this_sends():
    """`thinking={"type": "adaptive"}` — `budget_tokens` is a 400 on Opus 5.

    Pinned as an assertion rather than left to review because it is the single
    most likely thing for a future edit to reintroduce: it was the correct shape
    for years and it is what most code in the wild still shows.
    """
    src = inspect.getsource(mind.anthropic_transport)
    assert "budget_tokens" not in src
    assert '"type": "adaptive"' in src


def test_the_schema_is_sent_the_way_structured_outputs_expects_it():
    src = inspect.getsource(mind.anthropic_transport)
    assert 'output_config={"format": {"type": "json_schema", "schema": SCHEMA}}' in src
    assert "output_format" not in src, "that parameter is the deprecated spelling"


def test_the_model_is_one_the_sdk_will_not_reject_out_of_hand():
    """Not that the model exists — that is the API's answer, not the SDK's — but
    that it is a plain id string. A date-suffixed guess is the usual failure."""
    assert mind.MODEL == "claude-opus-5"
    assert not mind.MODEL[-1].isdigit() or "-20" not in mind.MODEL


def test_an_sdk_error_becomes_a_transport_error_rather_than_escaping():
    """The tick must survive anything the SDK raises, including at construction.

    Driven through the real client with a key it will reject, so the exception
    is the SDK's own rather than one this test invented.
    """
    complete = mind.anthropic_transport("not-a-key")
    with pytest.raises(mind.TransportError):
        complete("system", "user")


def test_a_missing_package_is_reported_as_something_to_install(monkeypatch):
    """The optional-extra path. Simulated by hiding the module, because the
    interesting case is a Maestro installed without it."""
    import builtins
    real = builtins.__import__

    def hide(name, *a, **kw):
        if name == "anthropic":
            raise ImportError("no module named anthropic")
        return real(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", hide)
    with pytest.raises(mind.TransportError, match=r"maestro\[mind\]"):
        mind.anthropic_transport("k")("system", "user")
