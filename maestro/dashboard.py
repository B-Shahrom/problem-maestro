"""A read-mostly view of the run store, over stdlib HTTP.

The pipeline is mostly unattended, so this exists for the moments when it is not:
a run parked on an expired session, a batch stopped mid-chore chain, an audit
that found a gap. Those states were designed to wait for a human, and until now
the only way to see or clear one was `sqlite3` at a prompt.

Four mutations, and only four. Each is a decision the state machine
deliberately refuses to make for itself:

* **approve** opens the apply gate for one run. Per-run because a scheduler-wide
  `apply` cannot be granted to a single batch.
* **resume** clears FAILED or BLOCKED back to RUNNING. Nothing else in the system
  does this, on purpose: a run stops so that a person looks at it, and code that
  un-stopped runs on a timer would make every stop meaningless.
* **forget** deletes the run. The only destructive action here, and the only one
  that is not reversible — see `Store.delete_run` for what it does and, more
  importantly, what it does not undo.
* **divisions** picks which divisions this batch is granted to. A per-batch
  choice rather than a per-install one, because that is what it actually is —
  see `maestro.divisions`.

None of them retries anything by itself. Clearing the status only makes the run
eligible for the next tick, and the lane's own idempotency rules still decide
what actually happens — approving a run does not bypass them.
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import divisions as div
from .model import RunStage, RunStatus
from .store import Store

#: Browsers send a cross-origin form POST without a preflight, so a page on
#: another site could otherwise drive this server through the operator's browser.
#: A custom header cannot be set cross-origin without CORS approval, which this
#: never gives — so requiring one is a complete defence against that, at the cost
#: of one line in the fetch call.
GUARD_HEADER = "X-Maestro"


class _Handler(BaseHTTPRequestHandler):
    server_version = "Maestro"
    store: Store           # set on the server, read through `self.server`
    scheduler = None

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args) -> None:
        """Silence the default stderr access log — the run log is the interesting one."""

    def _send(self, code: int, payload: object, content_type: str = "application/json") -> None:
        body = (payload if isinstance(payload, bytes)
                else json.dumps(payload, default=str).encode("utf-8"))
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here should be cached: the whole point is the current state.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    @property
    def _store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    # -------------------------------------------------------------- routing

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        try:
            if not parts:
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if parts == ["api", "divisions"]:
                # The vocabulary, served rather than duplicated in the page, so
                # the checklist and the validator can never list different names.
                return self._send(200, {"divisions": list(div.DIVISIONS)})
            if parts == ["api", "runs"]:
                return self._send(200, {"runs": [self._summary(r)
                                                 for r in self._store.list_runs()]})
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                return self._run_detail(parts[2])
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "events":
                return self._events(parts[2], url.query)
        except ValueError:
            return self._send(400, {"error": "bad run id"})
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.headers.get(GUARD_HEADER) is None:
            # See GUARD_HEADER. This is the CSRF check, not authentication —
            # binding to localhost is what keeps strangers out.
            return self._send(403, {"error": f"missing {GUARD_HEADER} header"})
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) != 4 or parts[:2] != ["api", "runs"]:
            return self._send(404, {"error": "not found"})
        try:
            run_id = int(parts[2])
        except ValueError:
            return self._send(400, {"error": "bad run id"})
        action = parts[3]
        if action not in ("approve", "resume", "forget", "divisions"):
            return self._send(404, {"error": f"unknown action {action!r}"})

        run = self._store.get_run(run_id)
        if run is None:
            return self._send(404, {"error": f"no run {run_id}"})
        if action == "forget":
            return self._forget(run)
        if action == "divisions":
            return self._divisions(run)
        getattr(self, f"_{action}")(run)
        self._send(200, {"run": self._summary(self._store.get_run(run_id))})

    # -------------------------------------------------------------- actions

    def _approve(self, run) -> None:
        self._store.approve(run.id)
        self._store.log(run.id, "info", "approved by the operator — writes to ElectiCode enabled")
        if run.status is RunStatus.BLOCKED:
            # An approval that left the run blocked would need a second click to
            # do anything, and the operator has already said what they want.
            self._resume(run)

    def _divisions(self, run) -> None:
        """Set this batch's division access from a ticked list.

        Validated against the closed vocabulary here rather than left to the
        Scraper. `division set` does reject an unknown name — with exit `1`, at
        the *end* of the chore chain, after `fixmdx` and `metadata` have already
        run and been paid for. Catching it at the moment of choosing costs one
        comparison.

        Sent after the chores have run, it is accepted and recorded but says so:
        the grant already happened (or did not), and changing the number now
        changes only what the audit will look for.
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send(400, {"error": "expected a JSON body"})

        raw = body.get("divisions")
        if raw is None:
            # Explicit null restores the configured default; an empty list does
            # not. They are different requests and must not collapse.
            self._store.set_divisions(run.id, None)
            self._store.log(run.id, "info", "divisions: cleared — the configured default applies")
            return self._send(200, {"run": self._summary(self._store.get_run(run.id))})

        if not isinstance(raw, list) or any(not isinstance(x, str) for x in raw):
            return self._send(400, {"error": "divisions must be a list of names, or null"})
        names, unknown = div.normalise(", ".join(raw))
        if unknown:
            return self._send(400, {"error": f"unknown division(s): {', '.join(unknown)}. "
                                             f"Valid: {', '.join(div.DIVISIONS)}"})

        spec = div.render(names)
        self._store.set_divisions(run.id, spec)
        note = ""
        if run.stage in (RunStage.AUDIT, RunStage.DONE):
            note = (" — but the chores have already run, so this changes only what the "
                    "audit checks for, not what was granted")
        self._store.log(run.id, "info",
                        f"divisions: {div.describe(spec)}{note}")
        self._send(200, {"run": self._summary(self._store.get_run(run.id)), "note": note})

    def _forget(self, run) -> None:
        """Delete a run, unless a subprocess is still working on its behalf.

        The refusal is the point. A browser driving an upload for run 12 does not
        stop because run 12 was deleted — it carries on writing to the platform
        for a batch Maestro no longer has any record of, and every store call the
        lane makes afterwards fails against rows that are gone. Stopping the
        scheduler first is one action; recovering from that state is several.
        """
        sched = getattr(self.server, "scheduler", None)
        if sched is not None and sched.current_electicode == run.id:
            return self._send(409, {"error": (
                f"run {run.id} is being uploaded right now — stop Maestro (or wait "
                f"for the stage to finish) before deleting it")})
        self._store.delete_run(run.id)
        self._send(200, {"deleted": run.id, "set_name": run.set_name})

    def _resume(self, run) -> None:
        if run.status not in (RunStatus.FAILED, RunStatus.BLOCKED):
            return
        self._store.set_run(run.id, status=RunStatus.RUNNING)
        self._store.log(run.id, "info", f"resumed by the operator from {run.status}")

    # --------------------------------------------------------------- reads

    def _run_detail(self, raw: str) -> None:
        run = self._store.get_run(int(raw))
        if run is None:
            return self._send(404, {"error": f"no run {raw}"})
        detail = self._summary(run)
        detail["problems"] = [asdict(p) for p in run.problems]
        detail["set_dir"] = run.set_dir
        return self._send(200, {"run": detail})

    def _events(self, raw: str, query: str) -> None:
        after = int((parse_qs(query).get("after") or ["0"])[0] or 0)
        rows = self._store.events(int(raw), after_id=after)
        return self._send(200, {"events": [dict(r) for r in rows],
                                "cursor": rows[-1]["id"] if rows else after})

    @staticmethod
    def _summary(run) -> dict:
        """One row's worth. Problem counts rather than the problems themselves."""
        by_status: dict[str, int] = {}
        for p in run.problems:
            by_status[str(p.status)] = by_status.get(str(p.status), 0) + 1
        return {
            "id": run.id,
            "set_name": run.set_name,
            "stage": str(run.stage),
            "status": str(run.status),
            "block_reason": str(run.block_reason) if run.block_reason else None,
            "error": run.error,
            "approved": run.approved,
            "divisions": run.divisions,
            "divisions_label": div.describe(run.divisions),
            "divisions_locked": run.stage in (RunStage.AUDIT, RunStage.DONE),
            "problems": len(run.problems),
            "by_status": by_status,
            "updated_at": run.updated_at,
        }


