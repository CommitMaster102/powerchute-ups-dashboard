"""Unit tests for the dashboard payload contract (pytest, no browser).

The load-bearing rule: timestamps cross the Python/JS boundary as
epoch-milliseconds integers where the log's naive local wall-clock time is
encoded as if it were UTC, and charts.js formats labels with UTC getters only.
These tests pin that encoding plus the payload shapes charts.js consumes
(series, gap spans, KPI severities, empty-data panels) and the end-to-end
build_dashboard() HTML smoke.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

import pcss.config as cfg
from pcss.dashboard import (
    PALETTES,
    _annotation_markers,
    _build_kpis,
    _gap_spans,
    _heatmap_pivot,
    _ms_list,
    _panel_bv,
    _panel_cad,
    _panel_cmp,
    _panel_hm,
    _panel_kw,
    _panel_lv,
    _panel_rt,
    _panel_wk,
    build_dashboard,
)
from pcss.stats import estimate_runtime

PAL = PALETTES["dark"]
EMPTY = pd.DataFrame()


def _datalog(n=60, start="2026-05-01 00:00", lv=120.0, ul=15.0, bc=100.0, bv=27.4):
    ts = pd.date_range(start, periods=n, freq="20min")
    return pd.DataFrame({
        "ts": ts,
        "Line Voltage": np.full(n, lv),
        "Battery Voltage": np.full(n, bv),
        "UPS Load": np.full(n, ul),
        "Battery Capacity": np.full(n, bc),
    })


def _energy(n=60, start="2026-05-01 00:00", power=250.0):
    ts = pd.date_range(start, periods=n, freq="5min")
    return pd.DataFrame({"ts": ts, "power_w": np.full(n, power), "interval_sec": 300})


# ---------------------------------------------------------------- timestamp contract
def test_ms_list_encodes_wallclock_as_utc():
    """2026-05-01 06:30 local wall-clock must encode to the epoch-ms of
    2026-05-01T06:30Z, no matter what timezone Python or the browser runs in.
    charts.js then formats it back with getUTC* — labels always match the log."""
    ms = _ms_list(pd.Series([pd.Timestamp("2026-05-01 06:30:00")]))[0]
    expected = int(datetime(2026, 5, 1, 6, 30, tzinfo=UTC).timestamp() * 1000)
    assert ms == expected
    # Round-trip: UTC formatting reproduces the original wall-clock text.
    rt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    assert (rt.year, rt.month, rt.day, rt.hour, rt.minute) == (2026, 5, 1, 6, 30)


def test_ms_list_is_ints_and_ordered():
    df = _datalog(5)
    ms = _ms_list(df["ts"])
    assert all(isinstance(v, int) for v in ms)
    assert ms == sorted(ms)
    assert ms[1] - ms[0] == 20 * 60 * 1000


# ---------------------------------------------------------------- gap spans
def test_gap_spans_shape():
    gaps = pd.DataFrame({
        "from": pd.to_datetime(["2026-05-01 10:00"]),
        "to": pd.to_datetime(["2026-05-01 12:00"]),
        "duration_min": [120.0],
    })
    spans = _gap_spans(gaps)
    assert len(spans) == 1
    assert spans[0][1] - spans[0][0] == 2 * 3600 * 1000


def test_gap_spans_empty():
    assert _gap_spans(pd.DataFrame()) == []


# ---------------------------------------------------------------- annotation markers (item 26)
def test_annotation_markers_shape_and_epoch_ms():
    annotations = pd.DataFrame({
        "date": [pd.Timestamp("2026-05-01").date()],
        "kind": ["battery_replaced"],
        "label": ["New battery installed"],
    })
    markers = _annotation_markers(annotations)
    assert len(markers) == 1
    expected = int(datetime(2026, 5, 1, tzinfo=UTC).timestamp() * 1000)
    assert markers[0]["x"] == expected
    assert markers[0]["label"] == "New battery installed"


def test_annotation_markers_blank_label_falls_back_to_kind():
    annotations = pd.DataFrame({
        "date": [pd.Timestamp("2026-05-01").date()],
        "kind": ["ups_moved"],
        "label": [""],
    })
    markers = _annotation_markers(annotations)
    assert markers[0]["label"] == "ups_moved"


def test_annotation_markers_empty_or_none():
    assert _annotation_markers(None) == []
    assert _annotation_markers(pd.DataFrame(columns=["date", "kind", "label"])) == []


# ---------------------------------------------------------------- panel builders
def test_panels_none_on_empty_frames():
    assert _panel_lv(EMPTY, EMPTY, PAL) is None
    assert _panel_hm(_heatmap_pivot(EMPTY)) is None
    assert _panel_kw({}, PAL) is None
    panel, slope = _panel_bv(EMPTY, PAL)
    assert panel is None and slope is None
    assert _panel_cad(EMPTY, PAL) is None


def test_panel_lv_markers_and_band():
    df = _datalog(20)
    anomalies = df.iloc[[3, 7]][["ts", "Line Voltage"]].copy()
    panel = _panel_lv(df, anomalies, PAL)
    assert panel["sync"] is True and panel["gaps"] is True
    assert panel["band"] == [cfg.VOLTAGE_NORMAL_LOW, cfg.VOLTAGE_NORMAL_HIGH]
    assert len(panel["markers"]) == 2
    assert all(m["type"] == "x" for m in panel["markers"])
    assert len(panel["series"][0]["x"]) == 20


def test_panel_bv_trend_slope_sign():
    df = _datalog(120)
    # A clearly declining battery: 27.4 V dropping 0.05 V per sample block.
    df["Battery Voltage"] = 27.4 - np.linspace(0, 0.5, len(df))
    panel, slope = _panel_bv(df, PAL)
    assert slope is not None and slope < 0
    assert [s["name"] for s in panel["series"]] == ["reading", "8h mean", "trend"]


def test_panel_rt_star_marker():
    panel, w, rt = _panel_rt(_energy(power=500.0), PAL)
    assert w == pytest.approx(500.0)
    assert rt == pytest.approx(estimate_runtime(500.0))
    assert panel["markers"][0]["type"] == "star"
    assert panel["xkind"] == "linear"
    # No energy data -> curve still renders, but no operating point.
    panel2, w2, rt2 = _panel_rt(EMPTY, PAL)
    assert panel2["markers"] == [] and w2 is None and rt2 is None


def test_panel_cad_bins_expected_and_gap():
    df = _datalog(30)
    # Insert one 60-min gap (3x the 20-min cadence).
    df.loc[15:, "ts"] = df.loc[15:, "ts"] + pd.Timedelta(minutes=40)
    panel = _panel_cad(df, PAL)
    data = {d["label"]: d["y"] for d in panel["data"]}
    assert data["19-21m"] == 28          # the regular cadence bucket
    assert data[">40m"] == 1             # the gap bucket
    # The expected-cadence bucket is highlighted teal.
    colors = {d["label"]: d["color"] for d in panel["data"]}
    assert colors["19-21m"] == PAL["teal"]


def test_heatmap_daykeys_parallel_to_days():
    """The heatmap's rows carry real dates (epoch-ms midnights) alongside the
    display labels so the crosshair can find the hovered day's row."""
    panel = _panel_hm(_heatmap_pivot(_energy(n=576)))     # 2 days at 5 min
    assert len(panel["dayKeys"]) == len(panel["days"]) == 2
    assert all(k % 86_400_000 == 0 for k in panel["dayKeys"])
    assert panel["dayKeys"][1] - panel["dayKeys"][0] == 86_400_000


