"""Unit tests for self-test detection and battery health under load
(roadmap item 18).

The Battery Charge card's sawtooth comes from the UPS's periodic
self-tests. `detect_self_tests` in `pcss/stats.py` finds them from the
DataLog's capacity-dip shape (a drop corroborated by line voltage staying
inside its normal envelope -- the opposite corroboration
`detect_on_battery_episodes` uses), or from parsed EventLog events once the
exact self-test event id is known (`pcss.eventlog.SELF_TEST_EVENT_IDS`,
still empty as of this writing). Each detected test carries a voltage sag --
the resting Battery Voltage just before the dip minus the minimum inside it
-- which `self_test_sag_trend` fits against time the same way
`battery_replace_projection` fits the resting-voltage slope, with the same
honesty floor (`battery_trend_min_days`). `battery_replace_projection` also
accepts the detected tests to mask their windows out of its own fit.
"""
from __future__ import annotations

import re
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import pcss.config as cfg
import pcss.eventlog as ev
from pcss.dashboard import _ms_list, _panel_bc, build_dashboard
from pcss.stats import battery_replace_projection, detect_self_tests, self_test_sag_trend


# ====================================================================== helpers
def _df(cap, lv=None, bv=None, start="2026-05-01 00:00", freq="20min"):
    n = len(cap)
    ts = pd.date_range(start, periods=n, freq=freq)
    data: dict = {"ts": ts, "Battery Capacity": np.array(cap, dtype=float)}
    if lv is not None:
        data["Line Voltage"] = np.array(lv, dtype=float)
    if bv is not None:
        data["Battery Voltage"] = np.array(bv, dtype=float)
    return pd.DataFrame(data)


def _bv_cap_frame(days, cadence_min=20, base_v=27.4, slope_per_day=-0.01,
                  add_dips=False, dip_v=8.0, dip_cap=8.0, period_days=1, dip_len=3):
    """A DataLog-shaped frame spanning `days` at `cadence_min` cadence, with a
    clean linear Battery Voltage decline and, optionally, a self-test-shaped
    dip in both Battery Capacity and Battery Voltage every `period_days`."""
    n = int(days * 24 * 60 / cadence_min)
    ts = pd.date_range("2026-01-01", periods=n, freq=f"{cadence_min}min")
    t_days = np.arange(n) * cadence_min / (24 * 60)
    v = base_v + slope_per_day * t_days
    cap = np.full(n, 100.0)
    lv = np.full(n, 120.0)
    if add_dips:
        period = int(period_days * 24 * 60 / cadence_min)
        for s in range(period, n - dip_len, period):
            v[s:s + dip_len] -= dip_v
            cap[s:s + dip_len] -= dip_cap
    return pd.DataFrame({"ts": ts, "Battery Voltage": v, "Battery Capacity": cap, "Line Voltage": lv})


def _tests(ts_list, sag_list):
    ts = pd.to_datetime(list(ts_list))
    return pd.DataFrame({
        "ts": ts, "dip_start": ts, "dip_end": ts,
        "capacity_drop_pct": [5.0] * len(ts), "sag_v": list(sag_list),
        "source": ["shape"] * len(ts),
    })


def _dashboard_inputs(datalog_df):
    return dict(
        datalog_df=datalog_df, energy_df=pd.DataFrame(), hist=pd.DataFrame(),
        dl_stats={}, hist_stats={},
        sizes={"DataLog": 0, "EventLog (binary)": 0, "energylog/": 0},
        energy_summary={}, stats_table=pd.DataFrame(), gaps=pd.DataFrame(),
        voltage_anomalies=pd.DataFrame(), high_load_episodes=pd.DataFrame(), crossval={},
    )


def _datalog(n=20, start="2026-05-01 00:00"):
    ts = pd.date_range(start, periods=n, freq="20min")
    return pd.DataFrame({
        "ts": ts,
        "Line Voltage": np.full(n, 120.0),
        "Battery Voltage": np.full(n, 27.4),
        "UPS Load": np.full(n, 15.0),
        "Battery Capacity": np.full(n, 100.0),
    })


