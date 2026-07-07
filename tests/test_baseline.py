"""Unit tests for baseline-deviation energy alerts (roadmap item 19).

The weekday and weekend hourly profiles that the Weekday vs Weekend
dashboard card already draws (`pcss.dashboard._panel_wk`) define what a
normal day looks like. `weekday_weekend_profiles` in `pcss/stats.py` is the
one place that math is computed, shared by the `wk` panel and by
`detect_baseline_deviations`, so the two views of "normal" cannot drift
apart. The detector compares each complete day's own hourly profile against
whichever baseline (weekday or weekend) matches its type, and flags the day
when the mean absolute deviation across the 24 hours — expressed as a
percent of the baseline's own mean power — exceeds `[thresholds]
baseline_deviation_pct`. Below `baseline_min_days` distinct days of
energylog history, the honest result is "insufficient_history" with
nothing flagged. The flag surfaces three ways: a marker on the Daily Energy
bars (`pcss.dashboard._panel_daily`), a console line, and the `[alerts]`
trigger (`analyze_ups._maybe_write_alerts`) — all wording says "deviates
from the recorded baseline", never a fault claim.
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd
import pytest

from pcss import config
from pcss.dashboard import _panel_daily, build_dashboard
from pcss.stats import compute_energy_summary, detect_baseline_deviations, weekday_weekend_profiles


def _edf_for_dates(dates, hourly_watts) -> pd.DataFrame:
    """An energylog-shaped frame (ts, power_w) at hourly cadence, one full
    day (24 hourly samples) per entry in `dates`. `hourly_watts(day_index,
    hour) -> watts` supplies each sample."""
    rows = []
    for i, d in enumerate(dates):
        base = pd.Timestamp(d)
        for h in range(24):
            rows.append((base + pd.Timedelta(hours=h), float(hourly_watts(i, h))))
    df = pd.DataFrame(rows, columns=["ts", "power_w"])
    df["interval_sec"] = 3600
    return df


def _edf(start: str, days: int, hourly_watts) -> pd.DataFrame:
    """An energylog-shaped frame (ts, power_w) at hourly cadence spanning
    `days` full consecutive calendar days from `start`. `hourly_watts` is
    either a fixed 24-length sequence (repeated every day) or a callable
    `(day_index, hour) -> watts` for per-day variation."""
    dates = pd.date_range(start, periods=days)
    fn = hourly_watts if callable(hourly_watts) else (lambda d, h: hourly_watts[h])
    return _edf_for_dates(dates, fn)


# ---------------------------------------------------------------- shared profile helper
def test_weekday_weekend_profiles_splits_by_day_type():
    """2026-01-05 is a Monday: five weekdays then a weekend day, each with a
    distinct flat wattage, so the two profiles must come back with disjoint,
    correctly-averaged values."""
    def watts(d, h):
        return 300.0 if d < 5 else 100.0

    df = _edf("2026-01-05", 7, watts)   # Mon..Sun
    profiles = weekday_weekend_profiles(df)
    assert set(profiles) == {"weekday", "weekend"}
    assert profiles["weekday"].loc[12] == pytest.approx(300.0)
    assert profiles["weekend"].loc[12] == pytest.approx(100.0)
    assert list(profiles["weekday"].index) == list(range(24))
    assert list(profiles["weekend"].index) == list(range(24))


def test_weekday_weekend_profiles_missing_weekend_days():
    """A history with no weekend days yet omits that key entirely rather
    than returning an empty/garbage series."""
    df = _edf("2026-01-05", 5, lambda d, h: 200.0)   # Mon-Fri only
    profiles = weekday_weekend_profiles(df)
    assert set(profiles) == {"weekday"}


def test_weekday_weekend_profiles_empty_input():
    assert weekday_weekend_profiles(pd.DataFrame()) == {}
    assert weekday_weekend_profiles(pd.DataFrame(columns=["ts"])) == {}


def test_weekday_weekend_profiles_all_nan_power_is_empty():
    df = _edf("2026-01-05", 3, lambda d, h: np.nan)
    assert weekday_weekend_profiles(df) == {}


# ---------------------------------------------------------------- config defaults
def test_config_defaults():
    assert config.BASELINE_MIN_DAYS == 14
    assert pytest.approx(35.0) == config.BASELINE_DEVIATION_PCT


def test_load_config_overrides_baseline_thresholds(tmp_path):
    saved = (config.BASELINE_MIN_DAYS, config.BASELINE_DEVIATION_PCT)
    try:
        conf = tmp_path / "config.toml"
        conf.write_text(
            "[thresholds]\nbaseline_min_days = 20\nbaseline_deviation_pct = 50.0\n",
            encoding="utf-8")
        config.load_config(conf)
        assert config.BASELINE_MIN_DAYS == 20
        assert pytest.approx(50.0) == config.BASELINE_DEVIATION_PCT
    finally:
        config.BASELINE_MIN_DAYS, config.BASELINE_DEVIATION_PCT = saved


# ---------------------------------------------------------------- detector: core behavior
def test_stuck_on_appliance_day_is_flagged():
    """A flat, unusually high load all day (day index 10 of 21 weekdays, not
    the trailing day) reads as a stuck-on appliance and is flagged against
    the two-level (night/day) weekday baseline."""
    dates = list(pd.bdate_range("2026-01-05", periods=21))
    stuck_idx = 10

    def watts(d, h):
        if d == stuck_idx:
            return 700.0
        return 100.0 if h < 12 else 300.0

    df = _edf_for_dates(dates, watts)
    result = detect_baseline_deviations(df)
    assert result["status"] == "ok"
    flagged = result["flagged"]
    stuck_date = dates[stuck_idx].date()
    assert stuck_date in set(flagged["date"])
    row = flagged[flagged["date"] == stuck_date].iloc[0]
    assert row["day_type"] == "weekday"
    assert row["deviation_pct"] > config.BASELINE_DEVIATION_PCT


def test_normal_day_is_not_flagged():
    """A day matching the shape every other day already has is not flagged,
    even with the stuck-on-appliance day (above) diluting the baseline."""
    dates = list(pd.bdate_range("2026-01-05", periods=21))
    stuck_idx = 10

    def watts(d, h):
        if d == stuck_idx:
            return 700.0
        return 100.0 if h < 12 else 300.0

    df = _edf_for_dates(dates, watts)
    result = detect_baseline_deviations(df)
    normal_date = dates[5].date()
    assert normal_date not in set(result["flagged"]["date"])


def test_weekend_day_compared_against_weekend_profile_not_weekday():
    """Weekdays run flat at 300 W, weekends flat at 100 W, consistently
    across three weeks. A weekend day (matching its own weekend baseline
    exactly) must not be flagged — if the detector wrongly compared it
    against the 300 W weekday baseline instead, the ~67% gap would trip the
    default 35% threshold and this assertion would fail."""
    dates = list(pd.date_range("2026-01-05", periods=21))  # 3 full weeks

    def watts(d, h):
        return 100.0 if dates[d].dayofweek >= 5 else 300.0

    df = _edf_for_dates(dates, watts)
    result = detect_baseline_deviations(df)
    assert result["status"] == "ok"
    # A weekend day that is not the trailing (excluded) date.
    weekend_idx = next(i for i, dt in enumerate(dates[:-1]) if dt.dayofweek >= 5)
    assert dates[weekend_idx].date() not in set(result["flagged"]["date"])


def test_trailing_partial_day_is_excluded():
    """The most recent calendar date in the frame is always excluded, even
    when its (incomplete) shape would obviously deviate if it were treated
    as a complete day."""
    dates = list(pd.bdate_range("2026-01-05", periods=20))

    def watts(d, h):
        return 100.0 if h < 12 else 300.0

    df = _edf_for_dates(dates, watts)
    trailing = dates[-1] + pd.Timedelta(days=1)
    while trailing.dayofweek >= 5:
        trailing += pd.Timedelta(days=1)
    partial = pd.DataFrame({
        "ts": [trailing, trailing + pd.Timedelta(hours=1)],
        "power_w": [9000.0, 9000.0],
        "interval_sec": [3600, 3600],
    })
    full = pd.concat([df, partial], ignore_index=True)
    result = detect_baseline_deviations(full)
    assert result["status"] == "ok"
    assert trailing.date() not in set(result["flagged"]["date"])


# ---------------------------------------------------------------- completeness filter
def test_gap_day_below_90pct_of_peers_is_never_flagged():
    """A mid-history day whose sampling gap leaves it well under 90% of its
    peers' median sample count is excluded from flagging entirely, even when
    the hours it did record deviate wildly. Without the completeness filter
    this day would flag at several hundred percent deviation (900 W recorded
    against a ~100 W night baseline), so an empty flagged frame pins the
    filter itself, not a small deviation."""
    dates = list(pd.bdate_range("2026-01-05", periods=21))
    gap_idx = 10

    rows = []
    for i, d in enumerate(dates):
        base = pd.Timestamp(d)
        for h in range(24):
            if i == gap_idx and h >= 8:
                continue    # a 16-hour sampling gap: 8 of 24 samples remain
            watts = 900.0 if i == gap_idx else (100.0 if h < 12 else 300.0)
            rows.append((base + pd.Timedelta(hours=h), watts))
    df = pd.DataFrame(rows, columns=["ts", "power_w"])
    df["interval_sec"] = 3600

    result = detect_baseline_deviations(df)
    assert result["status"] == "ok"
    assert dates[gap_idx].date() not in set(result["flagged"]["date"])
    assert result["flagged"].empty


def test_uniformly_coarser_day_is_currently_excluded():
    """A day recorded at a uniformly coarser cadence — for example PCSS
    reconfigured from hourly to two-hourly sampling for one day, halving its
    sample count — falls under the 90%-of-peers completeness bar and is
    excluded from flagging, even though its coverage of the day is complete
    and its recorded behavior (flat 900 W) deviates wildly.

    This pins the documented trade-off: the completeness filter counts
    samples, so it cannot tell a mid-day sampling gap apart from a
    deliberate interval reconfiguration, and it errs on the side of staying
    silent about such a day rather than flagging on partial evidence. If the
    detector is ever taught to weigh coverage (hours spanned) instead of raw
    sample count, this test is the one to update."""
    dates = list(pd.bdate_range("2026-01-05", periods=21))
    coarse_idx = 10

    rows = []
    for i, d in enumerate(dates):
        base = pd.Timestamp(d)
        for h in range(24):
            if i == coarse_idx and h % 2:
                continue    # every other hour: 12 of 24 samples, full-day span
            watts = 900.0 if i == coarse_idx else (100.0 if h < 12 else 300.0)
            rows.append((base + pd.Timedelta(hours=h), watts))
    df = pd.DataFrame(rows, columns=["ts", "power_w"])
    df["interval_sec"] = 3600

    result = detect_baseline_deviations(df)
    assert result["status"] == "ok"
    # Current behavior: 12 samples < 0.9 * 24-sample peer median, so the day
    # is silently excluded and nothing is flagged.
    assert dates[coarse_idx].date() not in set(result["flagged"]["date"])
    assert result["flagged"].empty


def test_empty_energy_df_is_insufficient_history():
    result = detect_baseline_deviations(pd.DataFrame())
    assert result["status"] == "insufficient_history"
    assert result["flagged"].empty
    assert result["n_days"] == 0
    assert list(result["flagged"].columns) == ["date", "day_type", "deviation_pct"]


# ---------------------------------------------------------------- floor
def test_below_floor_is_honest():
    dates = list(pd.bdate_range("2026-01-05", periods=10))   # < default 14
    df = _edf_for_dates(dates, lambda d, h: 200.0)
    result = detect_baseline_deviations(df)
    assert result["status"] == "insufficient_history"
    assert result["flagged"].empty
    assert result["n_days"] == 10
    assert result["min_days"] == pytest.approx(config.BASELINE_MIN_DAYS)


def test_min_days_argument_overrides_config():
    dates = list(pd.bdate_range("2026-01-05", periods=10))
    df = _edf_for_dates(dates, lambda d, h: 200.0)
    assert detect_baseline_deviations(df)["status"] == "insufficient_history"
    assert detect_baseline_deviations(df, min_days=5)["status"] == "ok"


# ---------------------------------------------------------------- threshold knob
def test_deviation_pct_threshold_respected():
    """One day at 286 W against its pure-peer 200 W baseline — leave-one-out
    excludes the evaluated day from its own comparison (polish wave B, item
    B8), so there is no self-dilution — deviates by (286 - 200) / 200 = 43%:
    past the default 35% floor, short of a stricter 45% one."""
    dates = list(pd.bdate_range("2026-01-05", periods=19))
    test_idx = 9

    def watts(d, h):
        return 286.0 if d == test_idx else 200.0

    df = _edf_for_dates(dates, watts)
    test_date = dates[test_idx].date()

    default_result = detect_baseline_deviations(df)
    assert test_date in set(default_result["flagged"]["date"])

    strict_result = detect_baseline_deviations(df, deviation_pct=45.0)
    assert test_date not in set(strict_result["flagged"]["date"])


# ---------------------------------------------------------------- leave-one-out (item B8)
def test_leave_one_out_flags_day_that_self_diluted_baseline_misses():
    """Leave-one-out baselines (polish wave B, item B8): a day is compared
    against a baseline built from its PEERS only, not one its own samples
    diluted. This borderline weekend day is missed by the old self-included
    baseline but caught once the day is excluded from its own comparison.

    Data: 21 consecutive days from Mon 2026-01-05 (three full weeks). Every
    weekday sits flat at 300 W; every weekend day flat at 100 W except the
    test day, Sat 2026-01-17, at 140 W. Six weekend days fall in the window
    (Jan 10, 11, 17, 18, 24, 25); Jan 25 is the trailing (always-excluded)
    day, leaving five weekend candidates.

    Self-included weekend baseline (the OLD behavior): per hour
    (5*100 + 140) / 6 = 106.67 W, so Jan 17 deviates by
    |140 - 106.67| / 106.67 = 31.25% — under the 35% default, NOT flagged.

    Leave-one-out weekend baseline for Jan 17 (its five weekend peers, all at
    100 W): 100 W, so it deviates by |140 - 100| / 100 = 40% — over 35%,
    flagged. No other day crosses: the other weekend days at 100 W deviate at
    most |100 - 108| / 108 = 7.4% against their own leave-one-out baseline,
    and every weekday sits exactly on its flat 300 W peer baseline.
    """
    dates = list(pd.date_range("2026-01-05", periods=21))
    test_date = pd.Timestamp("2026-01-17").date()

    def watts(d, h):
        day = dates[d]
        if day.dayofweek < 5:
            return 300.0
        return 140.0 if day.date() == test_date else 100.0

    df = _edf_for_dates(dates, watts)
    result = detect_baseline_deviations(df)
    assert result["status"] == "ok"
    flagged_dates = set(result["flagged"]["date"])
    assert test_date in flagged_dates
    assert flagged_dates == {test_date}, "leave-one-out should flag only the borderline day"
    row = result["flagged"][result["flagged"]["date"] == test_date].iloc[0]
    assert row["day_type"] == "weekend"
    assert row["deviation_pct"] == pytest.approx(40.0, abs=0.5)


# ---------------------------------------------------------------- payload markers (_panel_daily)
def test_panel_daily_flags_deviating_day_with_marker_and_color():
    dates = list(pd.bdate_range("2026-01-05", periods=21))
    stuck_idx = 10

    def watts(d, h):
        if d == stuck_idx:
            return 700.0
        return 100.0 if h < 12 else 300.0

    edf = _edf_for_dates(dates, watts)
    es = compute_energy_summary(edf)
    baseline = detect_baseline_deviations(edf)
    panel = _panel_daily(es, baseline["flagged"])

    stuck_label = f"{dates[stuck_idx].month}/{dates[stuck_idx].day}"
    idx = next(i for i, item in enumerate(panel["data"]) if item["label"] == stuck_label)
    assert panel["data"][idx]["color"] == "amber"
    pct = baseline["flagged"].iloc[0]["deviation_pct"]
    # The marker carries the y/type/color fields renderBar needs to draw the
    # glyph above the flagged bar, mirroring the lv/bc marker-list shape.
    # deviation_pct is an unsigned mean-absolute deviation, so the label is
    # bare (no leading "+", matching the console) — a below-baseline day would
    # otherwise read "+38%".
    assert panel["markers"] == [{"x": idx, "y": panel["data"][idx]["y"], "type": "dot",
                                 "color": "amber", "label": f"{pct:.0f}%"}]
    # Every other bar stays uncolored (the default panel color applies).
    assert all("color" not in item for i, item in enumerate(panel["data"]) if i != idx)


def test_panel_daily_no_flagged_days_is_no_markers():
    edf = _edf("2026-01-05", 5, lambda d, h: 200.0)
    es = compute_energy_summary(edf)
    panel = _panel_daily(es, None)
    assert panel["markers"] == []
    assert all("color" not in item for item in panel["data"])
    empty_flagged = pd.DataFrame(columns=["date", "day_type", "deviation_pct"])
    panel2 = _panel_daily(es, empty_flagged)
    assert panel2["markers"] == []


def test_panel_daily_none_on_empty_energy_summary():
    assert _panel_daily({}, None) is None


# ---------------------------------------------------------------- payload markers (build_dashboard)
def _dashboard_inputs(energy_df, energy_summary):
    return dict(
        datalog_df=pd.DataFrame(), energy_df=energy_df, hist=pd.DataFrame(),
        dl_stats={}, hist_stats={},
        sizes={"DataLog": 0, "EventLog (binary)": 0, "energylog/": 0},
        energy_summary=energy_summary, stats_table=pd.DataFrame(), gaps=pd.DataFrame(),
        voltage_anomalies=pd.DataFrame(), high_load_episodes=pd.DataFrame(), crossval={},
    )


def _payload(html: str) -> dict:
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    return json.loads(m.group(1).replace("<\\/", "</"))


def _stuck_appliance_energy(periods=21, stuck_idx=10):
    dates = list(pd.bdate_range("2026-01-05", periods=periods))

    def watts(d, h):
        if d == stuck_idx:
            return 700.0
        return 100.0 if h < 12 else 300.0

    return _edf_for_dates(dates, watts)


def test_build_dashboard_daily_panel_carries_markers():
    edf = _stuck_appliance_energy()
    es = compute_energy_summary(edf)
    html = build_dashboard(**_dashboard_inputs(edf, es))
    daily = _payload(html)["panels"]["daily"]
    assert len(daily["markers"]) == 1


def test_build_dashboard_daily_sub_mentions_deviating_day_count():
    edf = _stuck_appliance_energy()
    es = compute_energy_summary(edf)
    html = build_dashboard(**_dashboard_inputs(edf, es))
    assert "1 day deviates from the recorded baseline" in html


def test_build_dashboard_daily_sub_silent_when_nothing_flagged():
    edf = _edf("2026-01-05", 20, lambda d, h: 200.0)
    es = compute_energy_summary(edf)
    html = build_dashboard(**_dashboard_inputs(edf, es))
    assert "deviates from the recorded baseline" not in html
    assert "deviate from the recorded baseline" not in html


def test_build_dashboard_daily_sub_spanish(monkeypatch):
    import pcss.config as cfg
    monkeypatch.setattr(cfg, "DASHBOARD_LANGUAGE", "es")
    edf = _stuck_appliance_energy()
    es = compute_energy_summary(edf)
    html = build_dashboard(**_dashboard_inputs(edf, es))
    assert "se desvía de la referencia registrada" in html


def test_build_dashboard_explicit_baseline_argument_used_over_default():
    """When ``baseline`` is passed explicitly, build_dashboard must not
    recompute it from energy_df — a caller's own precomputed result wins
    (the same contract ``self_tests``/``battery``/``forecast`` already
    honor)."""
    edf = _edf("2026-01-05", 20, lambda d, h: 200.0)   # nothing would flag on its own
    es = compute_energy_summary(edf)
    fake_date = edf["ts"].iloc[0].date()
    fake_baseline = {
        "status": "ok", "min_days": 14, "n_days": 20, "deviation_pct": 35.0,
        "flagged": pd.DataFrame([{"date": fake_date, "day_type": "weekday",
                                  "deviation_pct": 99.0}]),
    }
    html = build_dashboard(**_dashboard_inputs(edf, es), baseline=fake_baseline)
    assert "1 day deviates from the recorded baseline" in html


# ---------------------------------------------------------------- console (analyze_ups.py integration)
def _write_baseline_agent(agent_dir, periods=21, stuck_idx=10, start="2026-01-05"):
    """An energylog-only agent directory (no DataLog needed for this
    console check) spanning `periods` weekdays at hourly cadence, with a
    stuck-on-appliance day at `stuck_idx` (or none, when `stuck_idx` is
    None)."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "energylog").mkdir(exist_ok=True)
    dates = list(pd.bdate_range(start, periods=periods))
    lines = [f"# $month={dates[0]:%Y-%m}", "# $interval=3600", "# $calculatedMaxLoad=1400.0"]
    epoch_2010 = pd.Timestamp("2010-01-01")
    for d, dt in enumerate(dates):
        for h in range(24):
            t = dt + pd.Timedelta(hours=h)
            watts = 700.0 if d == stuck_idx else (100.0 if h < 12 else 300.0)
            secs = (t - epoch_2010).total_seconds()
            lines.append(f"{secs:.0f};null;{watts / 1400 * 100:.4f};{watts:.1f}")
    (agent_dir / "energylog" / "combined.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return agent_dir


def _hermetic_config(tmp_path):
    conf = tmp_path / "config.toml"
    conf.write_text("[archive]\nenabled = false\n", encoding="utf-8")
    return conf


def test_console_reports_flagged_day_text(tmp_path, capsys):
    import analyze_ups
    agent = _write_baseline_agent(tmp_path / "agent")
    exit_code = analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                                  "--no-browser", "--no-snapshot",
                                  "--config", str(_hermetic_config(tmp_path))])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "1 day deviates from the recorded baseline" in out
    # The flagged day's own line names the deviation percent, and nowhere
    # does the wording overclaim a fault.
    assert "% deviation" in out
    assert "fault" not in out.lower()


