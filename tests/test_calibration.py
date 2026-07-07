"""Unit tests for runtime-curve calibration from observed discharges.

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

import json

import pandas as pd
import pytest

from pcss import config
from pcss.dashboard import _panel_rt, build_dashboard
from pcss.stats import calibrate_runtime_curve


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
    """Sub-percentage-point capacity drops (the "lasted seconds,
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
    # Even below the floor, the observed range from the usable observations is
    # reported: two discharges at 100 W and 200 W.
    assert result["watts_observed_min"] == pytest.approx(100.0)
    assert result["watts_observed_max"] == pytest.approx(200.0)


# ---------------------------------------------------------------- observed watt range
def test_calibrated_result_carries_observed_watt_range():
    """The measured overlay extrapolates one global k across all configured
    watt points, so the honest range is the span of the observations it was
    fitted from — here 120 W to 480 W."""
    k_true = 0.01
    dl_rows: list[dict] = []
    energy_rows: list[dict] = []
    spans = []
    for i, (power, duration) in enumerate(
            [(120.0, 10.0), (200.0, 5.0), (480.0, 2.5), (300.0, 4.0)]):
        drop = k_true * power * duration
        spans.append(_episode(f"2026-08-{1 + i:02d} 00:00", power, duration, drop,
                              dl_rows, energy_rows))
    datalog_df, energy_df = _frames(dl_rows, energy_rows)
    result = calibrate_runtime_curve(_spans(spans), datalog_df, energy_df)
    assert result["status"] == "calibrated"
    assert result["watts_observed_min"] == pytest.approx(120.0)
    assert result["watts_observed_max"] == pytest.approx(480.0)


def test_empty_result_has_null_observed_range():
    result = calibrate_runtime_curve(pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
    assert result["watts_observed_min"] is None
    assert result["watts_observed_max"] is None


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


def test_back_to_back_outages_share_the_same_datalog_bracket():
    """Two outages closer together than the DataLog's own sampling cadence
    fall inside the same gap between samples, so both spans bracket to the
    identical before/after pair: the lookup finds the last sample at-or-
    before each span's start and the first sample at-or-after its end, with
    no per-span isolation. This pins the CURRENT trade-off (polish item
    A7b, see the comment at the bracket lookup in calibrate_runtime_curve)
    rather than fixing it: both spans are still counted as separate
    episodes, but neither reading is independent — the whole 20-minute
    inter-sample capacity drop gets attributed to each span's own, much
    shorter, duration."""
    # Only two DataLog samples span the whole window, 20 minutes apart (the
    # PCSS factory default cadence) -- both outages fall inside that one gap.
    dl_rows = [
        {"ts": pd.Timestamp("2026-01-01 00:00"), "Battery Capacity": 100.0},
        {"ts": pd.Timestamp("2026-01-01 00:20"), "Battery Capacity": 90.0},
    ]
    energy_rows = [
        {"ts": pd.Timestamp("2026-01-01 00:00"), "power_w": 500.0, "interval_sec": 300},
        {"ts": pd.Timestamp("2026-01-01 00:20"), "power_w": 500.0, "interval_sec": 300},
    ]
    spans = _spans([
        ("2026-01-01 00:02", "2026-01-01 00:06"),
        ("2026-01-01 00:10", "2026-01-01 00:14"),
    ])
    datalog_df, energy_df = _frames(dl_rows, energy_rows)

    result = calibrate_runtime_curve(spans, datalog_df, energy_df, min_episodes=2)
    assert result["status"] == "calibrated"
    assert result["n_episodes"] == 2   # both spans kept, not deduplicated
    # Both readings share the same (power, rate) pair drawn from the shared
    # bracket: 10 capacity points over 4 minutes at 500 W, for both spans.
    expected_k = (10.0 / 4.0) / 500.0
    assert result["k"] == pytest.approx(expected_k, rel=1e-9)


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
    panel, latest_w, latest_rt = _panel_rt(energy_df, calibration)
    assert len(panel["series"]) == 2
    assert panel["series"][1]["x"] == [100.0, 200.0]
    assert panel["series"][1]["y"] == [10.0, 5.0]
    assert panel["legend"] is True
    assert latest_w == pytest.approx(250.0)


def test_panel_rt_no_measured_series_below_floor():
    calibration = {"status": "insufficient_evidence", "n_episodes": 1, "min_episodes": 3,
                   "k": None, "watts": None, "minutes": None}
    panel, _, _ = _panel_rt(pd.DataFrame(), calibration)
    assert len(panel["series"]) == 1


def test_panel_rt_no_calibration_argument_matches_none():
    panel_default, w1, rt1 = _panel_rt(pd.DataFrame())
    panel_none, w2, rt2 = _panel_rt(pd.DataFrame(), None)
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


def test_build_dashboard_rt_subtitle_names_observed_watt_range():
    """The honesty note names the observed load span the single global k was
    fitted across."""
    calibration = {"status": "calibrated", "n_episodes": 4, "min_episodes": 3,
                   "k": 0.01, "watts": [100.0, 200.0], "minutes": [10.0, 5.0],
                   "watts_observed_min": 120.0, "watts_observed_max": 480.0}
    html = build_dashboard(**_minimal_dashboard_inputs(), calibration=calibration)
    assert "measured from 4 discharges near 120-480 W" in html


def test_build_dashboard_rt_subtitle_degenerate_single_wattage():
    """When every usable discharge sat at one wattage, the note reads a single
    figure rather than a nonsensical N-N range."""
    calibration = {"status": "calibrated", "n_episodes": 4, "min_episodes": 3,
                   "k": 0.01, "watts": [100.0, 200.0], "minutes": [10.0, 5.0],
                   "watts_observed_min": 250.0, "watts_observed_max": 250.0}
    html = build_dashboard(**_minimal_dashboard_inputs(), calibration=calibration)
    assert "measured from 4 discharges near 250 W" in html
    assert "250-250" not in html


def test_build_dashboard_rt_subtitle_spanish_observed_range(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_LANGUAGE", "es")
    calibration = {"status": "calibrated", "n_episodes": 4, "min_episodes": 3,
                   "k": 0.01, "watts": [100.0, 200.0], "minutes": [10.0, 5.0],
                   "watts_observed_min": 120.0, "watts_observed_max": 480.0}
    html = build_dashboard(**_minimal_dashboard_inputs(), calibration=calibration)
    assert "cerca de 120-480 W" in html


# ---------------------------------------------------------------- console + --json surfaces
def test_calibration_console_lines_calibrated_names_k_and_range():
    import analyze_ups
    cal = {"status": "calibrated", "n_episodes": 4, "min_episodes": 3, "k": 0.01234,
           "watts": [100.0], "minutes": [10.0],
           "watts_observed_min": 120.0, "watts_observed_max": 480.0}
    text = "\n".join(analyze_ups._calibration_console_lines(cal))
    assert "Fitted from 4" in text
    assert "0.01234" in text        # k printed to 5 decimals
    assert "120-480 W" in text


def test_calibration_console_lines_below_floor_is_honest():
    import analyze_ups
    cal = {"status": "insufficient_evidence", "n_episodes": 1, "min_episodes": 3,
           "k": None, "watts": None, "minutes": None,
           "watts_observed_min": 200.0, "watts_observed_max": 200.0}
    lines = analyze_ups._calibration_console_lines(cal)
    assert any("1/3" in ln for ln in lines)
    assert any("not enough" in ln.lower() for ln in lines)


def test_calibration_for_json_calibrated_is_clean():
    import analyze_ups
    cal = {"status": "calibrated", "n_episodes": 4, "min_episodes": 3, "k": 0.01,
           "watts": [100.0], "minutes": [10.0],
           "watts_observed_min": 120.0, "watts_observed_max": 480.0}
    j = analyze_ups._calibration_for_json(cal)
    assert j["status"] == "calibrated"
    assert j["k"] == pytest.approx(0.01)
    assert j["n_episodes"] == 4
    assert j["watts_observed_min"] == pytest.approx(120.0)
    assert j["watts_observed_max"] == pytest.approx(480.0)


def test_calibration_for_json_is_nan_safe():
    import analyze_ups
    cal = {"status": "insufficient_evidence", "n_episodes": 0, "min_episodes": 3,
           "k": float("nan"), "watts": None, "minutes": None,
           "watts_observed_min": float("nan"), "watts_observed_max": None}
    j = analyze_ups._calibration_for_json(cal)
    assert j["k"] is None
    assert j["watts_observed_min"] is None
    assert j["watts_observed_max"] is None
    # The whole thing must serialize as standard JSON (no NaN token).
    assert "NaN" not in json.dumps(j)


def _write_calibration_agent(agent):
    """A minimal DataLog + energylog agent with no EventLog, so a real
    pipeline run reaches the calibration surfaces with an honest
    insufficient-evidence result (no authoritative outage spans to fit)."""
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "energylog").mkdir(exist_ok=True)
    start = pd.Timestamp("2026-05-01 00:00")
    dl_lines = ["Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity"]
    for i in range(48):
        t = start + pd.Timedelta(minutes=20 * i)
        vals = f"{120.0:.1f}\t{27.0:.1f}\t{20.0:.1f}\t{100.0:.1f}".replace(".", ",")
        dl_lines.append(f"{t:%m/%d/%Y %H:%M:%S}\t{vals}")
    (agent / "DataLog").write_text("\n".join(dl_lines) + "\n", encoding="utf-8")
    epoch_2010 = pd.Timestamp("2010-01-01")
    el = ["# $month=2026-05", "# $interval=300", "# $calculatedMaxLoad=1400.0"]
    for i in range(288):
        t = start + pd.Timedelta(minutes=5 * i)
        secs = (t - epoch_2010).total_seconds()
        el.append(f"{secs:.0f};null;20.0;280.0")
    (agent / "energylog" / "2026-05.log").write_text("\n".join(el) + "\n", encoding="utf-8")
    return agent


def _cal_config(tmp_path):
    conf = tmp_path / "config.toml"
    conf.write_text("[archive]\nenabled = false\n", encoding="utf-8")
    return conf


def test_pipeline_reports_calibration_console_and_json(tmp_path, capsys):
    import analyze_ups
    agent = _write_calibration_agent(tmp_path / "agent")
    jpath = tmp_path / "summary.json"
    code = analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                             "--no-browser", "--no-snapshot", "--json", str(jpath),
                             "--config", str(_cal_config(tmp_path))])
    assert code == 0
    out = capsys.readouterr().out
    assert "RUNTIME-CURVE CALIBRATION" in out
    assert "not enough discharge data" in out.lower()
    data = json.loads(jpath.read_text(encoding="utf-8"))
    assert "calibration" in data
    assert data["calibration"]["status"] == "insufficient_evidence"