# ====================================================================== config
def test_config_defaults():
    assert cfg.SELFTEST_DIP_PCT == 3.0
    assert cfg.SELFTEST_RECOVERY_SAMPLES == 4


def test_load_config_overrides_selftest_thresholds(tmp_path):
    saved = (cfg.SELFTEST_DIP_PCT, cfg.SELFTEST_RECOVERY_SAMPLES)
    try:
        conf = tmp_path / "config.toml"
        conf.write_text(
            "[thresholds]\nselftest_dip_pct = 5.0\nselftest_recovery_samples = 6\n",
            encoding="utf-8")
        cfg.load_config(conf)
        assert cfg.SELFTEST_DIP_PCT == 5.0
        assert cfg.SELFTEST_RECOVERY_SAMPLES == 6
    finally:
        cfg.SELFTEST_DIP_PCT, cfg.SELFTEST_RECOVERY_SAMPLES = saved


def test_default_self_test_event_ids_is_empty_tuple():
    assert ev.SELF_TEST_EVENT_IDS == ()


# ====================================================================== detector: shape
def test_clean_dip_detected_with_timestamp_and_sag():
    cap = [100, 100, 95, 96, 98, 100, 100]
    lv = [120] * 7
    bv = [27.4, 27.4, 26.9, 27.0, 27.2, 27.4, 27.4]
    df = _df(cap, lv, bv)
    result = detect_self_tests(df)
    assert len(result) == 1
    row = result.iloc[0]
    assert row["ts"] == df["ts"].iloc[2]
    assert row["dip_start"] == df["ts"].iloc[1]
    assert row["dip_end"] == df["ts"].iloc[5]
    assert row["capacity_drop_pct"] == pytest.approx(5.0)
    assert row["sag_v"] == pytest.approx(0.5)
    assert row["source"] == "shape"


def test_low_line_voltage_dip_is_not_a_self_test():
    """The same capacity-dip shape, but with line voltage collapsing during
    the window, is episode territory (detect_on_battery_episodes), not a
    self-test."""
    cap = [100, 100, 95, 96, 98, 100]
    lv = [120, 120, 40, 120, 120, 120]
    df = _df(cap, lv)
    assert detect_self_tests(df).empty


def test_dip_that_never_recovers_is_not_a_self_test():
    cap = [100, 100, 95, 90, 85, 80, 75]
    lv = [120] * 7
    df = _df(cap, lv)
    assert detect_self_tests(df).empty


def test_dip_pct_threshold_respected():
    cap = [100, 100, 97.5, 98, 99, 100]     # a 2.5-point drop
    lv = [120] * 6
    df = _df(cap, lv)
    assert detect_self_tests(df).empty                       # default 3.0 misses it
    assert len(detect_self_tests(df, dip_pct=2.0)) == 1       # a looser floor catches it


def test_recovery_samples_threshold_respected():
    cap = [100, 100, 95, 95, 95, 95, 95, 100]   # recovers only on the 5th sample after the dip
    lv = [120] * 8
    df = _df(cap, lv)
    assert detect_self_tests(df).empty                                  # default window (4) misses it
    assert len(detect_self_tests(df, recovery_samples=5)) == 1           # a wider window catches it


def test_missing_battery_capacity_column_is_empty():
    df = pd.DataFrame({"ts": pd.date_range("2026-01-01", periods=5, freq="20min"),
                       "Line Voltage": [120.0] * 5})
    assert detect_self_tests(df).empty


def test_missing_line_voltage_column_is_empty():
    """Without Line Voltage there is no way to rule out an on-battery
    episode, so the shape route reports nothing rather than guessing."""
    cap = [100, 100, 95, 96, 98, 100]
    df = _df(cap, lv=None, bv=[27.4] * 6)
    assert detect_self_tests(df).empty


