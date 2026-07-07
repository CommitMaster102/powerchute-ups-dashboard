"""E2E: initial render. Every panel has a populated SVG, the KPI row and
health pill are filled in, and the reference tables carry data."""
from __future__ import annotations

import pytest
from harness import PANELS, svg_js

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("key", PANELS)
def test_panel_renders_svg(dash, key):
    assert dash.evaluate(f"!!{svg_js(key)}"), f"panel {key}: no svg"
    marks = dash.evaluate(f"{svg_js(key)}.querySelectorAll('path, rect, circle').length")
    assert marks > 0, f"panel {key}: svg has no marks"


def test_kpi_row(dash):
    cards = dash.locator(".kpi-card")
    assert cards.count() == 5
    values = dash.eval_on_selector_all(".kpi-value", "els => els.map(e => e.textContent)")
    assert all(v.strip() for v in values)
    pills = dash.eval_on_selector_all(".kpi-pill", "els => els.map(e => e.textContent)")
    assert all(p in ("OK", "WARN", "ALERT", "LIVE") for p in pills)
    # Sparklines render for the data-backed KPIs.
    assert dash.evaluate("document.querySelectorAll('.kpi-spark svg').length") >= 3


def test_health_pill_and_header(dash):
    assert dash.locator(".health-label").text_content().strip()
    assert dash.locator(".health-sub").text_content().strip()
    assert "PowerChute Serial Shutdown" in dash.locator(".header-sub").text_content()
    # The staleness badge is filled in by charts.js at load time.
    assert "last sample" in dash.locator("#staleness").text_content()


def test_reference_tables(dash):
    rows = dash.evaluate("document.querySelectorAll('.stats-table tbody tr').length")
    assert rows >= 4  # Line Voltage, Battery Voltage, UPS Load, Battery Capacity
    sums = dash.evaluate("document.querySelectorAll('.sum-row').length")
    assert sums >= 15  # latest sample + energy + files + growth + anomalies
    # Anomalies from the synthetic logs must be reported.
    text = dash.locator(".sum-grid").text_content()
    assert "Voltage out-of-range" in text


def test_gap_shading_present(dash):
    # The synthetic DataLog has one 2-hour gap; the DataLog panels mark it
    # with a slim strip along the x-axis.
    strips = dash.evaluate(
        "document.querySelectorAll('#panel-lv svg rect.gap-strip').length")
    assert strips >= 1


def test_on_battery_shading_present(dash):
    # The synthetic DataLog has one corroborated on-battery episode (voltage
    # collapse + capacity drop); the DataLog panels mark it with an amber
    # strip just above the gap strip.
    strips = dash.evaluate(
        "document.querySelectorAll('#panel-lv svg rect.ep-strip').length")
    assert strips >= 1


def test_anomaly_markers_present(dash):
    """The synthetic DataLog has two out-of-envelope samples; each renders as
    an X marker made of two red diagonal strokes on the Line Voltage panel."""
    red = dash.evaluate(
        "Array.from(document.querySelectorAll('#panel-lv svg line'))"
        ".filter(l => l.getAttribute('stroke-width') === '2').length")
    assert red >= 4


def test_annotation_marker_present(dash):
    """The synthetic fixture (tests/conftest.py) writes one battery_replaced
    annotation inside the synthesized DataLog's range; a dashed vertical
    marker with the entry's label must render on a time-axis panel."""
    labels = dash.locator("#panel-lv svg text.annotation-label")
    assert labels.count() >= 1
    assert labels.first.text_content().strip() == "New battery installed"
    lines = dash.evaluate(
        "document.querySelectorAll('#panel-lv svg line.annotation-marker').length")
    assert lines >= 1
