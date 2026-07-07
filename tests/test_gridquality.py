"""Unit tests for the grid-quality trend (roadmap item 28).

`detect_voltage_anomalies` already finds out-of-envelope samples, but
nothing aggregates them over time. `grid_quality_trend` in `pcss/stats.py`
turns a DataLog frame, `detect_gaps` output, and an on-battery episodes
frame (the caller's already-resolved authoritative-EventLog-or-inferred
frame — this function does not re-implement that precedence) into one row
per calendar month: sag count, swell count, interruption count, a
per-recorded-day event rate (so a gap-heavy month does not read as unusually
quiet), mean depth per direction, and the single worst envelope-violation
sample. Every reader of this result must say the counts are events visible
at the DataLog's own sampling cadence — the same item 6 caveat.

Tests are grouped by layer: classification/merging of sag and swell events,
per-recorded-day normalization, mean-depth/worst-event arithmetic, the
dashboard reference table, the console section, and the --json key.
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd
import pytest

from pcss import config
from pcss.dashboard import build_dashboard
from pcss.stats import detect_gaps, grid_quality_trend


def _dl(readings, start="2026-01-01 00:00", freq="20min") -> pd.DataFrame:
    """A DataLog-shaped frame (ts, Line Voltage) from a flat list of
    voltages at the default 20-minute cadence."""
    ts = pd.date_range(start, periods=len(readings), freq=freq)
    return pd.DataFrame({"ts": ts, "Line Voltage": np.array(readings, dtype=float)})


def _episodes(starts, ends=None) -> pd.DataFrame:
    """A start/end-shaped interruptions frame — the same shape
    `on_battery_spans`/`detect_on_battery_episodes` both already produce."""
    starts = [pd.Timestamp(s) for s in starts]
    ends = starts if ends is None else [pd.Timestamp(e) for e in ends]
    return pd.DataFrame({"start": starts, "end": ends})


# ---------------------------------------------------------------- classify: direction split
def test_classify_sag_below_low_envelope():
    df = _dl([120.0, 110.0, 120.0])   # 110 < 114 low bound
    gq = grid_quality_trend(df)
    assert gq.iloc[0]["sag_count"] == 1
    assert gq.iloc[0]["swell_count"] == 0


def test_classify_swell_above_high_envelope():
    df = _dl([120.0, 130.0, 120.0])   # 130 > 126 high bound
    gq = grid_quality_trend(df)
    assert gq.iloc[0]["sag_count"] == 0
    assert gq.iloc[0]["swell_count"] == 1


def test_classify_in_envelope_samples_are_not_events():
    df = _dl([114.0, 120.0, 126.0])   # both bounds inclusive of "normal"
    gq = grid_quality_trend(df)
    assert gq.iloc[0]["sag_count"] == 0
    assert gq.iloc[0]["swell_count"] == 0


def test_classify_respects_explicit_envelope_override():
    df = _dl([100.0, 100.0])
    assert grid_quality_trend(df).iloc[0]["sag_count"] == 1
    assert grid_quality_trend(df, voltage_low=90.0).iloc[0]["sag_count"] == 0


# ---------------------------------------------------------------- classify: consecutive-sample merging
def test_classify_consecutive_sags_merge_into_one_event():
    df = _dl([120.0, 110.0, 108.0, 109.0, 120.0])   # 3 consecutive sags
    gq = grid_quality_trend(df)
    assert gq.iloc[0]["sag_count"] == 1


def test_classify_two_separate_sag_runs_are_two_events():
    df = _dl([120.0, 110.0, 120.0, 120.0, 108.0, 120.0])
    gq = grid_quality_trend(df)
    assert gq.iloc[0]["sag_count"] == 2


def test_classify_direction_change_without_normal_between_is_two_events():
    """A sample that drops straight from a sag to a swell (no in-envelope
    sample between) still starts a new event — "consecutive samples in the
    SAME direction" merge, a direction change does not."""
    df = _dl([120.0, 110.0, 130.0, 120.0])
    gq = grid_quality_trend(df)
    assert gq.iloc[0]["sag_count"] == 1
    assert gq.iloc[0]["swell_count"] == 1


def test_classify_nan_sample_breaks_a_run():
    df = _dl([120.0, 110.0, np.nan, 110.0, 120.0])
    gq = grid_quality_trend(df)
    assert gq.iloc[0]["sag_count"] == 2


# ---------------------------------------------------------------- interruptions
def test_interruption_counted_by_episode_start_month():
    df = _dl([120.0] * 4, start="2026-01-01 00:00")
    eps = _episodes(["2026-01-01 00:20"])
    gq = grid_quality_trend(df, episodes=eps)
    assert gq.iloc[0]["interruption_count"] == 1


