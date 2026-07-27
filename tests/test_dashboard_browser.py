"""The dashboard driven by a real browser, when one is available.

This exists because a structural check cannot see a broken page. The delete
button shipped doing nothing — `JSON.stringify(run.set_name)` was interpolated
into an `onclick=""` attribute, its double quotes closed the attribute, and the
handler never parsed. Every server-side test passed throughout, because the
server was never wrong.

Skipped, like the two cross-repo round-trip modules, when its dependency is
absent — so `python -m pytest` still needs nothing but the stdlib. Install
`playwright` to turn it on; the browser is found from `PLAYWRIGHT_BROWSERS_PATH`
or the usual locations rather than downloaded.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from maestro.dashboard import Dashboard
from maestro.model import ProblemSeed, RunStatus
from maestro.store import Store

sync_playwright = pytest.importorskip(
    "playwright.sync_api", reason="playwright not installed").sync_playwright


def _chromium() -> str | None:
    """An installed Chromium, or None. Never downloads one."""
    roots = [Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")),
             Path.home() / ".cache" / "ms-playwright"]
    for root in roots:
        if not root.is_dir():
            continue
        for pattern in ("chromium-*/chrome-linux/chrome", "chromium-*/chrome-*/Chromium.app/*/*/MacOS/Chromium",
                        "chromium-*/chrome-win/chrome.exe"):
            if found := sorted(root.glob(pattern)):
                return str(found[-1])
    return None


@pytest.fixture
def page(tmp_path):
    exe = _chromium()
    if exe is None:
        pytest.skip("no installed Chromium found; not downloading one")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=exe, args=["--no-sandbox"])
        p = browser.new_page()
        p.errors = []  # type: ignore[attr-defined]
        p.on("pageerror", lambda e: p.errors.append(str(e)))
        yield p
        browser.close()


@pytest.fixture
def live(tmp_path):
    """A store with one stopped run, and a dashboard serving it."""
    store = Store(tmp_path / "m.db")
    run_id = store.create_run(
        "edu-arrays-20260725", str(tmp_path / "set"),
        [ProblemSeed(slug="edu-arrays-running-max", idx=1, title="Running Maximum",
                     archive="edu-arrays-running-max.zip")])
    store.set_run(run_id, status=RunStatus.FAILED, error="upload failed")
    store.log(run_id, "info", "upload: detected edu-arrays-running-max (new)")
    store.log(run_id, "warn", "upload: still running after 312s, but nothing for 94s")
    with Dashboard(store, port=0) as dash:
        yield store, run_id, f"http://127.0.0.1:{dash.port}/"
    store.close()


def _open(page, url):
    page.goto(url)
    page.wait_for_selector("tbody tr")
    page.click("tbody tr")
    page.wait_for_selector("#actions button")


def test_the_delete_button_actually_deletes(page, live):
    """The regression. It rendered, it read `delete run`, and it did nothing."""
    store, run_id, url = live
    _open(page, url)
    page.on("dialog", lambda d: d.accept())
    page.click("button.danger")
    page.wait_for_function("document.querySelectorAll('tbody tr').length === 0", timeout=10_000)
    assert store.get_run(run_id) is None
    assert page.errors == []


def test_the_confirmation_names_the_run_and_what_it_cannot_undo(page, live):
    store, run_id, url = live
    _open(page, url)
    seen: list[str] = []
    page.on("dialog", lambda d: (seen.append(d.message), d.dismiss()))
    # The dialog handler runs during the click, so no wait is needed after it.
    page.click("button.danger")
    assert seen, "the destructive action must ask first"
    assert "edu-arrays-20260725" in seen[0]
    assert "NOT removed" in seen[0]
    # Dismissed means nothing happened.
    assert store.get_run(run_id) is not None


def test_approve_and_resume_still_work_after_the_handler_rewrite(page, live):
    """Three buttons were rewired; a fix that broke the other two is not a fix."""
    store, run_id, url = live
    _open(page, url)

    page.click("[data-act='approve']")
    page.wait_for_function("document.querySelector('#actions').innerText.includes('approved')",
                           timeout=10_000)
    assert store.get_run(run_id).approved is True

    page.click("[data-act='resume']")
    page.wait_for_function(
        "!document.querySelector('#actions').querySelector(\"[data-act='resume']\")",
        timeout=10_000)
    assert store.get_run(run_id).status is RunStatus.RUNNING
    assert page.errors == []


def test_a_running_stage_s_progress_is_visible_on_the_page(page, live):
    """The other half of the report: an upload that says nothing looks hung."""
    _, _, url = live
    _open(page, url)
    page.wait_for_function(
        "document.querySelector('#log').innerText.includes('312s')", timeout=10_000)
    log = page.inner_text("#log")
    assert "detected edu-arrays-running-max (new)" in log
    # The heartbeat is the line that answers "is it stuck", so it must stand out.
    warns = page.eval_on_selector_all("#log .warn", "els => els.map(e => e.textContent)")
    assert any("312s" in w for w in warns)