def test_heatmap_nan_becomes_none():
    edf = _energy(n=30)                  # 2.5 h of samples -> most hours empty
    panel = _panel_hm(_heatmap_pivot(edf))
    flat = [v for row in panel["z"] for v in row]
    assert None in flat                  # NaN hours -> None (JSON-safe)
    assert any(v is not None for v in flat)


# ---------------------------------------------------------------- comparison panels
def test_panel_cmp_compares_last_two_periods():
    from pcss.stats import compute_energy_summary
    edf = pd.concat([_energy(n=288, start="2026-04-29 00:00"),
                     _energy(n=288, start="2026-05-01 00:00")], ignore_index=True)
    edf["interval_sec"] = 300
    panel = _panel_cmp(compute_energy_summary(edf), PAL)
    assert panel["xkind"] == "linear"
    assert [s["name"] for s in panel["series"]] == ["2026-04", "2026-05"]
    cur = panel["series"][1]
    # Cumulative energy is monotonically non-decreasing.
    assert all(b >= a for a, b in zip(cur["y"], cur["y"][1:], strict=False))
    # Day offsets are within the period: April 29 sits at offset ~28 days.
    assert panel["series"][0]["x"][0] == pytest.approx(28.0, abs=0.1)
    assert cur["x"][0] == pytest.approx(0.0, abs=0.1)


def test_panel_cmp_needs_two_periods():
    from pcss.stats import compute_energy_summary
    assert _panel_cmp(compute_energy_summary(_energy(60)), PAL) is None
    assert _panel_cmp({}, PAL) is None