def test_interruption_absent_when_episodes_none():
    df = _dl([120.0] * 3)
    gq = grid_quality_trend(df)
    assert gq.iloc[0]["interruption_count"] == 0


def test_interruption_count_does_not_care_which_episode_frame_shape():
    """The function takes whatever start/end frame the caller already
    resolved (authoritative EventLog spans, or the DataLog-inferred
    fallback) — it does not re-implement that precedence, so an
    `on_battery_spans`-shaped frame (which also carries `duration_min` and
    `open`) works exactly like a plain start/end frame."""
    df = _dl([120.0] * 4, start="2026-01-01 00:00")
    spans = pd.DataFrame({
        "start": [pd.Timestamp("2026-01-01 00:20")],
        "end": [pd.Timestamp("2026-01-01 00:40")],
        "duration_min": [20.0],
        "open": [False],
    })
    gq = grid_quality_trend(df, episodes=spans)
    assert gq.iloc[0]["interruption_count"] == 1


# ---------------------------------------------------------------- normalization: recorded days
def test_recorded_days_subtracts_known_gap():
    # 00:00, 00:20, then a gap to 03:00 (160 min gap by detect_gaps' own math
    # relative to the 20-min expected interval), then one more sample.
    ts = [pd.Timestamp("2026-01-01 00:00"), pd.Timestamp("2026-01-01 00:20"),
          pd.Timestamp("2026-01-01 03:00")]
    df = pd.DataFrame({"ts": ts, "Line Voltage": [120.0, 120.0, 120.0]})
    gaps = detect_gaps(df, expected_interval_min=20)
    assert len(gaps) == 1
    assert gaps.iloc[0]["duration_min"] == pytest.approx(160.0)

    gq = grid_quality_trend(df, gaps=gaps)
    covered_span_days = (ts[-1] - ts[0]).total_seconds() / 86400.0
    gap_days = 160.0 / 1440.0
    assert gq.iloc[0]["recorded_days"] == pytest.approx(covered_span_days - gap_days)


def test_recorded_days_with_no_gaps_is_the_full_covered_span():
    ts = pd.date_range("2026-01-01 00:00", periods=4, freq="20min")
    df = pd.DataFrame({"ts": ts, "Line Voltage": [120.0] * 4})
    gq = grid_quality_trend(df)
    expected_days = (ts[-1] - ts[0]).total_seconds() / 86400.0
    assert gq.iloc[0]["recorded_days"] == pytest.approx(expected_days)


def test_events_per_recorded_day_uses_total_event_count():
    ts = [pd.Timestamp("2026-01-01 00:00"), pd.Timestamp("2026-01-01 00:20"),
          pd.Timestamp("2026-01-01 03:00"), pd.Timestamp("2026-01-01 03:20")]
    df = pd.DataFrame({"ts": ts, "Line Voltage": [120.0, 110.0, 130.0, 120.0]})
    gaps = detect_gaps(df, expected_interval_min=20)
    eps = _episodes(["2026-01-01 03:20"])
    gq = grid_quality_trend(df, gaps=gaps, episodes=eps)
    row = gq.iloc[0]
    assert row["sag_count"] == 1
    assert row["swell_count"] == 1
    assert row["interruption_count"] == 1
    assert row["events_per_recorded_day"] == pytest.approx(3.0 / row["recorded_days"])


def test_recorded_days_zero_gives_nan_rate_not_a_crash():
    # A single sample has no span at all.
    df = _dl([120.0], freq="20min")
    gq = grid_quality_trend(df)
    assert gq.iloc[0]["recorded_days"] == pytest.approx(0.0)
    assert np.isnan(gq.iloc[0]["events_per_recorded_day"])


# ---------------------------------------------------------------- mean depth / worst event
def test_mean_sag_depth_averages_per_event_not_per_sample():
    # Event 1: two samples deep at 10 V below the low bound (mean of the
    # samples would be the same either way here, so this alone doesn't
    # discriminate) -- event 2 is a single 4 V-deep sample. Two events ->
    # mean depth is (10 + 4) / 2 = 7, not the 5-sample mean.
    df = _dl([120.0, 104.0, 104.0, 120.0, 110.0, 120.0])
    gq = grid_quality_trend(df)
    assert gq.iloc[0]["sag_count"] == 2
    assert gq.iloc[0]["sag_mean_depth_v"] == pytest.approx((10.0 + 4.0) / 2)


def test_mean_swell_depth_nan_when_no_swells():
    df = _dl([120.0, 110.0, 120.0])
    gq = grid_quality_trend(df)
    assert np.isnan(gq.iloc[0]["swell_mean_depth_v"])


