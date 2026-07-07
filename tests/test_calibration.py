"""Unit tests for runtime-curve calibration from observed discharges (roadmap
item 16).

Every real on-battery outage is a measurement: the EventLog spans give the
exact duration, the DataLog gives the battery capacity consumed, and the
energylog gives the mean power draw during the span. `calibrate_runtime_curve`
in `pcss/stats.py` turns accumulated observations like these into a fitted
"capacity percent per minute at W watts" model (a through-origin fit of drain
rate against power), which `_panel_rt` in `pcss/dashboard.py` draws as a
second, measured curve next to the configured `[runtime_curve]` line. Below
`[runtime_curve] calibration_min_episodes` (default 3) usable observations,
the honest result carries no curve at all — the same floor pattern as
`battery_replace_projection`'s `battery_trend_min_days`.
"""
from __future__ import annotations

import pandas as pd
import pytest

from pcss import config
from pcss.dashboard import PALETTES, _panel_rt, build_dashboard
from pcss.stats import calibrate_runtime_curve

PAL = PALETTES["dark"]


def _spans(pairs):
    """Synthetic `on_battery_spans`-shaped frame: one closed span per (start,
    end) pair."""
    rows = [
        {"start": pd.Timestamp(s), "end": pd.Timestamp(e),
         "duration_min": (pd.Timestamp(e) - pd.Timestamp(s)).total_seconds() / 60.0,
         "open": False}
        for s, e in pairs
    ]
    return pd.DataFrame(rows, columns=["start", "end", "duration_min", "open"])


def _episode(start, power_w, duration_min, drop_pct, dl_rows, energy_rows):
    """Append one candidate discharge observation: a DataLog sample one
    minute before the span starts, one one minute after it ends (bracketing
    the span the way `calibrate_runtime_curve` reads capacity consumed), and
    an energylog sample at the span's midpoint. Returns (start, end) so the
    caller can build the matching span row."""
    start = pd.Timestamp(start)
    end = start + pd.Timedelta(minutes=duration_min)
    dl_rows.append({"ts": start - pd.Timedelta(minutes=1), "Battery Capacity": 100.0})
    dl_rows.append({"ts": end + pd.Timedelta(minutes=1), "Battery Capacity": 100.0 - drop_pct})
    mid = start + (end - start) / 2
    energy_rows.append({"ts": mid, "power_w": power_w, "interval_sec": 300})
    return start, end


def _frames(dl_rows, energy_rows):
    datalog_df = pd.DataFrame(dl_rows).sort_values("ts").reset_index(drop=True)
    energy_df = pd.DataFrame(energy_rows).sort_values("ts").reset_index(drop=True)
    return datalog_df, energy_df


# ---------------------------------------------------------------- config default
def test_config_default_is_three_episodes():
    assert config.CALIBRATION_MIN_EPISODES == 3


def test_load_config_overrides_calibration_min_episodes(tmp_path):
    saved = config.CALIBRATION_MIN_EPISODES
    try:
        conf = tmp_path / "config.toml"
        conf.write_text("[runtime_curve]\ncalibration_min_episodes = 5\n", encoding="utf-8")
        config.load_config(conf)
        assert config.CALIBRATION_MIN_EPISODES == 5
    finally:
        config.CALIBRATION_MIN_EPISODES = saved


# ---------------------------------------------------------------- calibrate_runtime_curve
def test_clean_multi_episode_fit_recovers_known_k():
    """Four well-separated discharges at different power levels, each with an
    exact drop = k_true * power * duration, must recover k_true."""
    k_true = 0.01
    dl_rows: list[dict] = []
    energy_rows: list[dict] = []
    spans = []
    for i, (power, duration) in enumerate(
            [(100.0, 10.0), (200.0, 5.0), (400.0, 2.5), (300.0, 4.0)]):
        drop = k_true * power * duration
        spans.append(_episode(f"2026-01-{1 + i:02d} 00:00", power, duration, drop,
                              dl_rows, energy_rows))
    datalog_df, energy_df = _frames(dl_rows, energy_rows)

    result = calibrate_runtime_curve(_spans(spans), datalog_df, energy_df)
    assert result["status"] == "calibrated"
    assert result["n_episodes"] == 4
    assert result["k"] == pytest.approx(k_true, rel=1e-6)
    assert result["watts"] is not None and result["minutes"] is not None
    assert len(result["watts"]) == len(result["minutes"])
    assert all(w > 0 for w in result["watts"])   # the zero-watt point is excluded
    w0 = result["watts"][0]
    assert result["minutes"][0] == pytest.approx(100.0 / (k_true * w0), rel=1e-6)


