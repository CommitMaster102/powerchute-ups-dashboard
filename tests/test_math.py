"""Unit tests for the analyzer's math (pytest, no browser).

These lock in the calculations audited by hand: tiered/flat cost, runtime
interpolation, energy kWh (incl. per-sample interval), gap/episode detection,
cross-validation, growth rate, and locale number parsing.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pcss import config
from pcss.common import parse_es_number, parse_pcss_number
from pcss.loaders import history_summary
from pcss.stats import (
    compute_energy_summary,
    compute_tiered_cost,
    cross_validate_load,
    datalog_stats,
    detect_gaps,
    detect_high_load_episodes,
    detect_voltage_anomalies,
    estimate_runtime,
)

LOW = config.COOPESANTOS_LOW_RATE
HIGH = config.COOPESANTOS_HIGH_RATE
LIMIT = config.COOPESANTOS_TIER_LIMIT_KWH


# ---------------------------------------------------------------- tiered cost
@pytest.mark.parametrize("kwh,expected", [
    (0, 0.0),
    (-5, 0.0),
    (100, 100 * LOW),
    (LIMIT, LIMIT * LOW),
    (LIMIT + 1, LIMIT * LOW + 1 * HIGH),
    (250, LIMIT * LOW + 50 * HIGH),
])
def test_tiered_cost(kwh, expected):
    assert compute_tiered_cost(kwh) == pytest.approx(expected)


def test_tiered_cost_nan():
    assert compute_tiered_cost(float("nan")) == 0.0


# ---------------------------------------------------------------- runtime curve
def test_runtime_endpoints_and_clamp():
    assert estimate_runtime(0) == pytest.approx(config.RUNTIME_CURVE_MIN[0])   # 60
    assert estimate_runtime(-10) == pytest.approx(config.RUNTIME_CURVE_MIN[0])
    assert estimate_runtime(1200) == pytest.approx(0.0)
    assert estimate_runtime(5000) == pytest.approx(0.0)   # beyond max -> last value


def test_runtime_interpolates_between_knots():
    # Halfway between 100W (40min) and 150W (30min) -> 125W ~ 35min.
    assert estimate_runtime(125) == pytest.approx(35.0)
    assert estimate_runtime(float("nan")) == pytest.approx(config.RUNTIME_CURVE_MIN[0])


# ---------------------------------------------------------------- energy summary
def _energy_df(power_w, interval_sec=300, start="2026-05-01 00:00:00"):
    ts = pd.date_range(start, periods=len(power_w), freq=f"{interval_sec}s")
    return pd.DataFrame({
        "ts": ts,
        "power_w": power_w,
        "load_pct": [p / 14.0 for p in power_w],   # arbitrary
        "interval_sec": [interval_sec] * len(power_w),
    })


def test_energy_summary_known_kwh():
    # 1200 W for two 300 s samples = 2 * (1200 * 300/3600) Wh = 200 Wh = 0.2 kWh
    es = compute_energy_summary(_energy_df([1200, 1200], 300))
    assert es["total_kwh"] == pytest.approx(0.2)
    assert es["total_cost_pcss"] == pytest.approx(0.2 * config.PCSS_FLAT_RATE)
    assert es["total_co2_kg"] == pytest.approx(0.2 * config.CO2_KG_PER_KWH)
    assert es["interval_sec"] == 300


def test_energy_summary_honours_per_sample_interval():
    # Same power but 600 s samples -> double the energy of the 300 s case.
    es = compute_energy_summary(_energy_df([1200, 1200], 600))
    assert es["total_kwh"] == pytest.approx(0.4)


def test_energy_summary_empty():
    assert compute_energy_summary(pd.DataFrame()) == {}


# ---------------------------------------------------------------- gaps
def test_detect_gaps_finds_the_gap():
    ts = [pd.Timestamp("2026-05-01 00:00"),
          pd.Timestamp("2026-05-01 00:20"),    # 20 min — fine
          pd.Timestamp("2026-05-01 02:00")]    # 100 min — gap (>2*20)
    df = pd.DataFrame({"ts": ts})
    gaps = detect_gaps(df, expected_interval_min=20)
    assert len(gaps) == 1
    assert gaps.iloc[0]["duration_min"] == pytest.approx(100.0)


def test_detect_gaps_robust_to_nonzero_index():
    # Index reset away from 0..n-1 must not break positional logic.
    ts = pd.to_datetime(["2026-05-01 00:00", "2026-05-01 03:00"])
    df = pd.DataFrame({"ts": ts}, index=[57, 58])
    assert len(detect_gaps(df, expected_interval_min=20)) == 1


# ---------------------------------------------------------------- high-load
def test_high_load_episode_duration_includes_interval():
    # 3 samples >=80% at 300 s spacing: span = 600 s, +1 interval = 900 s >= 600.
    df = _energy_df([1190, 1190, 1190], 300)   # ~85% of 1400 W
    df["load_pct"] = [85.0, 85.0, 85.0]
    eps = detect_high_load_episodes(df, threshold_pct=80, min_duration_sec=600)
    assert len(eps) == 1
    assert eps.iloc[0]["duration_min"] == pytest.approx(15.0)   # 900 s
    assert eps.iloc[0]["peak_pct"] == pytest.approx(85.0)


def test_high_load_none_when_below_threshold():
    df = _energy_df([100, 100], 300)
    df["load_pct"] = [10.0, 10.0]
    assert detect_high_load_episodes(df, threshold_pct=80).empty


# ---------------------------------------------------------------- cross-validation
def test_cross_validate_matches_near_samples():
    base = pd.Timestamp("2026-05-01 00:00")
    dl = pd.DataFrame({"ts": [base, base + pd.Timedelta(minutes=20)], "UPS Load": [20.0, 30.0]})
    el = pd.DataFrame({"ts": [base + pd.Timedelta(minutes=1),
                              base + pd.Timedelta(minutes=21)], "load_pct": [22.0, 27.0]})
    xv = cross_validate_load(dl, el)
    assert xv["n_pairs"] == 2
    assert xv["mean_abs_error_pct"] == pytest.approx((2 + 3) / 2)


# ---------------------------------------------------------------- voltage anomalies
def test_voltage_anomalies_envelope():
    df = pd.DataFrame({
        "ts": pd.date_range("2026-05-01", periods=4, freq="20min"),
        "Line Voltage": [120.0, 100.0, 130.0, 122.0],   # 100 low, 130 high
    })
    assert len(detect_voltage_anomalies(df)) == 2


# ---------------------------------------------------------------- growth rate
def test_history_summary_rate_scaling():
    hist = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-05-01 00:00:00", "2026-05-02 00:00:00"]),
        "total_bytes": [1000, 1000 + 86400],   # +86400 bytes over exactly 1 day
    })
    hs = history_summary(hist)
    assert hs["bytes_per_second"] == pytest.approx(1.0)
    assert hs["bytes_per_day"] == pytest.approx(86400.0)


def test_history_summary_needs_two_rows():
    one = pd.DataFrame({"timestamp": pd.to_datetime(["2026-05-01"]), "total_bytes": [1]})
    assert history_summary(one) == {}


# ---------------------------------------------------------------- datalog stats
def test_datalog_stats_basic():
    df = pd.DataFrame({"ts": pd.date_range("2026-05-01", periods=3, freq="20min"),
                       "Line Voltage": [120.0, 121.0, 122.0]})
    st = datalog_stats(df, file_size=3000)
    assert st["n_entries"] == 3
    assert st["bytes_per_entry"] == pytest.approx(1000.0)
    assert st["median_interval_sec"] == pytest.approx(1200.0)   # 20 min


# ---------------------------------------------------------------- locale parsing
@pytest.mark.parametrize("raw,expected", [
    ("1.234,56", 1234.56),
    ("122,0", 122.0),
    ("99", 99.0),
    ("N/A", None),
    ("null", None),
    ("", None),
])
def test_parse_es_number(raw, expected):
    out = parse_es_number(raw)
    if expected is None:
        assert np.isnan(out)
    else:
        assert out == pytest.approx(expected)


@pytest.mark.parametrize("raw,expected", [
    ("1234.567", 1234.567),
    ("null", None),
    ("N/A", None),
])
def test_parse_pcss_number(raw, expected):
    out = parse_pcss_number(raw)
    if expected is None:
        assert np.isnan(out)
    else:
        assert out == pytest.approx(expected)
