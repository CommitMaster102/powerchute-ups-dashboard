"""E2E: what is (and is not) shared between panels — the crosshair mirrors
across the sync group on hover, the preset pills set every time panel's own
window, and zoom state never propagates by itself."""
from __future__ import annotations

import pytest
from harness import SYNC_PANELS, TIME_PANELS, full_xwin, hover_panel, is_full, span, xwin

pytestmark = pytest.mark.e2e


def test_crosshair_mirrors_across_group(dash):
    hover_panel(dash, "lv")
    assert dash.evaluate("__chartsDebug.hover('lv')")
    for key in SYNC_PANELS:
        n = dash.evaluate(
            f"document.querySelector('#panel-{key} svg .chart-overlay').childNodes.length")
        assert n > 0, f"sync panel {key} has no crosshair while hovering lv"
    # A panel outside the group does not mirror the crosshair.
    assert dash.evaluate(
        "document.querySelector('#panel-growth svg .chart-overlay').childNodes.length") == 0


def test_heatmap_highlights_hovered_day(dash):
    """Hovering a sync time panel outlines the matching day row (and hour
    cell) in the Hourly Power Map, so a spike in Power Draw is easy to locate
    in the day-by-hour view."""
    hover_panel(dash, "lv", fx=0.3)
    marks = dash.evaluate(
        "document.querySelector('#panel-hm svg .chart-overlay').childNodes.length")
    assert marks >= 2, "heatmap did not highlight the hovered day and hour"
    dash.mouse.move(0, 0)
    dash.wait_for_timeout(80)
    assert dash.evaluate(
        "document.querySelector('#panel-hm svg .chart-overlay').childNodes.length") == 0


def test_setwindow_hook_addresses_all_time_panels(dash):
    """__chartsDebug.setWindow (the preset/test path) clamps to each panel's
    own span — panels the window doesn't cover fall back to full range."""
    dash.evaluate("(() => { const f = __chartsDebug.fullXwin('lv');"
                  "__chartsDebug.setWindow(f[0], f[0] + (f[1]-f[0])*0.3); })()")
    dash.wait_for_timeout(80)
    assert dash.evaluate("__chartsDebug.isZoomed('lv')")
    assert dash.evaluate("__chartsDebug.isZoomed('ul')")


def test_preset_pills_anchor_each_panel(dash):
    dash.click(".preset-pill[data-days='1']")
    dash.wait_for_timeout(80)
    for key in ["lv", "pw"]:
        win, full = xwin(dash, key), full_xwin(dash, key)
        assert abs(span(win) - 86400e3) < 1000, f"{key}: 24h preset span wrong"
        assert abs(win[1] - full[1]) < 1000, f"{key}: preset must anchor to its own newest sample"
    assert dash.locator(".preset-pill[data-days='1']").evaluate(
        "el => el.classList.contains('is-active')")
    dash.click(".preset-pill[data-days='all']")
    dash.wait_for_timeout(80)
    for key in TIME_PANELS:
        assert dash.evaluate(f"__chartsDebug.zoom('{key}')") is None
    assert dash.locator(".preset-pill[data-days='all']").evaluate(
        "el => el.classList.contains('is-active')")


def test_preset_pills_carry_aria_pressed_kept_current(dash):
    """The preset pills are a toggle group: exactly the active one reports
    aria-pressed=true, and it moves with the selection."""
    dash.click(".preset-pill[data-days='1']")
    dash.wait_for_timeout(80)
    assert dash.locator(".preset-pill[data-days='1']").get_attribute("aria-pressed") == "true"
    assert dash.locator(".preset-pill[data-days='all']").get_attribute("aria-pressed") == "false"
    assert dash.locator(".preset-pill[data-days='30']").get_attribute("aria-pressed") == "false"
    dash.click(".preset-pill[data-days='all']")
    dash.wait_for_timeout(80)
    assert dash.locator(".preset-pill[data-days='all']").get_attribute("aria-pressed") == "true"
    assert dash.locator(".preset-pill[data-days='1']").get_attribute("aria-pressed") == "false"


def test_preset_30d_clamps_or_windows_by_history(dash):
    """A 30 d preset on a history shorter than 30 days must leave the short
    panels at full range instead of producing an out-of-range window (the
    synthetic fixture's ~4 days); on a longer history (a real dashboard
    under STATEOFUPS_E2E_REAL) it must window to exactly 30 days anchored
    at the panel's own newest sample."""
    full = full_xwin(dash, "lv")
    dash.click(".preset-pill[data-days='30']")
    dash.wait_for_timeout(80)
    if span(full) <= 30 * 86400e3:
        assert is_full(dash, "lv")
    else:
        win = xwin(dash, "lv")
        assert abs(span(win) - 30 * 86400e3) < 1000, "30 d preset span wrong"
        assert abs(win[1] - full[1]) < 1000, "preset must anchor to the newest sample"


def test_manual_zoom_clears_preset_state(dash):
    dash.click(".preset-pill[data-days='1']")
    dash.wait_for_timeout(80)
    dash.evaluate("(() => { const f = __chartsDebug.fullXwin('lv');"
                  "__chartsDebug.setWindow(f[0], f[0] + (f[1]-f[0])*0.5); })()")
    dash.wait_for_timeout(80)
    assert not dash.locator(".preset-pill[data-days='1']").evaluate(
        "el => el.classList.contains('is-active')")


def test_reset_all_restores_everything(dash):
    dash.evaluate("(() => { const f = __chartsDebug.fullXwin('lv');"
                  "__chartsDebug.setWindow(f[0], f[0] + (f[1]-f[0])*0.2); })()")
    dash.evaluate("__chartsDebug.resetAll()")
    dash.wait_for_timeout(80)
    for key in TIME_PANELS:
        assert dash.evaluate(f"__chartsDebug.zoom('{key}')") is None
    assert is_full(dash, "lv")