def test_capacity_drop_floor_excludes_second_long_blips():
    """Sub-percentage-point capacity drops (the roadmap's "lasted seconds,
    drains no measurable capacity" outages) must not become observations,
    even though their spans are otherwise well-formed."""
    dl_rows: list[dict] = []
    energy_rows: list[dict] = []
    spans = []
    for i, (power, duration) in enumerate([(100.0, 20.0), (200.0, 10.0)]):
        drop = 0.01 * power * duration
        spans.append(_episode(f"2026-02-{1 + i:02d} 00:00", power, duration, drop,
                              dl_rows, energy_rows))
    for i, power in enumerate([150.0, 250.0, 350.0]):
        # 3-second blip, well under the 1-percentage-point floor.
        spans.append(_episode(f"2026-02-{10 + i:02d} 00:00", power, 0.05, 0.1,
                              dl_rows, energy_rows))
    datalog_df, energy_df = _frames(dl_rows, energy_rows)

    result = calibrate_runtime_curve(_spans(spans), datalog_df, energy_df, min_episodes=2)
    assert result["status"] == "calibrated"
    assert result["n_episodes"] == 2   # the three blips were excluded


def test_missing_power_sample_is_excluded():
    """A span with a real capacity drop but no energylog coverage nearby
    (shorter than one energylog interval, or simply unlogged) cannot be
    priced in watts, so it must not become an observation."""
    dl_rows: list[dict] = []
    energy_rows: list[dict] = []
    spans = []
    for i, (power, duration) in enumerate([(100.0, 10.0), (200.0, 5.0)]):
        drop = 0.01 * power * duration
        spans.append(_episode(f"2026-03-{1 + i:02d} 00:00", power, duration, drop,
                              dl_rows, energy_rows))
    # A third valid discharge (capacity drop is fine) with no energylog
    # sample anywhere nearby.
    start = pd.Timestamp("2026-03-10 00:00")
    end = start + pd.Timedelta(minutes=8)
    dl_rows.append({"ts": start - pd.Timedelta(minutes=1), "Battery Capacity": 100.0})
    dl_rows.append({"ts": end + pd.Timedelta(minutes=1), "Battery Capacity": 92.0})
    spans.append((start, end))
    datalog_df, energy_df = _frames(dl_rows, energy_rows)

    result = calibrate_runtime_curve(_spans(spans), datalog_df, energy_df, min_episodes=2)
    assert result["status"] == "calibrated"
    assert result["n_episodes"] == 2   # the power-less span did not count


def test_below_floor_returns_honest_status():
    """Two usable observations, below the default floor of three: the result
    must say so plainly and carry no curve."""
    dl_rows: list[dict] = []
    energy_rows: list[dict] = []
    spans = []
    for i, (power, duration) in enumerate([(100.0, 10.0), (200.0, 5.0)]):
        drop = 0.01 * power * duration
        spans.append(_episode(f"2026-04-{1 + i:02d} 00:00", power, duration, drop,
                              dl_rows, energy_rows))
    datalog_df, energy_df = _frames(dl_rows, energy_rows)

    result = calibrate_runtime_curve(_spans(spans), datalog_df, energy_df)
    assert result["status"] == "insufficient_evidence"
    assert result["n_episodes"] == 2
    assert result["min_episodes"] == 3
    assert result["k"] is None
    assert result["watts"] is None
    assert result["minutes"] is None