def test_no_battery_voltage_column_yields_record_with_nan_sag():
    """A test window with no usable voltage samples yields a test record
    without a sag value, never a crash."""
    cap = [100, 100, 95, 96, 98, 100]
    lv = [120] * 6
    df = _df(cap, lv)                      # no Battery Voltage column at all
    result = detect_self_tests(df)
    assert len(result) == 1
    assert np.isnan(result.iloc[0]["sag_v"])
    assert result.iloc[0]["capacity_drop_pct"] == pytest.approx(5.0)


def test_empty_frame():
    assert detect_self_tests(pd.DataFrame()).empty


# ====================================================================== detector: event-id precedence
def test_event_id_precedence_over_shape(monkeypatch):
    """Once SELF_TEST_EVENT_IDS names a real id, the event route replaces
    the shape heuristic entirely for that call -- even a shape-detectable
    dip elsewhere in the same DataLog is not independently reported."""
    monkeypatch.setattr(ev, "SELF_TEST_EVENT_IDS", ("9.9.9.9",))
    start = pd.Timestamp("2026-03-01 00:00")
    n = 10
    cap = [100.0] * n
    cap[2], cap[3] = 95.0, 96.0            # a shape-detectable dip at index 2-4
    cap[4] = 100.0
    df = pd.DataFrame({
        "ts": pd.date_range(start, periods=n, freq="20min"),
        "Battery Capacity": cap,
        "Battery Voltage": [27.4] * n,
        "Line Voltage": [120.0] * n,
    })
    event_ts = start + pd.Timedelta(minutes=160)      # index 8, well clear of the shape dip
    events = pd.DataFrame({
        "ts": [event_ts], "ts_ms": [0], "oid": ["9.9.9.9"],
        "active": [True], "name": ["event 9.9.9.9"],
    })
    result = detect_self_tests(df, events=events)
    assert len(result) == 1
    assert result.iloc[0]["source"] == "event"
    assert result.iloc[0]["ts"] == event_ts


def test_no_matching_event_falls_back_to_shape(monkeypatch):
    """A configured id that never actually appears in this run's events must
    not suppress the shape heuristic."""
    monkeypatch.setattr(ev, "SELF_TEST_EVENT_IDS", ("9.9.9.9",))
    cap = [100, 100, 95, 96, 98, 100]
    lv = [120] * 6
    df = _df(cap, lv)
    events = pd.DataFrame({
        "ts": [df["ts"].iloc[0]], "ts_ms": [0], "oid": ["1.2.3.4"],
        "active": [True], "name": ["event 1.2.3.4"],
    })
    result = detect_self_tests(df, events=events)
    assert len(result) == 1
    assert result.iloc[0]["source"] == "shape"


# ====================================================================== sag trend
def test_sag_trend_below_floor_is_honest():
    ts_list = [f"2026-01-{d:02d}" for d in range(1, 11)]      # 9-day span
    trend = self_test_sag_trend(_tests(ts_list, [0.5] * 10))
    assert trend["status"] == "insufficient_history"
    assert trend["slope_v_per_day"] is None
    assert trend["n_tests"] == 10
    assert trend["median_sag_v"] == pytest.approx(0.5)        # known even below the floor


def test_sag_trend_available_above_floor():
    ts_list = pd.date_range("2026-01-01", periods=40, freq="2D")     # spans 78 days
    trend = self_test_sag_trend(_tests(ts_list, [0.5] * 40))
    assert trend["status"] == "trended"
    assert trend["slope_v_per_day"] == pytest.approx(0.0, abs=1e-6)
    assert trend["median_sag_v"] == pytest.approx(0.5)
    assert trend["span_days"] == pytest.approx(78.0)


