"""Shared constants and Playwright helpers for the E2E suites.

The dashboard exposes a deterministic test surface at ``window.__chartsDebug``
(see pcss/charts.js): ``ready()`` gates on the load reveal finishing,
``resetAll()`` restores the pristine state between tests without a page
reload (which keeps the parallel suite fast and order-independent under
pytest-xdist), and ``window()/zoom()/hover()/pinned()/hidden()/lightbox()``
report interaction state so the suites can assert on data instead of pixels.
"""
from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Page

# output/dashboard.html lives one level up from tests/ (used only when
# STATEOFUPS_E2E_REAL=1 points the suites at a real generated dashboard).
DASHBOARD = Path(__file__).resolve().parent.parent / "output" / "dashboard.html"

# Panel keys, kept in sync with the payload built by pcss/dashboard.py
# (the conftest session fixture asserts the page agrees). "ev" is the event
# timeline, a categorical-row time panel.
PANELS = ["lv", "ul", "pw", "hm", "bv", "bc", "rt", "kw", "daily", "cmp", "wk",
          "growth", "proj", "cad", "ev"]

# The crosshair-sync group: hovering any of these mirrors the crosshair at
# the same timestamp on the others. Zoom and pan are strictly per panel. The
# event timeline "ev" is deliberately NOT here — its y axis is a category
# list, not a shared value scale, so a mirrored crosshair would be meaningless.
SYNC_PANELS = ["lv", "ul", "pw", "bv", "bc", "kw"]

# Every panel with a time x-axis (local zoom; presets address all of them).
# "ev" zooms and pans locally like the others even though it is not a line
# panel, so it joins here.
TIME_PANELS = [*SYNC_PANELS, "growth", "ev"]

# Panels by chart kind (drives tooltip/export parametrization).
LINE_PANELS = ["lv", "ul", "pw", "bv", "bc", "rt", "kw", "cmp", "wk", "growth", "proj"]
BAR_PANELS = ["daily", "cad"]
HEATMAP_PANELS = ["hm"]

# Non-sync panels that still own a local drag zoom.
LOCAL_ZOOM_PANELS = ["growth"]


def svg_js(key: str) -> str:
    """JS expression for one panel's rendered SVG element."""
    return f"document.querySelector('#panel-{key} svg')"


def wait_ready(page: Page, timeout_ms: int = 20_000) -> None:
    """Block until charts.js has rendered every panel and finished the load
    reveal (or skipped it under ?noanim=1 / reduced motion)."""
    page.wait_for_function("window.__chartsDebug && window.__chartsDebug.ready()",
                           timeout=timeout_ms)


def panel_box(page: Page, key: str) -> dict:
    """Viewport bounding box of one panel's SVG, scrolled into view first —
    page.mouse events only land inside the viewport, and the lower panels sit
    below the initial fold."""
    loc = page.locator(f"#panel-{key} svg")
    loc.scroll_into_view_if_needed()
    box = loc.bounding_box()
    assert box is not None, f"panel {key} has no rendered svg"
    return box


def hover_panel(page: Page, key: str, fx: float = 0.5, fy: float = 0.5) -> dict:
    """Move the mouse to a fractional position inside a panel and let the
    hover handlers run. Returns the bounding box used."""
    box = panel_box(page, key)
    page.mouse.move(box["x"] + box["width"] * fx, box["y"] + box["height"] * fy)
    page.wait_for_timeout(60)
    return box


def drag_panel(page: Page, key: str, fx0: float, fx1: float, fy: float = 0.5) -> None:
    """Horizontal drag across a panel (zoom-select at full range, pan when
    zoomed)."""
    box = panel_box(page, key)
    y = box["y"] + box["height"] * fy
    page.mouse.move(box["x"] + box["width"] * fx0, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * fx1, y, steps=6)
    page.mouse.up()
    page.wait_for_timeout(80)


def xwin(page: Page, key: str) -> list[float]:
    """One panel's current x-window (its local zoom, or its full data span)."""
    return page.evaluate(f"__chartsDebug.xwin('{key}')")


def full_xwin(page: Page, key: str) -> list[float]:
    """One panel's full data span."""
    return page.evaluate(f"__chartsDebug.fullXwin('{key}')")


def span(window: list[float]) -> float:
    return window[1] - window[0]


def is_full(page: Page, key: str, tol_ms: float = 1000) -> bool:
    win, full = xwin(page, key), full_xwin(page, key)
    return abs(win[0] - full[0]) < tol_ms and abs(win[1] - full[1]) < tol_ms