def test_empty_spans_or_missing_columns_is_insufficient():
    assert calibrate_runtime_curve(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())["status"] \
        == "insufficient_evidence"
    dl_rows, energy_rows = [], []
    spans = [_episode("2026-01-01 00:00", 100.0, 10.0, 5.0, dl_rows, energy_rows)]
    datalog_df, energy_df = _frames(dl_rows, energy_rows)
    no_capacity = datalog_df.drop(columns=["Battery Capacity"])
    assert calibrate_runtime_curve(_spans(spans), no_capacity, energy_df)["status"] \
        == "insufficient_evidence"
    no_power = energy_df.drop(columns=["power_w"])
    assert calibrate_runtime_curve(_spans(spans), datalog_df, no_power)["status"] \
        == "insufficient_evidence"


def test_open_span_is_excluded():
    """An outage still open at the end of the log (no `end`) cannot be a
    completed discharge observation."""
    dl_rows: list[dict] = []
    energy_rows: list[dict] = []
    spans = []
    for i, (power, duration) in enumerate([(100.0, 10.0), (200.0, 5.0)]):
        drop = 0.01 * power * duration
        spans.append(_episode(f"2026-06-{1 + i:02d} 00:00", power, duration, drop,
                              dl_rows, energy_rows))
    datalog_df, energy_df = _frames(dl_rows, energy_rows)
    spans_df = _spans(spans)
    open_row = pd.DataFrame([{
        "start": pd.Timestamp("2026-06-20"), "end": pd.NaT,
        "duration_min": float("nan"), "open": True,
    }])
    spans_with_open = pd.concat([spans_df, open_row], ignore_index=True)

    result = calibrate_runtime_curve(spans_with_open, datalog_df, energy_df, min_episodes=2)
    assert result["status"] == "calibrated"
    assert result["n_episodes"] == 2


def test_battery_boundary_filter_excludes_pre_replacement_episodes():
    """`battery_replace_projection` reuses `latest_battery_replacement` to
    segment its fit at the newest battery_replaced annotation; calibration
    must reuse the same boundary so a degraded old battery's discharges
    don't contaminate the fresh battery's fit."""
    boundary = pd.Timestamp("2026-05-15")
    dl_rows: list[dict] = []
    energy_rows: list[dict] = []
    spans = []
    # Pre-boundary: an old, badly degraded battery.
    for i, (power, duration) in enumerate([(100.0, 10.0), (200.0, 5.0), (300.0, 4.0)]):
        drop = 0.05 * power * duration
        spans.append(_episode(f"2026-05-{1 + i:02d} 00:00", power, duration, drop,
                              dl_rows, energy_rows))
    # Post-boundary: the fresh battery's true rate.
    k_true = 0.01
    for i, (power, duration) in enumerate([(100.0, 10.0), (200.0, 5.0), (400.0, 2.5)]):
        drop = k_true * power * duration
        spans.append(_episode(f"2026-05-{20 + i:02d} 00:00", power, duration, drop,
                              dl_rows, energy_rows))
    datalog_df, energy_df = _frames(dl_rows, energy_rows)
    spans_df = _spans(spans)
    annotations = pd.DataFrame({
        "date": [boundary.date()], "kind": ["battery_replaced"], "label": ["new battery"],
    })

    result = calibrate_runtime_curve(spans_df, datalog_df, energy_df, annotations=annotations)
    assert result["status"] == "calibrated"
    assert result["n_episodes"] == 3          # only the post-boundary episodes
    assert result["k"] == pytest.approx(k_true, rel=1e-6)

    unfiltered = calibrate_runtime_curve(spans_df, datalog_df, energy_df)
    assert unfiltered["n_episodes"] == 6
    assert unfiltered["k"] != pytest.approx(k_true, rel=0.2)


def test_future_dated_annotation_leaves_fit_unfiltered():
    dl_rows: list[dict] = []
    energy_rows: list[dict] = []
    spans = []
    for i, (power, duration) in enumerate([(100.0, 10.0), (200.0, 5.0), (300.0, 4.0)]):
        drop = 0.01 * power * duration
        spans.append(_episode(f"2026-07-{1 + i:02d} 00:00", power, duration, drop,
                              dl_rows, energy_rows))
    datalog_df, energy_df = _frames(dl_rows, energy_rows)
    spans_df = _spans(spans)
    future_date = (datalog_df["ts"].iloc[-1] + pd.Timedelta(days=30)).date()
    annotations = pd.DataFrame({
        "date": [future_date], "kind": ["battery_replaced"], "label": ["planned"],
    })
    with_future = calibrate_runtime_curve(spans_df, datalog_df, energy_df, annotations=annotations)
    without = calibrate_runtime_curve(spans_df, datalog_df, energy_df)
    assert with_future["n_episodes"] == without["n_episodes"]
    assert with_future["k"] == pytest.approx(without["k"])


