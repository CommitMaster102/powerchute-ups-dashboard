"""E2E: permalink view state.

The current view — per-panel zoom windows, or the active preset — is encoded
in the URL hash via history.replaceState, so a specific view can be
bookmarked and reopened. The dashboard file is rewritten on every analyzer
run, so a restored window is clamped to each panel's own data span and falls
back to the full range silently when it no longer applies.
"""
from __future__ import annotations

import pytest
from harness import drag_panel, is_full, span, wait_ready, xwin

pytestmark = pytest.mark.e2e


def _fresh(browser, dashboard_path, hash_=""):
    page = browser.new_page(viewport={"width": 1600, "height": 2400})
    page.goto(dashboard_path.resolve().as_uri() + hash_)
    wait_ready(page)
    return page


def test_zoom_writes_hash_and_reopens(dash, _browser, dashboard_path):
    drag_panel(dash, "lv", 0.3, 0.6)
    win = xwin(dash, "lv")
    h = dash.evaluate("location.hash")
    assert h.startswith("#") and "lv." in h
    page2 = _fresh(_browser, dashboard_path, h)
    try:
        restored = page2.evaluate("__chartsDebug.xwin('lv')")
        assert restored[0] == pytest.approx(win[0], abs=1000)
        assert restored[1] == pytest.approx(win[1], abs=1000)
        # Only lv was zoomed; the others stay at full range.
        assert page2.evaluate("__chartsDebug.zoom('ul')") is None
    finally:
        page2.close()


def test_preset_written_and_restored(dash, _browser, dashboard_path):
    dash.locator('.preset-pill[data-days="1"]').click()
    dash.wait_for_timeout(80)
    assert "p=1" in dash.evaluate("location.hash")
    page2 = _fresh(_browser, dashboard_path, "#p=1")
    try:
        win = page2.evaluate("__chartsDebug.xwin('lv')")
        assert span(win) == pytest.approx(86400e3, rel=0.05)
        # The pill row reflects the restored preset.
        active = page2.locator(".preset-pill.is-active").get_attribute("data-days")
        assert active == "1"
    finally:
        page2.close()


def test_malformed_hash_falls_back_silently(_browser, dashboard_path):
    for h in ("#z=lv.zzz", "#z=nope.1.2,also-bad", "#garbage&&&"):
        page = _fresh(_browser, dashboard_path, h)
        try:
            assert page.evaluate("__chartsDebug.ready()")
            assert is_full(page, "lv"), f"hash {h!r} did not fall back to full range"
        finally:
            page.close()


def test_reset_clears_hash(dash):
    drag_panel(dash, "lv", 0.3, 0.6)
    assert dash.evaluate("location.hash") != ""
    dash.locator('.tool-reset[data-panel="lv"]').click()
    dash.wait_for_timeout(80)
    assert dash.evaluate("location.hash") in ("", "#")
