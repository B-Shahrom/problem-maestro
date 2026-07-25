import json

import pytest

from maestro.polygon import (
    RETRY_CAP, STEP_FAILED_RETRY_CAP, Action, PolygonClient, PolygonError, decide,
    problem_decisions,
)


# ------------------------------------------------------------------ policy

def test_already_verified_is_success_not_failure():
    """The C-2 finding: a FAILED that means success must not halt the batch."""
    d = decide(client_action="success", error_code="IMPORTED_ALREADY_VERIFIED")
    assert d.action is Action.SUCCESS


def test_lost_job_resubmits_rather_than_halting():
    """The Middleman's job registry is in-memory; its restart must not fail our run."""
    d = decide(http_status=404, detail="Unknown jobId: abc123")
    assert d.action is Action.RESUBMIT
    assert "idempotent" in d.reason


def test_download_before_ready_waits():
    d = decide(http_status=404, detail="No READY package for this problem yet.")
    assert d.action is Action.WAIT


def test_step_failed_retries_then_gives_up():
    early = decide(client_action="retry", error_code="STEP_FAILED", attempts=1)
    assert early.action is Action.RETRY
    late = decide(client_action="retry", error_code="STEP_FAILED", attempts=STEP_FAILED_RETRY_CAP)
    assert late.action is Action.HALT
    assert "content error" in late.reason


def test_verify_states_map_through():
    assert decide(client_action="wait", error_code="VERIFY_RUNNING").action is Action.WAIT
    assert decide(client_action="wait", error_code="VERIFY_UNKNOWN").action is Action.WAIT
    assert decide(client_action="success", error_code="VERIFY_READY").action is Action.SUCCESS
    assert decide(client_action="halt", error_code="VERIFY_FAILED").action is Action.HALT


def test_credentials_missing_halts():
    assert decide(http_status=401, detail="API credentials not configured").action is Action.HALT


def test_unrecognised_action_halts_rather_than_guessing():
    assert decide(client_action="teleport").action is Action.HALT


def test_no_action_yet_waits():
    assert decide().action is Action.WAIT


# ------------------------------------------------------- per-problem folding

VERIFY_BODY = {
    "jobId": "j1",
    "state": "running",
    "problems": [
        {"slug": "a", "errorCode": "IMPORTED", "clientAction": "proceed",
         "verify": {"state": "READY", "code": "VERIFY_READY", "clientAction": "success"}},
        {"slug": "b", "errorCode": "IMPORTED", "clientAction": "proceed",
         "verify": {"state": "RUNNING", "code": "VERIFY_RUNNING", "clientAction": "wait"}},
        {"slug": "c", "errorCode": "STEP_FAILED", "clientAction": "retry", "verify": None},
    ],
    "parseErrors": [],
}


def test_mixed_job_yields_one_decision_per_slug():
    d = problem_decisions(200, VERIFY_BODY)
    assert d["a"].action is Action.SUCCESS
    assert d["b"].action is Action.WAIT
    assert d["c"].action is Action.RETRY


def test_verify_supersedes_import_state():
    """Import says 'proceed' while the package is still building — verify wins."""
    assert problem_decisions(200, VERIFY_BODY)["b"].action is Action.WAIT


def test_per_slug_attempt_counts_are_respected():
    d = problem_decisions(200, VERIFY_BODY, attempts={"c": STEP_FAILED_RETRY_CAP})
    assert d["c"].action is Action.HALT
    assert d["a"].action is Action.SUCCESS  # unaffected by its sibling's attempts


def test_lost_job_folds_to_a_single_decision():
    d = problem_decisions(404, {"detail": "Unknown jobId: j1"})
    assert d["*"].action is Action.RESUBMIT


# ------------------------------------------------------------------ client

def fake(responses):
    calls = []
    def transport(method, url, body, headers=None):
        calls.append((method, url, body))
        status, payload = responses.pop(0)
        return status, json.dumps(payload).encode() if payload is not None else b""
    transport.calls = calls
    return transport