# ---------------------------------------------------------------- payload (_panel_rt)
def test_panel_rt_adds_measured_series_when_calibrated():
    calibration = {"status": "calibrated", "n_episodes": 4, "min_episodes": 3,
                   "k": 0.01, "watts": [100.0, 200.0], "minutes": [10.0, 5.0]}
    energy_df = pd.DataFrame({
        "ts": pd.date_range("2026-01-01", periods=3, freq="5min"),
        "power_w": [250.0, 250.0, 250.0], "interval_sec": 300,
    })
    panel, latest_w, latest_rt = _panel_rt(energy_df, PAL, calibration)
    assert len(panel["series"]) == 2
    assert panel["series"][1]["x"] == [100.0, 200.0]
    assert panel["series"][1]["y"] == [10.0, 5.0]
    assert panel["legend"] is True
    assert latest_w == pytest.approx(250.0)


def test_panel_rt_no_measured_series_below_floor():
    calibration = {"status": "insufficient_evidence", "n_episodes": 1, "min_episodes": 3,
                   "k": None, "watts": None, "minutes": None}
    panel, _, _ = _panel_rt(pd.DataFrame(), PAL, calibration)
    assert len(panel["series"]) == 1


def test_panel_rt_no_calibration_argument_matches_none():
    panel_default, w1, rt1 = _panel_rt(pd.DataFrame(), PAL)
    panel_none, w2, rt2 = _panel_rt(pd.DataFrame(), PAL, None)
    assert len(panel_default["series"]) == len(panel_none["series"]) == 1
    assert w1 == w2 and rt1 == rt2


# ---------------------------------------------------------------- payload (build_dashboard)
def _minimal_dashboard_inputs():
    return dict(
        datalog_df=pd.DataFrame(), energy_df=pd.DataFrame(), hist=pd.DataFrame(),
        dl_stats={}, hist_stats={},
        sizes={"DataLog": 0, "EventLog (binary)": 0, "energylog/": 0},
        energy_summary={}, stats_table=pd.DataFrame(), gaps=pd.DataFrame(),
        voltage_anomalies=pd.DataFrame(), high_load_episodes=pd.DataFrame(), crossval={},
    )


def test_build_dashboard_rt_subtitle_names_discharge_count_when_calibrated():
    calibration = {"status": "calibrated", "n_episodes": 4, "min_episodes": 3,
                   "k": 0.01, "watts": [100.0, 200.0], "minutes": [10.0, 5.0]}
    html = build_dashboard(**_minimal_dashboard_inputs(), calibration=calibration)
    assert "measured from 4 discharges" in html


def test_build_dashboard_rt_subtitle_honest_below_floor():
    calibration = {"status": "insufficient_evidence", "n_episodes": 1, "min_episodes": 3,
                   "k": None, "watts": None, "minutes": None}
    html = build_dashboard(**_minimal_dashboard_inputs(), calibration=calibration)
    assert "not enough discharge data yet" in html
    assert "1/3" in html


def test_build_dashboard_rt_subtitle_default_when_calibration_omitted():
    """With no calibration argument at all, the dashboard must not crash and
    must show the honest floor note (as if zero episodes were observed)."""
    html = build_dashboard(**_minimal_dashboard_inputs())
    assert "not enough discharge data yet" in html


def test_build_dashboard_spanish_calibration_note(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_LANGUAGE", "es")
    try:
        calibration = {"status": "calibrated", "n_episodes": 4, "min_episodes": 3,
                       "k": 0.01, "watts": [100.0, 200.0], "minutes": [10.0, 5.0]}
        html = build_dashboard(**_minimal_dashboard_inputs(), calibration=calibration)
        assert "descargas" in html
    finally:
        pass
