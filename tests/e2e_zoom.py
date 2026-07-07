"""E2E: zoom and pan are strictly local to the gestured panel — drag
zoom-selects, Shift+drag pans, wheel zooms, double-click / pill / keyboard
reset. No gesture ever changes another chart's window."""
from __future__ import annotations

import pytest
from harness import (
    TIME_PANELS,
    drag_panel,
    full_xwin,
    hover_panel,
    is_full,
    panel_box,
    span,
    xwin,
)

pytestmark = pytest.mark.e2e


def test_drag_zoom_is_local_and_resets(dash):
    full = full_xwin(dash, "lv")
    drag_panel(dash, "lv", 0.3, 0.6)
    win = xwin(dash, "lv")
    assert win[0] > full[0] and win[1] < full[1], "drag did not zoom lv"
    assert span(win) < 0.5 * span(full)
    # No other panel moved.
    for key in TIME_PANELS:
        if key != "lv":
            assert dash.evaluate(f"__chartsDebug.zoom('{key}')") is None, f"{key} zoomed too"
    assert not dash.locator(".tool-reset[data-panel='lv']").evaluate("el => el.hidden")
    assert dash.locator(".tool-reset[data-panel='ul']").evaluate("el => el.hidden")
    box = panel_box(dash, "lv")
    dash.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    dash.wait_for_timeout(80)
    assert is_full(dash, "lv")
    assert dash.locator(".tool-reset[data-panel='lv']").evaluate("el => el.hidden")


def test_drag_always_selects_even_when_zoomed(dash):
    """A plain drag is always a zoom selection — zooming twice in a row
    narrows twice, it never silently switches to panning."""
    drag_panel(dash, "lv", 0.2, 0.8)
    first = xwin(dash, "lv")
    drag_panel(dash, "lv", 0.3, 0.7)
    second = xwin(dash, "lv")
    assert span(second) < span(first), "second drag did not re-select"


def test_shift_drag_pans_keeps_span(dash):
    dash.evaluate("(() => { const f = __chartsDebug.fullXwin('lv');"
                  "__chartsDebug.setWindow(f[0] + (f[1]-f[0])*0.4, f[0] + (f[1]-f[0])*0.6); })()")
    dash.wait_for_timeout(50)
    before = xwin(dash, "lv")
    other_before = xwin(dash, "pw")
    box = panel_box(dash, "lv")
    y = box["y"] + box["height"] * 0.5
    dash.keyboard.down("Shift")
    dash.mouse.move(box["x"] + box["width"] * 0.7, y)
    dash.mouse.down()
    dash.mouse.move(box["x"] + box["width"] * 0.3, y, steps=6)   # drag left -> window slides right
    dash.mouse.up()
    dash.keyboard.up("Shift")
    dash.wait_for_timeout(80)
    after = xwin(dash, "lv")
    assert after[0] > before[0]
    assert abs(span(after) - span(before)) < 1000
    assert xwin(dash, "pw") == other_before, "pan on lv leaked into pw"


def test_wheel_zoom_local(dash):
    full = full_xwin(dash, "lv")
    hover_panel(dash, "lv")
    dash.mouse.wheel(0, -240)   # wheel up -> zoom in
    dash.wait_for_timeout(80)
    win = xwin(dash, "lv")
    assert span(win) < span(full)
    assert dash.evaluate("__chartsDebug.zoom('ul')") is None
    dash.mouse.wheel(0, 480)    # wheel down -> zoom back out (clamped to full)
    dash.wait_for_timeout(80)
    assert span(xwin(dash, "lv")) > span(win)


def test_keyboard_pan_zoom_reset(dash):
    hover_panel(dash, "lv")
    dash.keyboard.press("+")
    dash.wait_for_timeout(60)
    win = xwin(dash, "lv")
    assert span(win) < span(full_xwin(dash, "lv"))
    dash.keyboard.press("ArrowRight")
    dash.wait_for_timeout(60)
    assert xwin(dash, "lv")[0] > win[0]
    dash.keyboard.press("0")
    dash.wait_for_timeout(60)
    assert is_full(dash, "lv")


@pytest.mark.parametrize("key", ["pw", "kw", "growth"])
def test_other_time_panels_zoom_locally(dash, key):
    drag_panel(dash, key, 0.2, 0.7)
    assert dash.evaluate(f"__chartsDebug.zoom('{key}')") is not None, f"{key} did not zoom"
    assert dash.evaluate("__chartsDebug.zoom('lv')") is None, "zoom leaked into lv"
    box = panel_box(dash, key)
    dash.mouse.dblclick(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    dash.wait_for_timeout(80)
    assert dash.evaluate(f"__chartsDebug.zoom('{key}')") is None


def test_zoom_rerenders_series(dash):
    """Zooming re-decimates from the full arrays — the drawn path must change."""
    d_before = dash.evaluate(
        "document.querySelector('#panel-lv svg path[stroke]').getAttribute('d').length")
    dash.evaluate("(() => { const f = __chartsDebug.fullXwin('lv');"
                  "__chartsDebug.setWindow(f[0], f[0] + (f[1]-f[0])*0.25); })()")
    dash.wait_for_timeout(80)
    d_after = dash.evaluate(
        "document.querySelector('#panel-lv svg path[stroke]').getAttribute('d').length")
    assert d_before != d_after