def test_console_reports_insufficient_history(tmp_path, capsys):
    import analyze_ups
    agent = _write_baseline_agent(tmp_path / "agent", periods=5, stuck_idx=None)
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path))])
    out = capsys.readouterr().out
    assert "not enough history" in out.lower()
    assert "5" in out


# ---------------------------------------------------------------- alerts trigger
EMPTY_BASELINE = {"status": "ok", "min_days": 14, "n_days": 20, "deviation_pct": 35.0,
                 "flagged": pd.DataFrame(columns=["date", "day_type", "deviation_pct"])}


def _flagged_baseline(n=1):
    rows = [{"date": pd.Timestamp("2026-01-01").date() + pd.Timedelta(days=i),
             "day_type": "weekday", "deviation_pct": 50.0} for i in range(n)]
    return {"status": "ok", "min_days": 14, "n_days": 20, "deviation_pct": 35.0,
            "flagged": pd.DataFrame(rows)}


def test_maybe_write_alerts_fires_on_baseline_deviation_alone(tmp_path, monkeypatch):
    import analyze_ups
    import pcss.config as cfg
    monkeypatch.setattr(cfg, "ALERTS_ENABLED", True)
    monkeypatch.setattr(cfg, "ALERTS_LOG", tmp_path / "alerts.log")
    empty = pd.DataFrame()
    path = analyze_ups._maybe_write_alerts(empty, empty, empty, None, _flagged_baseline(2))
    assert path == cfg.ALERTS_LOG
    text = path.read_text(encoding="utf-8")
    assert "baseline_deviations=2" in text


def test_maybe_write_alerts_silent_when_baseline_has_nothing_flagged(tmp_path, monkeypatch):
    import analyze_ups
    import pcss.config as cfg
    monkeypatch.setattr(cfg, "ALERTS_ENABLED", True)
    monkeypatch.setattr(cfg, "ALERTS_LOG", tmp_path / "alerts.log")
    empty = pd.DataFrame()
    path = analyze_ups._maybe_write_alerts(empty, empty, empty, None, EMPTY_BASELINE)
    assert path is None
    assert not (tmp_path / "alerts.log").exists()


def test_maybe_write_alerts_baseline_defaults_to_none(tmp_path, monkeypatch):
    """Existing callers that don't pass `baseline` at all keep working
    exactly as before this feature existed."""
    import analyze_ups
    import pcss.config as cfg
    monkeypatch.setattr(cfg, "ALERTS_ENABLED", True)
    monkeypatch.setattr(cfg, "ALERTS_LOG", tmp_path / "alerts.log")
    empty = pd.DataFrame()
    path = analyze_ups._maybe_write_alerts(empty, empty, empty, None)
    assert path is None
