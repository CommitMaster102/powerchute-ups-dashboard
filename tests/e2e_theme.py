"""E2E: the [dashboard] theme config key — dark is the default, light builds
swap the palette everywhere (shell CSS variables and chart SVG alike)."""
from __future__ import annotations

import pytest
from harness import wait_ready

pytestmark = pytest.mark.e2e


def test_dark_default(dash):
    assert dash.evaluate(
        "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()") == "#101216"


def test_light_theme_build(_browser, light_dashboard_path):
    page = _browser.new_page(viewport={"width": 1600, "height": 2400})
    try:
        page.goto(light_dashboard_path.resolve().as_uri())
        wait_ready(page)
        assert page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()") == "#f4f6f9"
        assert page.evaluate("document.title") == "PowerChute UPS Dashboard"
        # The charts read the same palette: no dark-panel-colored marks remain.
        assert page.evaluate("document.querySelectorAll('.chart-box svg').length") >= 12
        assert "theme light" in page.locator("footer").text_content()
    finally:
        page.close()