def test_sag_trend_detects_worsening_slope_sign():
    ts_list = pd.date_range("2026-01-01", periods=40, freq="2D")
    days = np.arange(40) * 2.0
    sag = (0.3 + 0.01 * days).tolist()
    trend = self_test_sag_trend(_tests(ts_list, sag))
    assert trend["status"] == "trended"
    assert trend["slope_v_per_day"] == pytest.approx(0.01, rel=0.05)


def test_sag_trend_no_tests_at_all():
    trend = self_test_sag_trend(pd.DataFrame(columns=["ts", "sag_v"]))
    assert trend["status"] == "insufficient_history"
    assert trend["n_tests"] == 0
    assert trend["median_sag_v"] is None
    trend_none = self_test_sag_trend(None)
    assert trend_none == trend


def test_sag_trend_custom_min_days_floor():
    ts_list = [f"2026-01-{d:02d}" for d in range(1, 11)]    # 9-day span
    tests = _tests(ts_list, [0.5] * 10)
    assert self_test_sag_trend(tests, min_days=5.0)["status"] == "trended"
    assert self_test_sag_trend(tests, min_days=30.0)["status"] == "insufficient_history"


# ====================================================================== projection mask
def test_battery_replace_projection_masks_self_test_windows():
    clean = _bv_cap_frame(days=90, add_dips=False)
    dipped = _bv_cap_frame(days=90, add_dips=True)
    tests = detect_self_tests(dipped)
    assert len(tests) > 10        # sanity: the injected dips were actually found

    proj_clean = battery_replace_projection(clean)
    proj_unmasked = battery_replace_projection(dipped)
    proj_masked = battery_replace_projection(dipped, self_tests=tests)

    assert proj_masked["status"] == "projected"
    assert proj_masked["slope_v_per_day"] == pytest.approx(proj_clean["slope_v_per_day"], rel=0.1)
    # Masking the detected windows recovers the clean slope at least as well
    # as leaving them in (belt and braces on top of the rolling-median fit).
    assert (abs(proj_masked["slope_v_per_day"] - proj_clean["slope_v_per_day"])
            <= abs(proj_unmasked["slope_v_per_day"] - proj_clean["slope_v_per_day"]) + 1e-9)


def test_battery_replace_projection_without_self_tests_is_unaffected():
    df = _bv_cap_frame(days=90, add_dips=False)
    assert battery_replace_projection(df) == battery_replace_projection(df, self_tests=None)
    assert battery_replace_projection(df) == battery_replace_projection(df, self_tests=pd.DataFrame())


# ====================================================================== payload markers (_panel_bc)
def test_panel_bc_markers_from_self_tests():
    df = _datalog(10)
    tests = pd.DataFrame({"ts": [df["ts"].iloc[3]]})
    panel = _panel_bc(df, tests)
    assert len(panel["markers"]) == 1
    assert panel["markers"][0]["type"] == "dot"
    assert panel["markers"][0]["x"] == _ms_list(df["ts"].iloc[[3]])[0]
    assert panel["markers"][0]["y"] == pytest.approx(100.0)


def test_panel_bc_no_self_tests_is_no_markers():
    df = _datalog(10)
    assert _panel_bc(df, None)["markers"] == []
    assert _panel_bc(df, pd.DataFrame(columns=["ts"]))["markers"] == []


def test_panel_bc_none_on_empty_datalog():
    assert _panel_bc(pd.DataFrame(), None) is None


# ====================================================================== payload markers + subtitle (build_dashboard)
def test_build_dashboard_bc_subtitle_and_markers_in_payload():
    df = _datalog(20)
    df.loc[5:6, "Battery Capacity"] = 95.0
    df.loc[5:6, "Battery Voltage"] = 26.9
    html = build_dashboard(**_dashboard_inputs(df))
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    payload = __import__("json").loads(m.group(1).replace("<\\/", "</"))
    bc = payload["panels"]["bc"]
    assert len(bc["markers"]) == 1
    assert bc["markers"][0]["type"] == "dot"
    assert "1 self-test" in html
    assert "0.50" in html