def test_panel_wk_weekday_weekend_profiles():
    # 2026-05-01 is a Friday (weekday, 200 W); 2026-05-02 a Saturday (400 W).
    edf = pd.concat([_energy(n=288, start="2026-05-01 00:00", power=200.0),
                     _energy(n=288, start="2026-05-02 00:00", power=400.0)],
                    ignore_index=True)
    panel = _panel_wk(edf, PAL)
    assert [s["name"] for s in panel["series"]] == ["weekday", "weekend"]
    assert panel["series"][0]["y"][0] == pytest.approx(200.0)
    assert panel["series"][1]["y"][0] == pytest.approx(400.0)
    assert panel["series"][0]["x"] == list(range(24))


def test_panel_wk_empty():
    assert _panel_wk(EMPTY, PAL) is None


# ---------------------------------------------------------------- KPI severities
def test_kpi_severities_all_nominal():
    cards, sparks, sevs = _build_kpis(_datalog(), _energy(), 250.0,
                                      estimate_runtime(250.0), PAL)
    assert [c["label"] for c in cards] == [
        "Line Voltage", "UPS Load", "Battery Charge", "Est. Runtime", "Power Draw"]
    by = {c["label"]: c["status"] for c in cards}
    assert by["Line Voltage"] == "OK"
    assert by["UPS Load"] == "OK"
    assert by["Battery Charge"] == "OK"
    assert by["Est. Runtime"] == "OK"     # 15 min at 250 W is exactly the warn edge
    assert by["Power Draw"] == "LIVE"
    assert "crit" not in sevs


def test_kpi_severities_degraded():
    df = _datalog(lv=130.0, ul=85.0, bc=60.0)
    latest_w = 600.0
    latest_rt = estimate_runtime(latest_w)   # 3.5 min -> below the 7-min crit line
    cards, _, sevs = _build_kpis(df, _energy(power=latest_w), latest_w, latest_rt, PAL)
    by = {c["label"]: c["status"] for c in cards}
    assert by["Line Voltage"] == "ALERT"      # 130 V outside the envelope
    assert by["UPS Load"] == "ALERT"          # 85% over the 80% threshold
    assert by["Battery Charge"] == "WARN"     # 60% between crit(50) and warn(90)
    assert by["Est. Runtime"] == "ALERT"
    assert sevs.count("crit") == 3


def test_kpi_no_data_is_info():
    cards, sparks, sevs = _build_kpis(EMPTY, EMPTY, None, None, PAL)
    assert all(c["value"] == "—" for c in cards)
    assert sevs == []                        # info never counts against health
    assert sparks == [None] * 5


# ---------------------------------------------------------------- page-level features
def test_print_stylesheet_and_button_present():
    html = build_dashboard(**_smoke_inputs())
    assert "@media print" in html
    assert 'id="print-btn"' in html


def test_meta_refresh_opt_in(monkeypatch):
    # Off by default: a static snapshot must not reload itself.
    html = build_dashboard(**_smoke_inputs())
    assert 'http-equiv="refresh"' not in html
    # [dashboard] refresh_minutes = 5 -> a 300-second meta refresh, for a
    # dashboard left open on a second screen.
    monkeypatch.setattr(cfg, "DASHBOARD_REFRESH_MINUTES", 5.0)
    html2 = build_dashboard(**_smoke_inputs())
    assert '<meta http-equiv="refresh" content="300">' in html2


def test_spanish_localization(monkeypatch):
    """[dashboard] language = "es" translates the page chrome via the single
    string table; the JS tooltip labels ride the payload so the two sides
    cannot drift. Number formatting stays en-US (the CSV export contract)."""
    monkeypatch.setattr(cfg, "DASHBOARD_LANGUAGE", "es")
    html = build_dashboard(**_smoke_inputs())
    for token in ["Calidad de energía", "Voltaje de línea", "Salud de la batería",
                  "Energía y costo", "Estadísticas por métrica"]:
        assert token in html, f"missing Spanish token: {token}"
    assert "Power Quality" not in html
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    assert payload["strings"]["meanPower"] == "potencia media"
    assert 'toLocaleString("en-US"' in html   # machine-standard numbers survive


def test_default_language_is_english():
    html = build_dashboard(**_smoke_inputs())
    assert "Power Quality" in html
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    assert payload["strings"]["meanPower"] == "mean power"


