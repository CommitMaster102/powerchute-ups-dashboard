"""E2E: the event timeline panel (roadmap item 20).

A fourth chart shape — one categorical row per event category, a dot per
occurrence, time on the x axis. It joins the standard time-window machinery
but not the crosshair-sync group (its y axis is categorical). These suites
pin the behavior that is specific to this shape: the default filter (power
and battery on, communication/monitoring housekeeping off), the per-dot
hover tooltip, and the machine-standard CSV export. The generic behaviors
(render, zoom isolation, permalink, reveal) are covered by the shared,
PANELS-parametrized suites, which "ev" now joins.
"""
from __future__ import annotations

import pytest
from harness import panel_box

pytestmark = pytest.mark.e2e


def _first_dot_client_xy(page):
    """Client-space center of the first visible dot on the ev panel, computed
    from its viewBox coordinates so the mouse can be moved exactly onto it."""
    panel_box(page, "ev")  # scroll into view first
    return page.evaluate(
        "(() => {"
        "  const c = document.querySelector('#panel-ev svg circle.ev-dot');"
        "  if (!c) return null;"
        "  const svg = c.ownerSVGElement, r = svg.getBoundingClientRect(),"
        "        vb = svg.viewBox.baseVal;"
        "  const cx = +c.getAttribute('cx'), cy = +c.getAttribute('cy');"
        "  return { x: r.left + cx / vb.width * r.width,"
        "           y: r.top + cy / vb.height * r.height };"
        "})()"
    )


def _click_legend(dash, cat):
    # Dispatch the click straight on the legend row, the same way the line
    # legend suite does (e2e_export.py): a real pointer click inside a
    # zoom-capable chart box is claimed by the drag/pin gesture machinery.
    dash.evaluate(
        "document.querySelector(\"#panel-ev .ev-legend[data-cat='" + cat + "']\")"
        ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    dash.wait_for_timeout(80)


def test_default_filter_hides_communication_until_toggled(dash):
    """Power and battery ship visible; the communication churn ships hidden.
    Its legend row is present but dimmed, and its dots appear only once the
    legend entry is clicked (the roadmap noise point)."""
    leg = dash.locator("#panel-ev .ev-legend[data-cat='communication']")
    assert leg.count() == 1, "communication legend row missing"
    assert leg.evaluate("el => el.getAttribute('opacity')") == "0.4", \
        "communication row should be dimmed (hidden) by default"
    dots_before = dash.evaluate(
        "document.querySelectorAll('#panel-ev svg circle.ev-dot').length")
    _click_legend(dash, "communication")
    leg = dash.locator("#panel-ev .ev-legend[data-cat='communication']")
    assert leg.evaluate("el => el.getAttribute('opacity')") in (None, "1"), \
        "communication row should un-dim after clicking its legend entry"
    dots_after = dash.evaluate(
        "document.querySelectorAll('#panel-ev svg circle.ev-dot').length")
    assert dots_after > dots_before, \
        "communication dots did not appear after toggling its row on"


def test_power_row_visible_by_default(dash):
    """The signal categories are on at load: the power row is not dimmed and
    the panel already shows dots without any interaction."""
    power = dash.locator("#panel-ev .ev-legend[data-cat='power']")
    assert power.count() == 1
    assert power.evaluate("el => el.getAttribute('opacity')") in (None, "1")
    assert dash.evaluate(
        "document.querySelectorAll('#panel-ev svg circle.ev-dot').length") > 0


def test_hovering_a_dot_shows_event_tooltip(dash):
    """Hovering a dot snaps to that event and shows a tooltip with the event's
    name and timestamp (click pins, Esc unpins — the shared contract)."""
    xy = _first_dot_client_xy(dash)
    assert xy, "no visible dot on the ev panel"
    dash.mouse.move(xy["x"], xy["y"])
    dash.wait_for_timeout(80)
    hov = dash.evaluate("__chartsDebug.hover('ev')")
    assert hov and "ts" in hov and hov.get("name"), "hover did not snap to an event"
    tt = dash.locator(".chart-tooltip:not(.is-pinned)")
    assert not tt.evaluate("el => el.hidden"), "tooltip not shown on dot hover"
    assert hov["name"] in tt.text_content()
    # Clicking pins it; Escape clears it.
    dash.mouse.click(xy["x"], xy["y"])
    dash.wait_for_timeout(60)
    assert dash.evaluate("__chartsDebug.pinned()") == {"key": "ev"}
    dash.keyboard.press("Escape")
    dash.wait_for_timeout(50)
    assert dash.evaluate("__chartsDebug.pinned()") is None


def test_csv_export_has_event_columns(dash, tmp_path):
    """The CSV export is machine-standard en-US with columns for timestamp,
    category, event id, and event name."""
    with dash.expect_download() as dl:
        dash.evaluate("document.querySelector('.tool-csv[data-panel=ev]').click()")
    d = dl.value
    assert d.suggested_filename == "ups-ev.csv"
    path = tmp_path / d.suggested_filename
    d.save_as(path)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "time,category,event id,event name"
    assert len(lines) >= 2, "csv has no data rows"
    # The default-visible power events must be present, with the stable
    # (un-localized) category key and the ObjectId.
    body = "\n".join(lines[1:])
    assert "power" in body
    assert "3.5.1.5.4.1" in body
