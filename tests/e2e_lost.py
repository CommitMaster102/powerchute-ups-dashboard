"""E2E: lost-telemetry rendering (broken lines, estimated band, honest tooltip).

The shared hermetic fixture stays null-free on purpose (every other suite
expects a clean page), so this module builds its own dashboard from synthetic
frames, following the dedicated-build pattern e2e_baseline uses. The frames
carry two different holes: a monitoring-off hole (no rows in either log, the
PC off) and a link-down hole (DataLog silent, energylog writing null-power
rows), so the suite can assert the two render and report differently.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from harness import panel_box, wait_ready

pytestmark = pytest.mark.e2e

# Hole A: monitoring off (no rows at all in either log).
GAP_START, GAP_END = "2026-05-04 12:00", "2026-05-04 20:00"
# Hole B: UPS link down (DataLog silent, energylog null rows).
LOST_START, LOST_END = "2026-05-05 08:00", "2026-05-06 10:00"


@pytest.fixture(scope="module")
def lost_dashboard_path(tmp_path_factory):
    import numpy as np
    import pandas as pd

    from pcss.dashboard import build_dashboard
    from pcss.stats import (
        compute_energy_summary,
        detect_gaps,
        detect_lost_windows,
        reconstruct_lost_windows,
    )

    start = pd.Timestamp("2026-05-04 00:00")     # a Monday, 4 days of data
    gap0, gap1 = pd.Timestamp(GAP_START), pd.Timestamp(GAP_END)
    lost0, lost1 = pd.Timestamp(LOST_START), pd.Timestamp(LOST_END)

    rows = []
    for i in range(288):                          # 20-min cadence
        t = start + pd.Timedelta(minutes=20 * i)
        if gap0 <= t < gap1 or lost0 <= t <= lost1:
            continue
        rows.append({"ts": t, "Line Voltage": 118.0 + (i % 5),
                     "Battery Voltage": 27.4, "UPS Load": 15.0 + (i % 7),
                     "Battery Capacity": 100.0})
    dl = pd.DataFrame(rows)

    erows = []
    for i in range(4 * 288):                      # 5-min cadence
        t = start + pd.Timedelta(minutes=5 * i)
        if gap0 <= t < gap1:
            continue                              # monitoring off: no rows
        in_lost = lost0 <= t <= lost1
        erows.append({"ts": t,
                      "power_w": np.nan if in_lost else 240.0 + (i % 9),
                      "load_pct": np.nan if in_lost else 17.0,
                      "interval_sec": 300})
    edf = pd.DataFrame(erows)

    summary = compute_energy_summary(edf)
    stretches = detect_lost_windows(edf)
    assert len(stretches) == 1, "fixture must produce exactly one lost stretch"
    lost = reconstruct_lost_windows(stretches, dl, edf, summary)
    empty = pd.DataFrame()
    html = build_dashboard(
        datalog_df=dl, energy_df=edf, hist=empty, dl_stats={}, hist_stats={},
        sizes={"DataLog": 0, "EventLog (binary)": 0, "energylog/": 0},
        energy_summary=summary, stats_table=empty,
        gaps=detect_gaps(dl), voltage_anomalies=empty,
        high_load_episodes=empty, crossval={}, lost=lost)
    out = tmp_path_factory.mktemp("lost-e2e") / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def lost_page(_browser, lost_dashboard_path):
    page = _browser.new_page(viewport={"width": 1600, "height": 2400})
    page.goto(lost_dashboard_path.resolve().as_uri())
    wait_ready(page)
    yield page
    page.close()


def _ms(y, mo, d, h, mi=0):
    """Epoch-ms under the payload contract (naive local encoded as if UTC)."""
    return int(datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


def _fx(page, key, ts_ms, pad_l=46, pad_r=16, w=820):
    """Fractional x inside a panel's svg box for a timestamp, mapping the
    plot-area padding of its 820-wide viewBox."""
    full = page.evaluate(f"__chartsDebug.fullXwin('{key}')")
    frac = (ts_ms - full[0]) / (full[1] - full[0])
    return (pad_l + frac * (w - pad_l - pad_r)) / w


def _hover_at(page, key, ts_ms):
    box = panel_box(page, key)
    fx = _fx(page, key, ts_ms)
    page.mouse.move(box["x"] + box["width"] * fx, box["y"] + box["height"] * 0.5)
    page.wait_for_timeout(80)


def test_lost_span_in_debug_surface(lost_page):
    lost = lost_page.evaluate("__chartsDebug.lost()")
    assert len(lost) == 1
    assert lost[0][0] == _ms(2026, 5, 5, 8)
    assert lost[0][1] == _ms(2026, 5, 6, 10)


def test_band_mean_and_strip_render_on_opted_panels(lost_page):
    for k in ("lv", "ul", "pw"):
        for cls in ("recon-band", "recon-mean", "lost-strip", "recon-chip"):
            n = lost_page.evaluate(
                f"document.querySelectorAll('#panel-{k} svg .{cls}').length")
            assert n == 1, f"{k}: expected one .{cls}, got {n}"
    # Battery panels keep the strips and broken line but never a band.
    for k in ("bv", "bc"):
        assert lost_page.evaluate(
            f"document.querySelectorAll('#panel-{k} svg .recon-band').length") == 0
        assert lost_page.evaluate(
            f"document.querySelectorAll('#panel-{k} svg .lost-strip').length") == 1


def test_line_breaks_across_both_holes(lost_page):
    """The Line Voltage stroke path must contain three subpaths (two holes),
    where it used to be one continuous fake line."""
    n_subpaths = lost_page.evaluate(
        "Math.max(...Array.from(document.querySelectorAll('#panel-lv svg path'))"
        ".filter(p => p.getAttribute('fill') === 'none' && p.getAttribute('stroke'))"
        ".map(p => ((p.getAttribute('d')||'').match(/M/g)||[]).length))")
    assert n_subpaths >= 3


def test_tooltip_honesty_in_holes(lost_page):
    # Inside the link-down hole: "lost", mirrored across the sync group.
    _hover_at(lost_page, "lv", _ms(2026, 5, 5, 21))
    hov = lost_page.evaluate("__chartsDebug.hover('lv')")
    assert hov and hov.get("hole") == "lost"
    assert hov["rows"] == []
    assert lost_page.evaluate("__chartsDebug.hover('ul')").get("hole") == "lost"
    # Inside the monitoring-off hole: "gap", no estimate claimed.
    _hover_at(lost_page, "lv", _ms(2026, 5, 4, 16))
    assert lost_page.evaluate("__chartsDebug.hover('lv')").get("hole") == "gap"
    # A healthy region still snaps to a real sample.
    _hover_at(lost_page, "lv", _ms(2026, 5, 4, 6))
    hov3 = lost_page.evaluate("__chartsDebug.hover('lv')")
    assert hov3.get("hole") is None
    assert hov3["rows"], "healthy hover lost its snapped sample rows"


def test_estimated_chip_toggles_band(lost_page):
    assert lost_page.evaluate("__chartsDebug.reconHidden('lv')") is False
    lost_page.evaluate("document.querySelector('#panel-lv svg .recon-chip')"
                       ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    lost_page.wait_for_timeout(80)
    assert lost_page.evaluate("__chartsDebug.reconHidden('lv')") is True
    assert lost_page.evaluate(
        "document.querySelectorAll('#panel-lv svg .recon-band').length") == 0
    # resetAll restores the band with the rest of the pristine state.
    lost_page.evaluate("__chartsDebug.resetAll()")
    lost_page.wait_for_timeout(80)
    assert lost_page.evaluate("__chartsDebug.reconHidden('lv')") is False
    assert lost_page.evaluate(
        "document.querySelectorAll('#panel-lv svg .recon-band').length") == 1


def test_run_summary_names_the_incident(lost_page):
    assert "Lost telemetry (UPS link down)" in lost_page.content()
