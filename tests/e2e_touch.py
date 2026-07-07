"""E2E: touch support, on a touch-emulating context.

The contract on a touch screen: a hesitant vertical swipe stays a page
scroll (gesture arbitration via a horizontal-intent threshold), a decisive
horizontal drag zoom-selects, two fingers pinch-zoom around their midpoint,
a tap pins the tooltip for the tapped sample, and the card tools are always
visible because there is no hover.

Multi-point gestures go through the Chrome DevTools Protocol
(Input.dispatchTouchEvent); Playwright's high-level API only offers tap.
"""
from __future__ import annotations

import pytest
from harness import span, wait_ready

pytestmark = pytest.mark.e2e


@pytest.fixture()
def touch_page(_browser, dashboard_path):
    ctx = _browser.new_context(has_touch=True, viewport={"width": 900, "height": 1400})
    page = ctx.new_page()
    page.goto(dashboard_path.resolve().as_uri())
    wait_ready(page)
    yield page
    ctx.close()


def _panel_box(page, key):
    loc = page.locator(f"#panel-{key} svg")
    loc.scroll_into_view_if_needed()
    box = loc.bounding_box()
    assert box is not None
    return box


def _touch_drag(page, points_from, points_to, steps=8):
    cdp = page.context.new_cdp_session(page)
    try:
        cdp.send("Input.dispatchTouchEvent", {
            "type": "touchStart",
            "touchPoints": [{"x": x, "y": y} for x, y in points_from]})
        for i in range(1, steps + 1):
            cur = [{"x": fx + (tx - fx) * i / steps, "y": fy + (ty - fy) * i / steps}
                   for (fx, fy), (tx, ty) in zip(points_from, points_to, strict=True)]
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": cur})
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    finally:
        cdp.detach()


def test_coarse_pointer_always_shows_tools(touch_page):
    assert touch_page.evaluate("matchMedia('(pointer: coarse)').matches")
    opacity = touch_page.evaluate(
        "getComputedStyle(document.querySelector('.card-tools')).opacity")
    assert opacity == "1", "card tools must be visible without hover on touch"


def test_horizontal_drag_zooms_vertical_does_not(touch_page):
    page = touch_page
    box = _panel_box(page, "lv")
    x, y = box["x"] + box["width"] * 0.3, box["y"] + box["height"] * 0.5
    # A vertical swipe is a scroll, never a zoom claim.
    _touch_drag(page, [(x, y)], [(x, y - 140)])
    page.wait_for_timeout(120)
    assert not page.evaluate("__chartsDebug.isZoomed('lv')")
    # A decisive horizontal drag zoom-selects (re-read the box: the vertical
    # swipe scrolled the page).
    box = _panel_box(page, "lv")
    y = box["y"] + box["height"] * 0.5
    _touch_drag(page, [(box["x"] + box["width"] * 0.3, y)],
                [(box["x"] + box["width"] * 0.7, y)])
    page.wait_for_timeout(120)
    assert page.evaluate("__chartsDebug.isZoomed('lv')")


def test_pinch_zooms_in(touch_page):
    page = touch_page
    full = page.evaluate("__chartsDebug.fullXwin('lv')")
    box = _panel_box(page, "lv")
    cx, y = box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5
    _touch_drag(page, [(cx - 40, y), (cx + 40, y)],
                [(cx - 160, y), (cx + 160, y)])
    page.wait_for_timeout(120)
    assert page.evaluate("__chartsDebug.isZoomed('lv')"), "pinch did not zoom"
    win = page.evaluate("__chartsDebug.xwin('lv')")
    assert span(win) < span(full)


def test_pinch_close_zooms_out(touch_page):
    """The discriminating pinch case: with the panel already zoomed, closing
    the fingers must WIDEN the window. A drag-selection misread of the
    gesture can only ever narrow it."""
    page = touch_page
    page.evaluate("(() => { const f = __chartsDebug.fullXwin('lv');"
                  "const s = f[1] - f[0];"
                  "__chartsDebug.setWindow(f[0] + s * 0.35, f[0] + s * 0.65); })()")
    page.wait_for_timeout(80)
    before = page.evaluate("__chartsDebug.xwin('lv')")
    box = _panel_box(page, "lv")
    cx, y = box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5
    _touch_drag(page, [(cx - 160, y), (cx + 160, y)],
                [(cx - 40, y), (cx + 40, y)])
    page.wait_for_timeout(120)
    after = page.evaluate("__chartsDebug.xwin('lv')")
    assert span(after) > span(before), "closing pinch did not zoom out"


def test_tap_pins_tooltip(touch_page):
    page = touch_page
    box = _panel_box(page, "lv")
    x, y = box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5
    page.touchscreen.tap(x, y)
    page.wait_for_timeout(120)
    pinned = page.evaluate("__chartsDebug.pinned()")
    assert pinned and pinned["key"] == "lv"
    page.touchscreen.tap(x, y)
    page.wait_for_timeout(120)
    assert page.evaluate("__chartsDebug.pinned()") is None
