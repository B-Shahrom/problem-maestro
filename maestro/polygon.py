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
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

#: Every `retry` is capped, whatever its error code.
#:
#: `STEP_FAILED` is the case that forced this: it covers both a transient Polygon
#: HTML-instead-of-JSON response and a genuine content error (a non-compiling
#: `solution.cpp`) indistinguishably, and the second kind never recovers.
#: `INTERRUPTED` — a job that was mid-import when the Middleman restarted — has the
#: same shape for a different reason: it recovers if the restart was incidental, and
#: never if this batch is what brings the service down. A retry here re-runs a
#: multi-minute import, so an uncapped loop is expensive as well as futile.
RETRY_CAP = 3
STEP_FAILED_RETRY_CAP = RETRY_CAP  # kept: the name says why the cap exists at all


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

    **`retry` is capped.** The taxonomy says retry without saying how often, and
    two of its codes (`STEP_FAILED`, `INTERRUPTED`) describe conditions that may
    never clear. Retrying a non-compiling solution forever is the failure mode a
    cap prevents.
    """
    if http_status == 404 and "unknown jobid" in detail.lower():
        return Decision(Action.RESUBMIT, "middleman restarted and lost the job; import is idempotent")
    if http_status == 404:
        # download-package before the package exists — poll verify-status instead.
        return Decision(Action.WAIT, detail or "not ready yet")
    if http_status is not None and http_status >= 400:
        return Decision(Action.HALT, detail or f"HTTP {http_status}")

    if client_action == "retry" and attempts >= RETRY_CAP:
        why = {
            "STEP_FAILED": "treating as a content error, not a transient",
            "INTERRUPTED": "the Middleman keeps restarting mid-import",
        }.get(error_code or "", "the condition is not clearing")
        return Decision(Action.HALT, f"{error_code or 'retry'} after {attempts} attempts — {why}")

    if client_action is None:
        return Decision(Action.WAIT, "no action reported yet")
    try:
        return Decision(Action(client_action), error_code or client_action)
    except ValueError:
        # An action this build doesn't know. Halting beats guessing.
        return Decision(Action.HALT, f"unrecognised clientAction {client_action!r}")


Transport = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, bytes]]
"""(method, url, body, headers) -> (status, raw). Injected so tests need no live service.

Bytes rather than a dict because the import endpoint takes real multipart file
uploads, not a JSON manifest of paths.
"""


def _urllib_transport(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _multipart(files: list[Path], fields: dict[str, str]) -> tuple[bytes, str]:
    """Encode archives as `files` parts plus form fields, per the endpoint's signature.

    `POST /api/import-problem` declares `files: List[UploadFile] = File(...)` with
    `timeLimit` / `memoryLimit` / `onExists` / `checkerType` / `solutionType` as
    optional form fields — so a JSON body of paths is rejected outright.
    """
    boundary = "----maestro" + uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
    for path in files:
        out += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="files"; filename="{path.name}"\r\n'
            f"Content-Type: application/zip\r\n\r\n"
        ).encode()
        out += path.read_bytes() + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


class PolygonClient:
    """Thin wrapper over the Middleman. Holds no state and no retry loop.

    Sequencing and retry live in the orchestrator, which owns the job store and so
    is the only layer that can persist an attempt count across a restart.
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8000", transport: Transport | None = None) -> None:
        self.base = base_url.rstrip("/")
        self._send = transport or _urllib_transport

    def _call(self, method: str, path: str) -> tuple[int, Any]:
        status, raw = self._send(method, f"{self.base}{path}", None, {})
        try:
            return status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            # Polygon has been observed returning HTML on a transient; the Middleman
            # folds that into VERIFY_UNKNOWN, but a proxied endpoint could still leak it.
            return status, {"detail": raw[:200].decode("utf-8", "replace")}

    def import_problem(self, archives: list[str | Path], *, on_exists: str = "fill") -> dict[str, Any]:
        """Submit archives as multipart. Returns the 202 job snapshot.

        `on_exists="fill"` is the endpoint's own default and the behaviour every
        retry in Maestro depends on: the existing problem is resolved by name and
        updated **in place**, keeping its Polygon id, and tests are keyed by
        description so a re-run replaces matching ones and appends new ones.

        The only other accepted value, `reset`, **discards the working copy
        first**. That is destructive and never right for a retry path, so it is
        rejected here rather than left to the server's clamp — a resubmit that
        silently reset a problem would destroy work no other stage can recover.

        Passing a main archive and its `<slug>-tests` pack together is correct —
        the endpoint merges same-slug archives into one problem.
        """
        if on_exists != "fill":
            raise ValueError(
                f"onExists={on_exists!r}: Maestro only ever imports with 'fill'. "
                "'reset' discards the existing working copy, which would turn a "
                "retry into data loss."
            )
        paths = [Path(a) for a in archives]
        body, content_type = _multipart(paths, {"onExists": on_exists})
        status, raw = self._send("POST", f"{self.base}/api/import-problem", body,
                                 {"Content-Type": content_type})
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = {"detail": raw[:200].decode("utf-8", "replace")}
        if status != 202:
            raise PolygonError(status, (parsed or {}).get("detail", "import rejected"))
        return parsed

    def parse(self, archives: list[str | Path]) -> dict[str, Any]:
        """Dry run: what would these archives import as? No Polygon calls.

        The same parser the import uses, so its answer is authoritative where
        Maestro's own reading of an archive is only a second opinion.
        """
        body, content_type = _multipart([Path(a) for a in archives], {})
        status, raw = self._send("POST", f"{self.base}/api/parse", body,
                                 {"Content-Type": content_type})
        try:
            parsed = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            parsed = None
        if status != 200 or parsed is None:
            detail = (parsed or {}).get("detail") if isinstance(parsed, dict) else \
                raw[:200].decode("utf-8", "replace")
            raise PolygonError(status, detail or "parse failed")
        return parsed

    def verify_status(self, job_id: str) -> tuple[int, dict[str, Any]]:
        """Poll a job. Returns `(http_status, body)` — 404 is expected after a restart."""
        return self._call("GET", f"/api/verify-status/{job_id}")

    def download_package(self, job_id: str, problem_id: int | None = None) -> tuple[int, bytes]:
        """Fetch the READY package. Returns raw zip bytes — never JSON-decoded.

        A 404 here means "not built yet", which `decide` maps to WAIT rather than
        an error; the body carries the reason as text.
        """
        q = f"?problemId={problem_id}" if problem_id is not None else ""
        return self._send("GET", f"{self.base}/api/download-package/{job_id}{q}", None, {})


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