def test_import_sends_real_multipart(tmp_path):
    """The endpoint declares files: List[UploadFile] — a JSON list of paths 422s."""
    z = tmp_path / "a.zip"; z.write_bytes(b"PK\x03\x04payload")
    t = fake([(202, {"jobId": "j9", "state": "running", "problems": [], "parseErrors": []})])
    c = PolygonClient(transport=t)
    assert c.import_problem([z])["jobId"] == "j9"
    method, url, body = t.calls[0]
    assert method == "POST" and body is not None
    assert b'name="files"; filename="a.zip"' in body
    assert b"PK\x03\x04payload" in body          # the actual bytes, not the path
    assert b'name="onExists"' in body and b"fill" in body   # idempotent retry default


def test_import_rejection_raises(tmp_path):
    z = tmp_path / "a.zip"; z.write_bytes(b"x")
    t = fake([(422, {"detail": "missing files"})])
    with pytest.raises(PolygonError) as e:
        PolygonClient(transport=t).import_problem([z])
    assert e.value.status == 422


def test_non_json_body_does_not_crash_the_client():
    """Polygon has been seen returning HTML on a transient; never let that raise."""
    def transport(method, url, body, headers=None):
        return 200, b"<html>gateway hiccup</html>"
    status, payload = PolygonClient(transport=transport).verify_status("j1")
    assert status == 200 and "gateway hiccup" in payload["detail"]


def test_download_passes_problem_id_for_multi_problem_jobs():
    t = fake([(200, {"ok": True})])
    PolygonClient(transport=t).download_package("j1", problem_id=563710)
    assert "problemId=563710" in t.calls[0][1]


def test_interrupted_retries_and_is_capped_like_any_other():
    """A job caught mid-import by a Middleman restart comes back as retryable.

    It reaches Maestro through the generic clientAction mapping rather than a
    case of its own — but it must still be capped: each retry re-runs a
    multi-minute import, and a batch that is itself crashing the service would
    otherwise loop forever.
    """
    early = decide(client_action="retry", error_code="INTERRUPTED", attempts=1)
    assert early.action is Action.RETRY
    late = decide(client_action="retry", error_code="INTERRUPTED", attempts=RETRY_CAP)
    assert late.action is Action.HALT
    assert "INTERRUPTED" in late.reason and "restarting" in late.reason


def test_an_unnamed_retry_code_is_capped_too():
    late = decide(client_action="retry", error_code="SOMETHING_NEW", attempts=RETRY_CAP)
    assert late.action is Action.HALT


def test_a_non_retry_action_ignores_the_cap():
    """`wait` is not an attempt at anything — polling must not exhaust a budget."""
    d = decide(client_action="wait", error_code="VERIFY_UNKNOWN", attempts=99)
    assert d.action is Action.WAIT


def test_importing_with_reset_is_refused():
    """`reset` discards the working copy — never right on a retry path."""
    client = PolygonClient("http://x", transport=lambda *a: (202, b"{}"))
    with pytest.raises(ValueError, match="data loss"):
        client.import_problem(["a.zip"], on_exists="reset")


def test_import_sends_the_limits_polygon_expects():
    """Seconds in the manifest, milliseconds on the wire."""
    sent = {}

    def transport(method, url, body, headers):
        sent["body"] = body
        return 202, b'{"jobId":"j1"}'

    PolygonClient("http://x", transport=transport).import_problem(
        [], time_limit_s=2, memory_limit_mb=512)
    body = sent["body"].decode("utf-8", "replace")
    assert 'name="timeLimit"' in body and "\r\n\r\n2000\r\n" in body
    assert 'name="memoryLimit"' in body and "\r\n\r\n512\r\n" in body


def test_omitting_a_limit_sends_no_field():
    """So the Middleman's default applies only where nothing was authored."""
    sent = {}

    def transport(method, url, body, headers):
        sent["body"] = body
        return 202, b'{"jobId":"j1"}'

    PolygonClient("http://x", transport=transport).import_problem([])
    assert "timeLimit" not in sent["body"].decode("utf-8", "replace")
