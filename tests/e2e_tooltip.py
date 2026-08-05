"""E2E: hover tooltips (line, bar, heatmap) and click-to-pin."""
from __future__ import annotations

import pytest
from harness import hover_panel

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("key", ["lv", "bv", "kw", "rt", "growth"])
def test_line_tooltip(dash, key):
    # A hover inside a data hole honestly reports no sample (rows: [] with a
    # hole tag) instead of snapping to one days away, and on a real dashboard
    # the nightly PC-off gaps cover much of the timeline — so probe a few
    # positions for one that actually carries data.
    hov = None
    for fx in (0.5, 0.55, 0.45, 0.6, 0.4, 0.65, 0.35, 0.7, 0.8, 0.9):
        hover_panel(dash, key, fx)
        hov = dash.evaluate(f"__chartsDebug.hover('{key}')")
        if hov and hov.get("rows"):
            break
    assert hov and "ts" in hov and hov["rows"], f"panel {key}: no hover snap"
    tt = dash.locator(".chart-tooltip:not(.is-pinned)")
    assert not tt.evaluate("el => el.hidden")
    text = tt.text_content()
    assert hov["rows"][0]["name"] in text


def test_bar_tooltip(dash):
    hover_panel(dash, "daily", fx=0.4, fy=0.6)
    hov = dash.evaluate("__chartsDebug.hover('daily')")
    assert hov and "label" in hov and "y" in hov
    assert not dash.locator(".chart-tooltip:not(.is-pinned)").evaluate("el => el.hidden")


def test_heatmap_tooltip(dash):
    hover_panel(dash, "hm", fx=0.6, fy=0.4)
    hov = dash.evaluate("__chartsDebug.hover('hm')")
    assert hov and "day" in hov and "hour" in hov
    text = dash.locator(".chart-tooltip:not(.is-pinned)").text_content()
    assert "mean power" in text


def test_tooltip_hides_on_leave(dash):
    hover_panel(dash, "lv")
    dash.mouse.move(2, 2)
    dash.wait_for_timeout(80)
    assert dash.locator(".chart-tooltip:not(.is-pinned)").evaluate("el => el.hidden")


def test_pin_and_unpin(dash):
    box = hover_panel(dash, "lv")
    dash.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    dash.wait_for_timeout(60)
    assert dash.evaluate("__chartsDebug.pinned()") == {"key": "lv"}
    assert not dash.locator(".chart-tooltip.is-pinned").evaluate("el => el.hidden")
    # The pinned copy survives hovering elsewhere...
    hover_panel(dash, "ul")
    assert dash.evaluate("__chartsDebug.pinned()") == {"key": "lv"}
    # ...and Escape clears it.
    dash.keyboard.press("Escape")
    dash.wait_for_timeout(50)
    assert dash.evaluate("__chartsDebug.pinned()") is None
    assert dash.locator(".chart-tooltip.is-pinned").evaluate("el => el.hidden")


def test_second_click_unpins(dash):
    box = hover_panel(dash, "lv")
    x, y = box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5
    dash.mouse.click(x, y)
    dash.wait_for_timeout(50)
    assert dash.evaluate("__chartsDebug.pinned()") == {"key": "lv"}
    dash.mouse.click(x, y)
    dash.wait_for_timeout(50)
    assert dash.evaluate("__chartsDebug.pinned()") is None
