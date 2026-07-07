"""E2E: baseline-deviation markers on the Daily Energy panel (roadmap item
19).

The shared hermetic fixture's energylog spans only about four days — well
under the 14-day baseline floor — so it can never produce a flagged day and
must not be reshaped for this suite. Following the dedicated-build pattern
the theme suite already uses (`tests/e2e_theme.py` opens its own page from
the session browser against a second build), this module builds a dashboard
directly from synthetic frames with 21 weekdays of energylog and one clearly
deviant (stuck-on-appliance) day, opens it from the session browser, and
asserts the flagged day renders as a real marker glyph on the daily panel —
not just as payload data.
"""
from __future__ import annotations

import pytest
from harness import wait_ready

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def baseline_dashboard_path(tmp_path_factory):
    """A dashboard built from synthetic frames whose energylog spans 21
    weekdays with a stuck-on-appliance day at index 10 (flat 700 W against
    a 100/300 W night/day baseline) — enough history to clear the 14-day
    floor and deviant enough to clear the 35% threshold."""
    import pandas as pd

    from pcss.dashboard import build_dashboard
    from pcss.stats import compute_energy_summary

    dates = list(pd.bdate_range("2026-01-05", periods=21))
    rows = []
    for i, d in enumerate(dates):
        base = pd.Timestamp(d)
        for h in range(24):
            watts = 700.0 if i == 10 else (100.0 if h < 12 else 300.0)
            rows.append((base + pd.Timedelta(hours=h), watts))
    edf = pd.DataFrame(rows, columns=["ts", "power_w"])
    edf["interval_sec"] = 3600

    empty = pd.DataFrame()
    html = build_dashboard(
        datalog_df=empty, energy_df=edf, hist=empty,
        dl_stats={}, hist_stats={},
        sizes={"DataLog": 0, "EventLog (binary)": 0, "energylog/": 0},
        energy_summary=compute_energy_summary(edf), stats_table=empty,
        gaps=empty, voltage_anomalies=empty, high_load_episodes=empty,
        crossval={},
    )
    out = tmp_path_factory.mktemp("baseline-e2e") / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    return out


def test_daily_panel_renders_deviation_marker(_browser, baseline_dashboard_path):
    """The flagged day must render as a marker glyph on the Daily Energy
    bars — a dot circle above the bar with the deviation-percent label —
    plus the amber bar recolor; the two reinforce each other."""
    page = _browser.new_page(viewport={"width": 1600, "height": 2400})
    try:
        page.goto(baseline_dashboard_path.resolve().as_uri())
        wait_ready(page)
        # The marker glyph: renderBar draws a "dot" marker as a circle.
        circles = page.evaluate(
            "document.querySelectorAll('#panel-daily svg circle').length")
        assert circles >= 1, "no marker glyph rendered on the daily panel"
        # The glyph's deviation-percent label rides just above it.
        labels = page.eval_on_selector_all(
            "#panel-daily svg text",
            "els => els.map(e => e.textContent).filter(t => /^\\d+%$/.test(t))")
        assert labels, "no deviation-percent label rendered on the daily panel"
        # The amber bar recolor is still there alongside the glyph: exactly
        # one bar (the flagged day) carries a fill different from the rest.
        fills = page.eval_on_selector_all(
            "#panel-daily svg rect[data-bar]",
            "els => els.map(e => e.getAttribute('fill'))")
        assert len(set(fills)) == 2, f"expected one recolored bar, fills: {set(fills)}"
    finally:
        page.close()