def test_build_dashboard_bc_subtitle_default_when_no_self_tests():
    df = _datalog(20)                       # perfectly flat: nothing to detect
    html = build_dashboard(**_dashboard_inputs(df))
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    payload = __import__("json").loads(m.group(1).replace("<\\/", "</"))
    assert payload["panels"]["bc"]["markers"] == []
    assert "% capacity" in html
    assert "self-test" not in html


def test_build_dashboard_spanish_self_test_subtitle(monkeypatch):
    monkeypatch.setattr(cfg, "DASHBOARD_LANGUAGE", "es")
    try:
        df = _datalog(20)
        df.loc[5:6, "Battery Capacity"] = 95.0
        df.loc[5:6, "Battery Voltage"] = 26.9
        html = build_dashboard(**_dashboard_inputs(df))
        assert "autoprueba" in html
        assert "caída media" in html
    finally:
        pass


def test_build_dashboard_explicit_self_tests_argument_used_over_default():
    """When self_tests is passed explicitly, build_dashboard must not
    recompute it from datalog_df -- a caller's own detection (for example
    event-based) wins."""
    df = _datalog(20)     # flat: the auto shape-detector would find nothing
    tests = pd.DataFrame({
        "ts": [df["ts"].iloc[3]], "dip_start": [df["ts"].iloc[2]],
        "dip_end": [df["ts"].iloc[4]], "capacity_drop_pct": [5.0],
        "sag_v": [0.4], "source": ["event"],
    })
    html = build_dashboard(**_dashboard_inputs(df), self_tests=tests)
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    payload = __import__("json").loads(m.group(1).replace("<\\/", "</"))
    assert len(payload["panels"]["bc"]["markers"]) == 1
    assert "1 self-test" in html


# ====================================================================== console (analyze_ups.py integration)
def _c(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def _write_selftest_agent(agent, days=5, cadence_min=20, dip_at=10, dip_len=3, dip_cap=8.0,
                          dip_every_days=None):
    agent.mkdir(parents=True, exist_ok=True)
    start = datetime(2026, 5, 1)
    n = int(days * 24 * 60 / cadence_min)
    period = int(dip_every_days * 24 * 60 / cadence_min) if dip_every_days else None
    lines = ["Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity"]
    for i in range(n):
        t = start + pd.Timedelta(minutes=cadence_min * i)
        cap = 100.0
        if period:
            if any(s <= i < s + dip_len for s in range(dip_at, n, period)):
                cap -= dip_cap
        elif dip_at <= i < dip_at + dip_len:
            cap -= dip_cap
        lines.append(f"{t:%m/%d/%Y %H:%M:%S}\t120,0\t27,4\t15,0\t{_c(cap)}")
    (agent / "DataLog").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return agent


def test_console_reports_self_test_count_and_honest_trend_floor(tmp_path, monkeypatch, capsys):
    import analyze_ups
    monkeypatch.setattr(cfg, "SIZE_HISTORY_CSV", tmp_path / "size_history.csv")
    agent = _write_selftest_agent(tmp_path / "agent")
    conf = tmp_path / "config.toml"
    conf.write_text("[archive]\nenabled = false\n", encoding="utf-8")
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot", "--config", str(conf)])
    out = capsys.readouterr().out
    assert "BATTERY SELF-TESTS" in out
    assert "Detected: 1" in out
    assert "not enough test history to trend a rate" in out


def test_console_reports_sag_trend_when_history_available(tmp_path, monkeypatch, capsys):
    import analyze_ups
    monkeypatch.setattr(cfg, "SIZE_HISTORY_CSV", tmp_path / "size_history.csv")
    agent = _write_selftest_agent(tmp_path / "agent", days=70, dip_every_days=2)
    conf = tmp_path / "config.toml"
    conf.write_text("[archive]\nenabled = false\n", encoding="utf-8")
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot", "--config", str(conf)])
    out = capsys.readouterr().out
    assert "Detected: " in out
    assert "Sag trend" in out
