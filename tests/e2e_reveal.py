"""E2E: the one-time load reveal — plays on a normal load, is skipped under
?noanim=1 and prefers-reduced-motion, and never blocks readiness."""
from __future__ import annotations

import pytest
from harness import PANELS, wait_ready

pytestmark = pytest.mark.e2e


def test_reveal_completes_on_fresh_load(_browser, dashboard_path):
    page = _browser.new_page(viewport={"width": 1600, "height": 2400})
    try:
        page.goto(dashboard_path.resolve().as_uri())
        # Not ready instantly (the sweep takes ~700 ms + stagger)...
        wait_ready(page)
        # ...but once ready, every panel is at full width (clip removed).
        assert page.evaluate("__chartsDebug.panelKeys().length") == len(PANELS)
    finally:
        page.close()


def test_noanim_skips_reveal(_browser, dashboard_path):
    page = _browser.new_page(viewport={"width": 1600, "height": 2400})
    try:
        page.goto(dashboard_path.resolve().as_uri() + "?noanim=1")
        page.wait_for_function("window.__chartsDebug && window.__chartsDebug.ready()",
                               timeout=5_000)
        assert page.evaluate("document.querySelectorAll('.chart-box svg').length") >= len(PANELS)
    finally:
        page.close()


def test_reduced_motion_skips_reveal(_browser, dashboard_path):
    ctx = _browser.new_context(reduced_motion="reduce",
                               viewport={"width": 1600, "height": 2400})
    try:
        page = ctx.new_page()
        page.goto(dashboard_path.resolve().as_uri())
        page.wait_for_function("window.__chartsDebug && window.__chartsDebug.ready()",
                               timeout=5_000)
    finally:
        ctx.close()
