"""E2E: the card-expand lightbox — open, interact inside, close."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_expand_button_opens_lightbox(dash):
    dash.evaluate("document.querySelector('.tool-expand[data-panel=lv]').click()")
    dash.wait_for_timeout(120)
    assert dash.evaluate("__chartsDebug.lightbox()") == "lv"
    assert not dash.locator("#lightbox").evaluate("el => el.hidden")
    assert dash.evaluate("!!document.querySelector('#lightbox-chart svg')")
    assert dash.locator("#lightbox-title").text_content() == "Line Voltage"


def test_lightbox_chart_is_interactive(dash):
    dash.evaluate("__chartsDebug.openLightbox('lv')")
    dash.wait_for_timeout(120)
    box = dash.locator("#lightbox-chart svg").bounding_box()
    dash.mouse.move(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    dash.wait_for_timeout(80)
    hov = dash.evaluate("__chartsDebug.hover('lv')")
    assert hov and "ts" in hov, "hover inside the lightbox did not snap"


def test_lightbox_zoom_stays_on_its_panel(dash):
    """The lightbox is a second view of the same panel state: zooming inside
    it zooms that panel (both views) and nothing else."""
    dash.evaluate("__chartsDebug.openLightbox('lv')")
    dash.wait_for_timeout(120)
    box = dash.locator("#lightbox-chart svg").bounding_box()
    y = box["y"] + box["height"] * 0.5
    dash.mouse.move(box["x"] + box["width"] * 0.3, y)
    dash.mouse.down()
    dash.mouse.move(box["x"] + box["width"] * 0.7, y, steps=6)
    dash.mouse.up()
    dash.wait_for_timeout(100)
    assert dash.evaluate("__chartsDebug.isZoomed('lv')")
    assert not dash.evaluate("__chartsDebug.isZoomed('ul')"), "zoom leaked to another panel"


def test_escape_closes_lightbox(dash):
    dash.evaluate("__chartsDebug.openLightbox('hm')")
    dash.wait_for_timeout(100)
    assert dash.evaluate("__chartsDebug.lightbox()") == "hm"
    dash.keyboard.press("Escape")
    dash.wait_for_timeout(80)
    assert dash.evaluate("__chartsDebug.lightbox()") is None
    assert dash.locator("#lightbox").evaluate("el => el.hidden")


def test_close_button_and_backdrop(dash):
    dash.evaluate("__chartsDebug.openLightbox('daily')")
    dash.wait_for_timeout(100)
    dash.click(".lightbox-close")
    dash.wait_for_timeout(80)
    assert dash.evaluate("__chartsDebug.lightbox()") is None
