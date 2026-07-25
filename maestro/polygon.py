"""Polygon Middleman client and the retry policy layered on top of it.

The Middleman's Phase 2 API is asynchronous: `POST /api/import-problem` returns
`202 {jobId, ...}` immediately, `GET /api/verify-status/{jobId}` reports per-problem
import *and* build/verify state, and every state carries an `errorCode` plus a
`clientAction` telling a client what to do. That taxonomy is the contract; this
module wraps it and adds the two places where Maestro must decide differently.

Transport is injected so the policy is testable without a live service.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

#: `STEP_FAILED` covers both a transient Polygon HTML-instead-of-JSON response and a
#: genuine content error (a non-compiling `solution.cpp`). The taxonomy says retry but
#: cap it, because the second kind never recovers.
STEP_FAILED_RETRY_CAP = 3


class Action(StrEnum):
    """What Maestro does next.

    The first five mirror the Middleman's `clientAction` values. `RESUBMIT` is
    Maestro's own — see `decide`.
    """

    PROCEED = "proceed"    # import done, start polling verify
    SUCCESS = "success"    # package is READY and fetchable
    WAIT = "wait"          # not terminal yet, poll again
    RETRY = "retry"        # re-attempt the same call
    HALT = "halt"          # stop; a human or a fix is required
    RESUBMIT = "resubmit"  # re-POST the import from scratch


@dataclass(frozen=True, slots=True)
class Decision:
    action: Action
    reason: str


class PolygonError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


def decide(
    *,
    client_action: str | None = None,
    error_code: str | None = None,
    http_status: int | None = None,
    detail: str = "",
    attempts: int = 1,
) -> Decision:
    """Translate a Middleman response into Maestro's next move.

    Two deliberate divergences from the published `clientAction`:

    **Lost job → RESUBMIT, not HALT.** The Middleman's job registry is in-memory
    and does not survive a restart, so a `404 Unknown jobId` is reported as `halt`
    — correct for a generic client, which has nothing to fall back on. Maestro
    does: it persists the slug and the import is idempotent (fill mode reuses the
    same Polygon problem rather than duplicating), so re-POSTing recovers. Halting
    a batch because the *orchestrated service* restarted would be a self-inflicted
    outage.

    **`STEP_FAILED` retries are capped.** The code covers a transient and a
    content error indistinguishably; retrying a non-compiling solution forever is
    the failure mode a cap prevents.
    """
    if http_status == 404 and "unknown jobid" in detail.lower():
        return Decision(Action.RESUBMIT, "middleman restarted and lost the job; import is idempotent")
    if http_status == 404:
        # download-package before the package exists — poll verify-status instead.
        return Decision(Action.WAIT, detail or "not ready yet")
    if http_status is not None and http_status >= 400:
        return Decision(Action.HALT, detail or f"HTTP {http_status}")

    if error_code == "STEP_FAILED" and attempts >= STEP_FAILED_RETRY_CAP:
        return Decision(
            Action.HALT,
            f"STEP_FAILED after {attempts} attempts — treating as a content error, not a transient",
        )

    if client_action is None:
        return Decision(Action.WAIT, "no action reported yet")
    try:
        return Decision(Action(client_action), error_code or client_action)
    except ValueError:
        # An action this build doesn't know. Halting beats guessing.
        return Decision(Action.HALT, f"unrecognised clientAction {client_action!r}")


Transport = Callable[[str, str, dict[str, Any] | None], tuple[int, bytes]]
"""(method, url, body) -> (status, raw). Injected so tests need no live service."""


def _urllib_transport(method: str, url: str, body: dict[str, Any] | None) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class PolygonClient:
    """Thin wrapper over the Middleman. Holds no state and no retry loop.

    Sequencing and retry live in the orchestrator, which owns the job store and so
    is the only layer that can persist an attempt count across a restart.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8000", transport: Transport | None = None) -> None:
        self.base = base_url.rstrip("/")
        self._send = transport or _urllib_transport

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
        status, raw = self._send(method, f"{self.base}{path}", body)
        try:
            return status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            # Polygon has been observed returning HTML on a transient; the Middleman
            # folds that into VERIFY_UNKNOWN, but a proxied endpoint could still leak it.
            return status, {"detail": raw[:200].decode("utf-8", "replace")}

    def import_problem(self, archives: list[str]) -> dict[str, Any]:
        """Submit archives. Returns the 202 job snapshot.

        Multipart upload is left to the caller's transport; `archives` are paths the
        transport is expected to attach as `files`.
        """
        status, body = self._call("POST", "/api/import-problem", {"files": archives})
        if status != 202:
            raise PolygonError(status, (body or {}).get("detail", "import rejected"))
        return body

    def verify_status(self, job_id: str) -> tuple[int, dict[str, Any]]:
        """Poll a job. Returns `(http_status, body)` — 404 is expected after a restart."""
        return self._call("GET", f"/api/verify-status/{job_id}")

    def download_package(self, job_id: str, problem_id: int | None = None) -> tuple[int, Any]:
        q = f"?problemId={problem_id}" if problem_id is not None else ""
        return self._call("GET", f"/api/download-package/{job_id}{q}")


def problem_decisions(status: int, body: dict[str, Any], attempts: dict[str, int] | None = None) -> dict[str, Decision]:
    """Fold a `verify-status` response into one decision per slug.

    A job mixes states — one problem `READY`, another still `RUNNING`, a third
    `STEP_FAILED`. The batch advances only when every problem is terminal, so the
    caller needs them individually, not an aggregate.
    """
    attempts = attempts or {}
    if status >= 400:
        detail = (body or {}).get("detail", "")
        return {"*": decide(http_status=status, detail=detail)}

    out: dict[str, Decision] = {}
    for p in body.get("problems", []):
        slug = p.get("slug") or p.get("name") or "?"
        verify = p.get("verify")
        # Verify state supersedes import state once the build has been requested:
        # the import may say `proceed` while the package is still RUNNING.
        src = verify if verify else p
        out[slug] = decide(
            client_action=src.get("clientAction"),
            error_code=src.get("code") or src.get("errorCode"),
            attempts=attempts.get(slug, 1),
        )
    return out
