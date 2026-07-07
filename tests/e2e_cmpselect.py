"""E2E: selectable comparison periods on the Period Comparison card.

The shared hermetic fixture's energylog spans only two billing periods
(2026-06/2026-07) — enough to exercise the default "previous" comparison
and the "quarter" pill's disabled state, but not enough for a meaningful
baseline switch. Following the dedicated-build pattern the baseline and
theme suites already use, this module builds its own dashboard directly
from six monthly energy_df frames with distinguishable power levels, so
switching the compared baseline actually changes the rendered numbers.
"""
from __future__ import annotations

import calendar

import pandas as pd
import pytest
from harness import wait_ready

pytestmark = pytest.mark.e2e


def _month_frame(year: int, month: int, power: float, n_days: int | None = None) -> pd.DataFrame:
    """One month of hourly energylog-shaped samples at a constant power, so
    each month's cumulative kWh is distinguishable from the others. n_days
    short of the calendar month leaves that period "partial" (still open),
    matching how a real current billing period looks mid-cycle."""
    full_days = calendar.monthrange(year, month)[1]
    n_days = full_days if n_days is None else n_days
    start = pd.Timestamp(year=year, month=month, day=1)
    ts = pd.date_range(start, periods=n_days * 24, freq="1h")
    return pd.DataFrame({"ts": ts, "power_w": power, "interval_sec": 3600})


@pytest.fixture(scope="module")
def cmpselect_dashboard_path(tmp_path_factory):
    """Six monthly periods (2026-01 .. 2026-06), each at a different power
    level. Current = 2026-06 (partial, 10 days in); previous (default
    baseline) = 2026-05; "same period one quarter back" = 2026-03 (three
    periods behind current) — enough history for every comparison mode."""
    from pcss.dashboard import build_dashboard
    from pcss.stats import compute_energy_summary

    edf = pd.concat([
        _month_frame(2026, 1, 100.0),
        _month_frame(2026, 2, 150.0),
        _month_frame(2026, 3, 200.0),
        _month_frame(2026, 4, 250.0),
        _month_frame(2026, 5, 300.0),
        _month_frame(2026, 6, 350.0, n_days=10),
    ], ignore_index=True)

    empty = pd.DataFrame()
    html = build_dashboard(
        datalog_df=empty, energy_df=edf, hist=empty,
        dl_stats={}, hist_stats={},
        sizes={"DataLog": 0, "EventLog (binary)": 0, "energylog/": 0},
        energy_summary=compute_energy_summary(edf), stats_table=empty,
        gaps=empty, voltage_anomalies=empty, high_load_episodes=empty,
        crossval={},
    )
    out = tmp_path_factory.mktemp("cmpselect-e2e") / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    return out


def _open(browser, dashboard_path, hash_=""):
    page = browser.new_page(viewport={"width": 1600, "height": 2400})
    page.goto(dashboard_path.resolve().as_uri() + hash_)
    wait_ready(page)
    return page


def test_default_selection_is_previous_period(_browser, cmpselect_dashboard_path):
    page = _open(_browser, cmpselect_dashboard_path)
    try:
        sel = page.evaluate("__chartsDebug.cmpSelection()")
        assert sel["mode"] == "previous"
        assert sel["baseline"] == "2026-05"
        assert sel["current"] == "2026-06"
    finally:
        page.close()


def test_quarter_pill_switches_baseline_changes_render_and_hash(_browser, cmpselect_dashboard_path):
    page = _open(_browser, cmpselect_dashboard_path)
    try:
        before = page.eval_on_selector_all(
            "#panel-cmp svg path[stroke]", "els => els.map(e => e.getAttribute('d'))")
        page.locator('.cmp-pill[data-mode="quarter"]').click()
        page.wait_for_timeout(80)
        sel = page.evaluate("__chartsDebug.cmpSelection()")
        assert sel["mode"] == "quarter"
        assert sel["baseline"] == "2026-03"
        assert sel["current"] == "2026-06"
        after = page.eval_on_selector_all(
            "#panel-cmp svg path[stroke]", "els => els.map(e => e.getAttribute('d'))")
        assert after != before, "switching baseline did not change the rendered series"
        assert "c=quarter" in page.evaluate("location.hash")
        # The pill row reflects the active mode.
        assert page.locator('.cmp-pill[data-mode="quarter"]').get_attribute("class") \
            and "is-active" in page.locator('.cmp-pill[data-mode="quarter"]').get_attribute("class")
        # aria-pressed tracks the active baseline pill and clears the other.
        assert page.locator('.cmp-pill[data-mode="quarter"]').get_attribute("aria-pressed") == "true"
        assert page.locator('.cmp-pill[data-mode="previous"]').get_attribute("aria-pressed") == "false"
        # The baseline's label is visible in the legend, so a shared
        # permalink reads correctly without any extra UI.
        legend_text = page.eval_on_selector_all(
            "#panel-cmp svg .legend-chip text", "els => els.map(e => e.textContent)")
        assert "2026-03" in legend_text
        assert "2026-05" not in legend_text
    finally:
        page.close()