def test_worst_event_picks_the_deepest_sample():
    ts = pd.date_range("2026-01-01 00:00", periods=5, freq="20min")
    # Depths: 6, 16 (worst), 0(normal), 20 swell depth... keep it simple:
    # one 6V-deep sag, one 16V-deep sag, one 4V-deep swell. Worst is the
    # 16V-deep sag.
    df = pd.DataFrame({"ts": ts, "Line Voltage": [108.0, 120.0, 98.0, 120.0, 130.0]})
    gq = grid_quality_trend(df)
    row = gq.iloc[0]
    assert row["worst_event_v"] == pytest.approx(98.0)
    assert row["worst_event_ts"] == ts[2]
    assert row["worst_event_direction"] == "sag"


def test_worst_event_none_when_only_interruptions():
    df = _dl([120.0] * 3, start="2026-01-01 00:00")
    eps = _episodes(["2026-01-01 00:20"])
    gq = grid_quality_trend(df, episodes=eps)
    row = gq.iloc[0]
    assert row["worst_event_ts"] is None
    assert row["worst_event_v"] is None
    assert row["worst_event_direction"] is None


# ---------------------------------------------------------------- empty months / empty input
def test_month_with_no_samples_is_absent():
    jan = pd.date_range("2026-01-01", periods=3, freq="20min")
    mar = pd.date_range("2026-03-01", periods=3, freq="20min")
    df = pd.DataFrame({"ts": list(jan) + list(mar), "Line Voltage": [120.0] * 6})
    gq = grid_quality_trend(df)
    assert set(gq["month"]) == {"2026-01", "2026-03"}


def test_empty_datalog_returns_empty_frame_with_documented_columns():
    gq = grid_quality_trend(pd.DataFrame())
    assert gq.empty
    assert list(gq.columns) == [
        "month", "sag_count", "swell_count", "interruption_count",
        "recorded_days", "events_per_recorded_day",
        "sag_mean_depth_v", "swell_mean_depth_v",
        "worst_event_ts", "worst_event_v", "worst_event_direction",
    ]


def test_no_line_voltage_column_returns_empty():
    df = pd.DataFrame({"ts": pd.date_range("2026-01-01", periods=3, freq="20min")})
    assert grid_quality_trend(df).empty


# ==================================================================
# Layer: dashboard reference table
# ==================================================================
def _gq_row(**overrides):
    row = {
        "month": "2026-01", "sag_count": 2, "swell_count": 1, "interruption_count": 1,
        "recorded_days": 28.0, "events_per_recorded_day": 4 / 28.0,
        "sag_mean_depth_v": 6.5, "swell_mean_depth_v": 3.0,
        "worst_event_ts": pd.Timestamp("2026-01-14 03:20"), "worst_event_v": 98.0,
        "worst_event_direction": "sag",
    }
    row.update(overrides)
    return row


def test_table_html_renders_month_row():
    # Imported lazily so the stats layer's tests collect and run before the
    # dashboard helper exists — the TDD layering this file follows.
    from pcss.dashboard import _grid_quality_table_html
    gq = pd.DataFrame([_gq_row()])
    html = _grid_quality_table_html(gq)
    assert "2026-01" in html
    assert ">2<" in html    # sag_count
    assert "98.0" in html   # worst event voltage


def test_table_html_shows_dash_for_missing_direction_mean():
    from pcss.dashboard import _grid_quality_table_html
    gq = pd.DataFrame([_gq_row(swell_count=0, swell_mean_depth_v=float("nan"))])
    html = _grid_quality_table_html(gq)
    assert "—" in html   # em dash placeholder for "no swells this month"


def _dashboard_inputs(datalog_df, gaps=None, episodes=None):
    return dict(
        datalog_df=datalog_df, energy_df=pd.DataFrame(), hist=pd.DataFrame(),
        dl_stats={}, hist_stats={},
        sizes={"DataLog": 0, "EventLog (binary)": 0, "energylog/": 0},
        energy_summary={}, stats_table=pd.DataFrame(),
        gaps=gaps if gaps is not None else pd.DataFrame(),
        voltage_anomalies=pd.DataFrame(), high_load_episodes=pd.DataFrame(), crossval={},
        on_battery=episodes if episodes is not None else pd.DataFrame(),
    )


def _payload(html: str) -> dict:
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_build_dashboard_includes_grid_quality_block_when_data_present():
    df = _dl([120.0, 110.0, 120.0], start="2026-01-01 00:00")
    html = build_dashboard(**_dashboard_inputs(df))
    assert "Grid Quality Trend" in html


def test_build_dashboard_omits_grid_quality_block_when_no_data():
    html = build_dashboard(**_dashboard_inputs(pd.DataFrame()))
    assert "Grid Quality Trend" not in html


