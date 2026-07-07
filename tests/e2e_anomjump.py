"""E2E: anomaly jump navigation.

The Line Voltage card grows a small flag button when out-of-envelope samples
exist. Each click zooms the panel to a few hours around the next anomaly
cluster (nearby anomalies are framed as one view), cycling through them.
"""
from __future__ import annotations

import pytest
from harness import xwin

pytestmark = pytest.mark.e2e


def test_button_only_where_anomalies_exist(dash):
    assert dash.locator('.tool-anom[data-panel="lv"]').count() == 1
    assert dash.locator('.tool-anom[data-panel="ul"]').count() == 0


def test_click_cycles_through_anomaly_clusters(dash):
    markers = dash.evaluate(
        "__chartsDebug.panelKeys().includes('lv') && "
        "(() => { const s = document.querySelector('#panel-lv svg'); return true; })()")
    assert markers
    btn = dash.locator('.tool-anom[data-panel="lv"]')
    first_marker_x = dash.evaluate(
        "(() => { const d = window.__chartsDebug; return d.anomalyClusters('lv')[0]; })()")

    btn.click()
    dash.wait_for_timeout(80)
    assert dash.evaluate("__chartsDebug.isZoomed('lv')")
    win1 = xwin(dash, "lv")
    assert win1[0] <= first_marker_x[0] and first_marker_x[1] <= win1[1], \
        "first jump does not frame the first anomaly cluster"
    # The zoom stays local to lv.
    assert dash.evaluate("__chartsDebug.zoom('ul')") is None

    btn.click()
    dash.wait_for_timeout(80)
    win2 = xwin(dash, "lv")
    assert win2 != win1, "second click did not advance to the next cluster"

    # The synthetic data has two clusters, so a third click wraps around.
    btn.click()
    dash.wait_for_timeout(80)
    win3 = xwin(dash, "lv")
    assert win3 == pytest.approx(win1, abs=1000)
