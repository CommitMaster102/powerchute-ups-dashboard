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
    _build_kpis,
    _gap_spans,
    _heatmap_pivot,
    _ms_list,
    _panel_bv,
    _panel_cad,
    _panel_hm,
    _panel_kw,
    _panel_lv,
    _panel_rt,
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


def test_heatmap_nan_becomes_none():
    edf = _energy(n=30)                  # 2.5 h of samples -> most hours empty
    panel = _panel_hm(_heatmap_pivot(edf))
    flat = [v for row in panel["z"] for v in row]
    assert None in flat                  # NaN hours -> None (JSON-safe)
    assert any(v is not None for v in flat)


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
        ["lv", "ul", "pw", "hm", "bv", "bc", "rt", "kw", "daily", "growth", "proj", "cad"])
    assert payload["theme"] in ("dark", "light")
    assert payload["meta"]["last_sample_ms"] is not None


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