class Dashboard:
    """Owns the HTTP server. `store` is shared with the scheduler, which is safe."""

    def __init__(self, store: Store, *, host: str = "127.0.0.1", port: int = 8787,
                 scheduler=None) -> None:
        self.store = store
        self.scheduler = scheduler
        # Localhost by default. This has no authentication of its own — remote
        # access is over the mesh VPN, which is where the access control lives —
        # so binding it to a public interface would expose the approve action to
        # anyone who can reach the port.
        self._server = ThreadingHTTPServer((host, port), _Handler)
        self._server.store = store  # type: ignore[attr-defined]
        # Read by `forget` so a delete cannot land under a live upload.
        self._server.scheduler = scheduler  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> "Dashboard":
        # serve_forever polls at 0.5s by default, and shutdown() waits for the
        # loop to notice — so that interval is the cost of every stop.
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        args=(0.05,), name="dashboard", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)

    def __enter__(self) -> "Dashboard":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>Maestro</title>
<style>
  :root { color-scheme: light dark; --line: #8883; --dim: #8888; }
  body { font: 14px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0; padding: 1.5rem; }
  h1 { font-size: 1.1rem; margin: 0 0 1rem; font-weight: 600; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--line); }
  th { font-weight: 600; color: var(--dim); font-size: .85rem; }
  tr.sel { background: #8882; }
  tbody tr { cursor: pointer; }
  .pill { padding: .05rem .4rem; border-radius: .5rem; border: 1px solid var(--line); font-size: .85rem; }
  .blocked, .failed { color: #d33; border-color: #d336; }
  .done { color: #2a2; border-color: #2a26; }
  .dim { color: var(--dim); }
  button { font: inherit; padding: .2rem .7rem; margin-right: .4rem; cursor: pointer; }
  button.danger { color: #d33; float: right; margin-right: 0; }
  #divs { margin-top: .7rem; }
  #divs label { display: inline-block; margin-right: .9rem; white-space: nowrap; cursor: pointer; }
  #divs input { vertical-align: -1px; margin-right: .25rem; }
  #divs .head { color: var(--dim); margin-bottom: .3rem; }
  #divs.locked label { opacity: .55; cursor: default; }
  .debug { color: var(--dim); }
  #log { white-space: pre-wrap; max-height: 22rem; overflow-y: auto; border: 1px solid var(--line);
         padding: .6rem; margin-top: .8rem; }
  .warn { color: #b80; } .error { color: #d33; }
  #detail { margin-top: 1.5rem; }
  #err { color: #d33; }
</style>
<h1>Maestro <span class="dim" id="tick"></span></h1>
<table><thead><tr><th>#</th><th>set</th><th>stage</th><th>status</th><th>problems</th><th>note</th></tr></thead>
<tbody id="runs"></tbody></table>
<div id="detail" hidden>
  <div id="actions"></div>
  <div id="divs"></div>
  <div id="err"></div>
  <div id="log"></div>
</div>
<script>
let sel = null, cursor = 0, runs = [], DIVISIONS = [];

const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// Every click is delegated from `data-act`, and nothing but a run id is ever
// written into markup. The delete button originally interpolated the set name
// into an `onclick=""` attribute: `JSON.stringify` emits double quotes, which
// closed the attribute on themselves, so the button rendered and its handler
// did not parse. Passing data through attributes at all is the bug; this
// removes the possibility rather than escaping around it.
document.addEventListener("click", ev => {
  const el = ev.target.closest("[data-act]");
  if (!el) return;
  const id = Number(el.dataset.id);
  const act = el.dataset.act;
  if (act === "select") select(id);
  else if (act === "save-divisions") saveDivisions(id);
  else if (act === "reset-divisions") resetDivisions(id);
  else post(id, act);
});

async function saveDivisions(id) {
  // Ticked boxes only — an empty list is a real answer ("grant nothing"), which
  // is why it is sent as [] rather than omitted. `null` is a separate request
  // that restores the configured default.
  const picked = [...document.querySelectorAll("#divs input:checked")].map(b => b.value);
  await send(id, "divisions", { divisions: picked });
}

async function resetDivisions(id) { await send(id, "divisions", { divisions: null }); }

async function send(id, action, body) {
  const r = await fetch(`/api/runs/${id}/${action}`, {
    method: "POST", headers: { "X-Maestro": "1", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const err = document.getElementById("err");
  err.textContent = r.ok ? "" : (await r.json()).error;
  refresh();
}

async function post(id, action) {
  const run = runs.find(r => r.id === id);
  // The one destructive action asks first, and names what it cannot undo. The
  // record goes; anything already on Polygon or ElectiCode stays.
  if (action === "forget" && !confirm(
      `Delete run ${id} (${run ? run.set_name : "?"}) from Maestro?\n\n` +
      `This removes Maestro's record only. Problems already imported to Polygon ` +
      `or uploaded to ElectiCode are NOT removed.\n\n` +
      `The set folder becomes eligible for ingest again, so leaving it in the ` +
      `watch directory will start the whole run over.`)) return;
  // The guard header is what makes this unreachable from another site's page.
  const r = await fetch(`/api/runs/${id}/${action}`, {
    method: "POST", headers: { "X-Maestro": "1" },
  });
  const err = document.getElementById("err");
  if (!r.ok) { err.textContent = (await r.json()).error; }
  else { err.textContent = ""; if (action === "forget") sel = null; }
  cursor = 0; document.getElementById("log").textContent = "";
  refresh();
}

function select(id) { sel = id; cursor = 0; document.getElementById("log").textContent = ""; refresh(); }

async function refresh() {
  if (!DIVISIONS.length) DIVISIONS = (await (await fetch("/api/divisions")).json()).divisions;
  runs = (await (await fetch("/api/runs")).json()).runs;
  document.getElementById("runs").innerHTML = runs.map(r => `
    <tr data-act="select" data-id="${r.id}" class="${r.id === sel ? "sel" : ""}">
      <td>${r.id}</td><td>${esc(r.set_name)}</td><td>${esc(r.stage)}</td>
      <td><span class="pill ${esc(r.status)}">${esc(r.status)}</span></td>
      <td>${r.problems}</td>
      <td class="dim">${esc(r.block_reason || r.error || "")}</td>
    </tr>`).join("");

  const detail = document.getElementById("detail");
  const run = runs.find(r => r.id === sel);
  detail.hidden = !run;
  if (run) {
    const stopped = run.status === "blocked" || run.status === "failed";
    document.getElementById("actions").innerHTML =
      (run.approved ? `<span class="dim">approved &middot; </span>` :
        `<button data-act="approve" data-id="${run.id}">approve writes</button>`) +
      (stopped ? `<button data-act="resume" data-id="${run.id}">resume</button>` : "") +
      `<button class="danger" data-act="forget" data-id="${run.id}">delete run</button>`;
    // Re-rendered only when the selection or the stored value changes, so a
    // half-ticked checklist is not wiped by the three-second poll underneath it.
    const stamp = `${run.id}:${run.divisions}:${run.divisions_locked}`;
    const box = document.getElementById("divs");
    if (box.dataset.stamp !== stamp) {
      box.dataset.stamp = stamp;
      box.className = run.divisions_locked ? "locked" : "";
      const on = new Set((run.divisions ?? "").split(",").map(s => s.trim()).filter(Boolean));
      const dis = run.divisions_locked ? " disabled" : "";
      box.innerHTML =
        `<div class="head">division access &middot; ${esc(run.divisions_label)}` +
        (run.divisions_locked ? " &middot; chores already ran" : "") + `</div>` +
        DIVISIONS.map(d =>
          `<label><input type="checkbox" value="${esc(d)}"${on.has(d) ? " checked" : ""}${dis}>` +
          `${esc(d)}</label>`).join("") +
        (run.divisions_locked ? "" :
          `<div style="margin-top:.5rem">` +
          `<button data-act="save-divisions" data-id="${run.id}">save divisions</button>` +
          `<button data-act="reset-divisions" data-id="${run.id}">use config default</button>` +
          `</div>`);
    }

    const { events, cursor: c } = await (await fetch(`/api/runs/${sel}/events?after=${cursor}`)).json();
    cursor = c;
    if (events.length) {
      const log = document.getElementById("log");
      log.innerHTML += events.map(e =>
        `<div class="${esc(e.level)}">${esc(e.at)} ${esc(e.slug || "")} ${esc(e.message)}</div>`).join("");
      log.scrollTop = log.scrollHeight;
    }
  }
  document.getElementById("tick").textContent = new Date().toLocaleTimeString();
}
refresh();
setInterval(refresh, 3000);
</script>
"""