def test_accessibility_surface():
    """Charts as SVG are opaque to screen readers, so every chart card gets a
    role, a data summary in its aria-label (min, max, latest — generated in
    Python where the numbers already exist), and keyboard focusability."""
    html = build_dashboard(**_smoke_inputs())
    assert 'role="img"' in html
    assert 'tabindex="0"' in html
    # The Line Voltage box describes its own data.
    m = re.search(r'id="panel-lv"[^>]*aria-label="([^"]+)"', html)
    assert m, "panel-lv has no aria-label"
    assert "latest" in m.group(1).lower()
    assert "minimum" in m.group(1).lower()
    # Empty panels say so instead of carrying a stale summary.
    empty_html = build_dashboard(
        datalog_df=EMPTY, energy_df=EMPTY, hist=EMPTY, dl_stats={}, hist_stats={},
        sizes={"DataLog": 0, "EventLog (binary)": 0, "energylog/": 0}, energy_summary={},
        stats_table=EMPTY, gaps=EMPTY, voltage_anomalies=EMPTY,
        high_load_episodes=EMPTY, crossval={})
    m2 = re.search(r'id="panel-lv"[^>]*aria-label="([^"]+)"', empty_html)
    assert "no data" in m2.group(1).lower()


# ---------------------------------------------------------------- build_dashboard smoke
def _smoke_inputs():
    datalog = _datalog(72)
    energy = _energy(144)
    hist = pd.DataFrame({
        "timestamp": pd.date_range("2026-05-01", periods=4, freq="1D"),
        "datalog_bytes": [1000, 2000, 3000, 4000],
        "eventlog_bytes": [100, 100, 100, 100],
        "energylog_bytes": [500, 900, 1300, 1700],
        "total_bytes": [1600, 3000, 4400, 5800],
    })
    stats_table = pd.DataFrame([{"Metric": "Line Voltage", "Min": "118.00", "Mean": "120.00",
                                 "Median": "120.00", "p95": "122.00", "Max": "122.00",
                                 "Samples": 72}])
    dl_stats = {"daily_bytes": 5000.0, "span_days": 1.0, "median_interval_sec": 1200.0}
    from pcss.stats import compute_energy_summary
    energy_summary = compute_energy_summary(energy)
    return dict(datalog_df=datalog, energy_df=energy, hist=hist, dl_stats=dl_stats,
                hist_stats={"bytes_per_hour": 100.0, "bytes_per_day": 2400.0, "snapshots": 4},
                sizes={"DataLog": 4000, "EventLog (binary)": 100, "energylog/": 1700},
                energy_summary=energy_summary, stats_table=stats_table,
                gaps=pd.DataFrame(), voltage_anomalies=pd.DataFrame(),
                high_load_episodes=pd.DataFrame(), crossval={})


def test_build_dashboard_html_smoke():
    html = build_dashboard(**_smoke_inputs())
    assert "__DASH_DATA__" not in html, "payload token was not substituted"
    for token in ["panel-lv", "panel-hm", "panel-cad", "panel-rt", "__chartsDebug",
                  "Per-metric Statistics", "Latest Readings", "lightbox", "preset-pill",
                  "fully offline"]:
        assert token in html, f"missing shell token: {token}"
    # Payload must be valid strict JSON (allow_nan=False at dump time).
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    assert m, "embedded payload not found"
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    assert sorted(payload["panels"]) == sorted(
        ["lv", "ul", "pw", "hm", "bv", "bc", "rt", "kw", "daily", "cmp", "wk",
         "growth", "proj", "cad"])
    assert payload["theme"] in ("dark", "light")
    assert payload["meta"]["last_sample_ms"] is not None


def test_on_battery_episodes_in_payload_and_health():
    """Episode spans ride the payload like gap spans (epoch-ms pairs) so
    charts.js can shade them, and the health pill counts them."""
    inputs = _smoke_inputs()
    inputs["on_battery"] = pd.DataFrame({
        "start": pd.to_datetime(["2026-05-01 04:00"]),
        "end": pd.to_datetime(["2026-05-01 04:40"]),
        "duration_min": [60.0], "min_voltage": [0.5], "capacity_drop_pct": [12.0],
    })
    html = build_dashboard(**inputs)
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    assert len(payload["episodes"]) == 1
    assert payload["episodes"][0][1] - payload["episodes"][0][0] == 40 * 60 * 1000
    assert "1 on-battery" in html


def test_build_dashboard_annotations_default_empty():
    """With no annotations argument, the payload carries an empty list — the
    page is unchanged for anyone who never creates annotations.csv."""
    html = build_dashboard(**_smoke_inputs())
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    assert payload["annotations"] == []


def test_build_dashboard_annotations_in_payload():
    inputs = _smoke_inputs()
    inputs["annotations"] = pd.DataFrame({
        "date": [pd.Timestamp("2026-05-01").date()],
        "kind": ["battery_replaced"],
        "label": ["New battery installed"],
    })
    html = build_dashboard(**inputs)
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    assert len(payload["annotations"]) == 1
    expected = int(datetime(2026, 5, 1, tzinfo=UTC).timestamp() * 1000)
    assert payload["annotations"][0]["x"] == expected
    assert payload["annotations"][0]["label"] == "New battery installed"