def test_reload_with_hash_restores_selection(_browser, cmpselect_dashboard_path):
    page = _open(_browser, cmpselect_dashboard_path, "#c=quarter")
    try:
        sel = page.evaluate("__chartsDebug.cmpSelection()")
        assert sel["mode"] == "quarter"
        assert sel["baseline"] == "2026-03"
    finally:
        page.close()


def test_pick_a_period_select_sets_explicit_baseline(_browser, cmpselect_dashboard_path):
    page = _open(_browser, cmpselect_dashboard_path)
    try:
        page.locator(".cmp-period-select").select_option("2026-01")
        page.wait_for_timeout(80)
        sel = page.evaluate("__chartsDebug.cmpSelection()")
        assert sel["mode"] == "2026-01"
        assert sel["baseline"] == "2026-01"
        assert "c=2026-01" in page.evaluate("location.hash")
    finally:
        page.close()


def test_reset_all_returns_to_default_and_clears_hash(_browser, cmpselect_dashboard_path):
    page = _open(_browser, cmpselect_dashboard_path, "#c=quarter")
    try:
        assert page.evaluate("__chartsDebug.cmpSelection()")["mode"] == "quarter"
        page.evaluate("__chartsDebug.resetAll()")
        page.wait_for_timeout(80)
        sel = page.evaluate("__chartsDebug.cmpSelection()")
        assert sel["mode"] == "previous"
        assert sel["baseline"] == "2026-05"
        assert page.evaluate("location.hash") in ("", "#")
    finally:
        page.close()


def test_stale_period_in_hash_falls_back_silently(_browser, cmpselect_dashboard_path):
    page = _open(_browser, cmpselect_dashboard_path, "#c=2099-12")
    try:
        assert page.evaluate("__chartsDebug.ready()")
        sel = page.evaluate("__chartsDebug.cmpSelection()")
        assert sel["mode"] == "previous"
    finally:
        page.close()


def test_csv_export_reflects_selected_comparison(_browser, cmpselect_dashboard_path, tmp_path):
    page = _open(_browser, cmpselect_dashboard_path)
    try:
        page.locator('.cmp-pill[data-mode="quarter"]').click()
        page.wait_for_timeout(80)
        with page.expect_download() as dl:
            page.evaluate("document.querySelector('.tool-csv[data-panel=cmp]').click()")
        path = tmp_path / "cmp.csv"
        dl.value.save_as(path)
        text = path.read_text(encoding="utf-8")
        header = text.splitlines()[0]
        assert "2026-03" in header
        assert "2026-05" not in header
        # A row-level value that provably belongs to the 2026-03 baseline:
        # March is a full 31-day period at 200 W hourly, so its
        # cumulative kWh reaches 31 * 24 * 0.2 = 148.8. The un-selected 2026-05
        # baseline (300 W) would instead top out at 223.2, which must be absent.
        assert "148.8" in text
        assert "223.2" not in text
    finally:
        page.close()


def test_quarter_pill_disabled_with_short_history(dash):
    """The shared hermetic fixture's energylog spans only two billing
    periods, well under the four "quarter" needs — must not be reshaped for
    this suite (other suites depend on its shape)."""
    pill = dash.locator('.cmp-pill[data-mode="quarter"]')
    assert pill.is_disabled()
    # Driving the mode directly (bypassing the disabled control) still
    # falls back instead of silently comparing against the wrong period.
    dash.evaluate("__chartsDebug.setCmpSelection('quarter')")
    dash.wait_for_timeout(50)
    assert dash.evaluate("__chartsDebug.cmpSelection()")["mode"] == "previous"
