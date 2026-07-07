"""E2E: marker labels stay at or below the plot's top padding (item B4).

A flagged-day marker sits above the bar it flags, at ``y - 13``. On the
tallest bar that nudge would push the label into (or above) the top padding,
so it is clamped to stay at or below ``pad.t + a small margin``. This suite
builds a dedicated dashboard whose flagged day is also the biggest bar (the
one case that exercises the clamp) rather than relying on the shared fixture,
following the same dedicated-build pattern the cmpselect and baseline suites
use.
"""
from __future__ import annotations

import pandas as pd
import pytest
from harness import wait_ready

pytestmark = pytest.mark.e2e

# renderBar's default top padding (pad.t) in viewBox units; the clamp keeps the
# marker-label baseline at or below pad.t + 12.
_BAR_PAD_T = 16


@pytest.fixture(scope="module")
def markerclamp_dashboard_path(tmp_path_factory):
    """Three days of energy where the last day dwarfs the others and is the
    flagged one, so its bar is the tallest and its deviation marker is what the
    clamp has to keep inside the plot."""
    from pcss.dashboard import build_dashboard
    from pcss.stats import compute_energy_summary

    rows = []
    for day, watts in [("2026-02-01", 100.0), ("2026-02-02", 100.0), ("2026-02-03", 2000.0)]:
        base = pd.Timestamp(day)
        for h in range(24):
            rows.append((base + pd.Timedelta(hours=h), watts))
    edf = pd.DataFrame(rows, columns=["ts", "power_w"])
    edf["interval_sec"] = 3600
    es = compute_energy_summary(edf)

    flagged = pd.DataFrame([{"date": pd.Timestamp("2026-02-03").date(),
                             "day_type": "weekday", "deviation_pct": 300.0}])
    baseline = {"status": "ok", "min_days": 14, "n_days": 3, "deviation_pct": 35.0,
                "flagged": flagged}

    empty = pd.DataFrame()
    html = build_dashboard(
        datalog_df=empty, energy_df=edf, hist=empty, dl_stats={}, hist_stats={},
        sizes={"DataLog": 0, "EventLog (binary)": 0, "energylog/": 0},
        energy_summary=es, stats_table=empty, gaps=empty, voltage_anomalies=empty,
        high_load_episodes=empty, crossval={}, baseline=baseline,
    )
    out = tmp_path_factory.mktemp("markerclamp-e2e") / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    return out


def test_flagged_bar_marker_label_stays_below_top_padding(_browser, markerclamp_dashboard_path):
    page = _browser.new_page(viewport={"width": 1600, "height": 2400})
    page.goto(markerclamp_dashboard_path.resolve().as_uri())
    wait_ready(page)
    try:
        # The marker label is the only bold (font-weight 600) text in the bar
        # svg; its baseline y attribute is set deterministically by the clamp.
        y_attr = page.evaluate(
            "() => { const t = document.querySelector("
            "'#panel-daily svg text[font-weight=\"600\"]');"
            " return t ? parseFloat(t.getAttribute('y')) : null; }")
        assert y_attr is not None, "flagged-bar marker label not rendered"
        # Without the clamp the label would land near y = pad.t + 2 (~18);
        # clamped, it stays at or below pad.t + 12 (28). pad.t + 10 sits cleanly
        # between the two.
        assert y_attr >= _BAR_PAD_T + 10, \
            f"marker label baseline {y_attr} pokes into the top padding"
    finally:
        page.close()