def test_build_dashboard_annotations_feed_default_battery_projection():
    """When build_dashboard computes its own battery projection (battery=None,
    the default), it must pass the annotations through so the fit segments at
    a battery_replaced boundary and the Battery Voltage subtitle names the
    battery's age — the same information a direct battery_replace_projection
    call with the same annotations would carry."""
    inputs = _smoke_inputs()
    long_df = _datalog(n=90 * 72)
    long_df["Battery Voltage"] = 27.4 - 0.01 * np.arange(len(long_df)) / 72.0
    inputs["datalog_df"] = long_df
    boundary_date = long_df["ts"].iloc[30 * 72].date()
    inputs["annotations"] = pd.DataFrame({
        "date": [boundary_date], "kind": ["battery_replaced"], "label": ["new battery"],
    })
    html = build_dashboard(**inputs)
    assert "battery age" in html
    assert f"{boundary_date:%Y-%m-%d}" in html


def test_battery_replace_projection_in_subtitle_and_health():
    """A long declining battery history puts a concrete replace-by date in
    the Battery Voltage subtitle; a short one states the honest fallback."""
    inputs = _smoke_inputs()
    long_df = _datalog(n=90 * 72)
    long_df["Battery Voltage"] = 27.4 - 0.01 * np.arange(len(long_df)) / 72.0
    inputs["datalog_df"] = long_df
    html = build_dashboard(**inputs)
    assert "replace ≈" in html

    short = _smoke_inputs()
    short_df = _datalog(n=10 * 72)
    short_df["Battery Voltage"] = 27.4 - 0.01 * np.arange(len(short_df)) / 72.0
    short["datalog_df"] = short_df
    html2 = build_dashboard(**short)
    assert "not enough history" in html2


def test_default_preset_only_for_long_history():
    """Once the archive grows past ~45 days, the page should open on the
    30-day preset instead of an unreadably dense full-range view; short
    histories keep the full range (no preset key in the payload meta)."""
    def _meta_for(days):
        inputs = _smoke_inputs()
        inputs["datalog_df"] = _datalog(n=days * 72)     # 20-min cadence
        html = build_dashboard(**inputs)
        m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
        return json.loads(m.group(1).replace("<\\/", "</"))["meta"]

    assert _meta_for(60).get("default_preset_days") == 30
    assert _meta_for(10).get("default_preset_days") is None


def test_no_tariff_history_dashboard_html_unchanged(monkeypatch):
    """With TARIFF_HISTORY empty (the default), the page must be
    byte-identical to a run with the key untouched — no Billing Periods
    card, no stray blank-line drift from the conditional block."""
    monkeypatch.setattr(cfg, "TARIFF_HISTORY", [])
    baseline = build_dashboard(**_smoke_inputs())
    monkeypatch.setattr(cfg, "TARIFF_HISTORY", [])
    again = build_dashboard(**_smoke_inputs())
    assert baseline == again
    assert "Billing Periods" not in baseline


def test_tariff_history_adds_billing_periods_table(monkeypatch):
    """Once [[tariff.history]] entries are configured, a Billing Periods
    table appears, tagged with which rates priced each period."""
    from pcss.config import TariffPeriod
    monkeypatch.setattr(cfg, "TARIFF_HISTORY", [
        TariffPeriod(datetime(2026, 1, 1).date(), coopesantos_low=70.0,
                     coopesantos_high=110.0, tier_limit_kwh=200.0, pcss_flat=110.0),
    ])
    inputs = _smoke_inputs()          # recomputes energy_summary against the patched history
    html = build_dashboard(**inputs)
    assert "Billing Periods" in html
    assert "rates from 2026-01-01" in html


def test_build_dashboard_empty_inputs():
    """A run against an empty agent dir must still produce a page (every
    panel None -> client-side empty states), not crash."""
    html = build_dashboard(
        datalog_df=EMPTY, energy_df=EMPTY, hist=EMPTY, dl_stats={}, hist_stats={},
        sizes={"DataLog": 0, "EventLog (binary)": 0, "energylog/": 0}, energy_summary={},
        stats_table=EMPTY, gaps=EMPTY, voltage_anomalies=EMPTY,
        high_load_episodes=EMPTY, crossval={})
    assert "panel-lv" in html
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    assert payload["panels"]["lv"] is None
    assert payload["panels"]["rt"] is not None   # the curve needs no log data