def test_build_dashboard_cadence_wording_uses_configured_interval(monkeypatch):
    monkeypatch.setattr(config, "DATALOG_EXPECTED_INTERVAL_MIN", 15.0)
    df = _dl([120.0, 110.0, 120.0], start="2026-01-01 00:00", freq="15min")
    html = build_dashboard(**_dashboard_inputs(df))
    assert "15-min" in html
    assert "20-min" not in html


def test_build_dashboard_grid_quality_localizes_title(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_LANGUAGE", "es")
    df = _dl([120.0, 110.0, 120.0], start="2026-01-01 00:00")
    html = build_dashboard(**_dashboard_inputs(df))
    assert "Grid Quality Trend" not in html
    assert "Tendencia de Calidad de Red" in html or "calidad de red" in html.lower()


def test_build_dashboard_explicit_grid_quality_argument_used_over_default():
    """When `grid_quality` is passed explicitly, build_dashboard must not
    recompute it — a caller's own precomputed result wins, the same
    contract `self_tests`/`battery`/`baseline` already honor."""
    df = _dl([120.0] * 3, start="2026-01-01 00:00")   # nothing would flag on its own
    fake_gq = pd.DataFrame([_gq_row(month="2026-01", sag_count=99)])
    html = build_dashboard(**_dashboard_inputs(df), grid_quality=fake_gq)
    assert "99" in html


# ==================================================================
# Layer: console section (analyze_ups.py end to end)
# ==================================================================
def _write_agent_with_sag(tmp_path, start="2026-01-01 00:00", n=6, sag_at=2):
    agent = tmp_path / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    lines = ["Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity"]
    ts0 = pd.Timestamp(start)
    for i in range(n):
        t = ts0 + pd.Timedelta(minutes=20 * i)
        v = 108.0 if i == sag_at else 120.0
        v_txt = f"{v:.1f}".replace(".", ",")
        lines.append(f"{t:%m/%d/%Y %H:%M:%S}\t{v_txt}\t27,4\t15,0\t100")
    (agent / "DataLog").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return agent


def _hermetic_config(tmp_path):
    conf = tmp_path / "config.toml"
    conf.write_text("[archive]\nenabled = false\n", encoding="utf-8")
    return conf


def test_console_reports_grid_quality_section_with_cadence_wording(tmp_path, capsys):
    import analyze_ups
    agent = _write_agent_with_sag(tmp_path)
    exit_code = analyze_ups.main([
        "--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
        "--no-browser", "--no-snapshot", "--config", str(_hermetic_config(tmp_path)),
    ])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "GRID QUALITY TREND" in out
    assert "20-min" in out
    assert "1 sag" in out or "sags=1" in out or "1 sags" in out


def test_console_grid_quality_silent_message_when_no_datalog(tmp_path, capsys):
    import analyze_ups
    agent = tmp_path / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    analyze_ups.main([
        "--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
        "--no-browser", "--no-snapshot", "--config", str(_hermetic_config(tmp_path)),
    ])
    out = capsys.readouterr().out
    assert "GRID QUALITY TREND" in out
    assert "not enough data" in out.lower() or "no data" in out.lower()


# ==================================================================
# Layer: --json key
# ==================================================================
def test_json_summary_has_grid_quality_key_when_data_present(tmp_path):
    import analyze_ups
    agent = _write_agent_with_sag(tmp_path)
    j = tmp_path / "out.json"
    analyze_ups.main([
        "--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
        "--no-browser", "--quiet", "--no-snapshot",
        "--config", str(_hermetic_config(tmp_path)), "--json", str(j),
    ])
    data = json.loads(j.read_text())
    assert "grid_quality" in data
    gq = data["grid_quality"]
    # The cadence-honesty label rides the machine-readable surface too: the
    # interval comes from datalog_expected_interval_min, not a hardcoded 20.
    assert gq["cadence_min"] == pytest.approx(config.DATALOG_EXPECTED_INTERVAL_MIN)
    assert "visible at the sampling cadence" in gq["note"]
    assert len(gq["months"]) == 1
    entry = gq["months"][0]
    assert entry["month"] == "2026-01"
    assert entry["sag_count"] == 1
    assert entry["swell_count"] == 0
    # NaN never leaks into the JSON: a direction with no events is null.
    assert entry["swell_mean_depth_v"] is None


def test_json_summary_omits_grid_quality_key_when_no_datalog(tmp_path):
    import analyze_ups
    agent = tmp_path / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    j = tmp_path / "out.json"
    analyze_ups.main([
        "--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
        "--no-browser", "--quiet", "--no-snapshot",
        "--config", str(_hermetic_config(tmp_path)), "--json", str(j),
    ])
    data = json.loads(j.read_text())
    assert "grid_quality" not in data
