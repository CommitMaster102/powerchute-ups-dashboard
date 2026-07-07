"""E2E: battery-lifecycle annotation labels stay inside the plot (item B1).

The hermetic fixture carries one battery_replaced annotation (2026-06-29).
Zoomed so it sits near the window's right edge, its label must flip to an
end-anchor and stay within the plot rather than overflowing past the right
padding off the chart.
"""
from __future__ import annotations

import pytest
from harness import panel_box

pytestmark = pytest.mark.e2e


def test_annotation_label_stays_within_plot_near_right_edge(dash):
    # The fixture annotation is at 2026-06-29 00:00, encoded as wall-clock-as-
    # UTC epoch-ms just like every timestamp on the payload.
    anno_ms = dash.evaluate("Date.UTC(2026, 5, 29, 0, 0, 0)")
    # Put the annotation near the window's right edge (about 92% across).
    dash.evaluate(f"__chartsDebug.setWindow({anno_ms} - 12*3600e3, {anno_ms} + 3600e3)")
    dash.wait_for_timeout(80)

    svg_box = panel_box(dash, "lv")   # scrolls the panel into view, returns its box
    labels = dash.locator("#panel-lv svg text.annotation-label")
    assert labels.count() >= 1, "annotation label not rendered near the right edge"
    lbl_box = labels.first.bounding_box()
    assert lbl_box is not None
    # The label's right edge must not spill past the panel's right edge.
    assert lbl_box["x"] + lbl_box["width"] <= svg_box["x"] + svg_box["width"] + 1, \
        "annotation label overflows the plot's right edge"
