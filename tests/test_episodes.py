"""Unit tests for on-battery episode detection.

The detector infers "running on battery" episodes from the data PCSS already
logs: line voltage collapsing toward zero, corroborated by the battery
capacity falling inside the same window. It only sees what the 20-minute
DataLog cadence sees — short outages between samples are invisible, and the
docstring says so.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from pcss.stats import detect_on_battery_episodes


def _df(lv, bc=None, start="2026-05-01 00:00", freq="20min"):
    n = len(lv)
    ts = pd.date_range(start, periods=n, freq=freq)
    data = {"ts": ts, "Line Voltage": np.array(lv, dtype=float)}
    if bc is not None:
        data["Battery Capacity"] = np.array(bc, dtype=float)
    return pd.DataFrame(data)


def test_no_low_voltage_no_episodes():
    df = _df([120, 121, 119, 120], bc=[100, 100, 100, 100])
    assert detect_on_battery_episodes(df).empty


def test_corroborated_episode_detected():
    # Voltage collapses for three samples while capacity falls 12 points.
    lv = [120, 120, 0.5, 1.0, 0.8, 120, 120]
    bc = [100, 100, 96, 92, 88, 88, 90]
    eps = detect_on_battery_episodes(_df(lv, bc))
    assert len(eps) == 1
    ep = eps.iloc[0]
    assert ep["start"] == pd.Timestamp("2026-05-01 00:40")
    assert ep["end"] == pd.Timestamp("2026-05-01 01:20")
    # Three consecutive 20-min samples span 40 min plus one cadence interval.
    assert ep["duration_min"] == 60.0
    assert ep["min_voltage"] == 0.5
    assert ep["capacity_drop_pct"] == 12.0   # from 100 before the episode to 88


def test_uncorroborated_low_voltage_is_dropped():
    # A lone 0 V sample with no capacity movement reads as a logging artifact.
    lv = [120, 0.0, 120, 120]
    bc = [100, 100, 100, 100]
    assert detect_on_battery_episodes(_df(lv, bc)).empty


def test_two_separate_episodes():
    lv = [120, 0.5, 120, 120, 120, 1.0, 0.5, 120]
    bc = [100, 95, 95, 100, 100, 97, 93, 93]
    eps = detect_on_battery_episodes(_df(lv, bc))
    assert len(eps) == 2
    assert eps["start"].iloc[1] == pd.Timestamp("2026-05-01 01:40")


def test_threshold_arguments_override():
    lv = [120, 80, 120]
    bc = [100, 90, 100]
    # 80 V is not "near zero" under the default threshold ...
    assert detect_on_battery_episodes(_df(lv, bc)).empty
    # ... but counts when the caller widens it.
    eps = detect_on_battery_episodes(_df(lv, bc), voltage_max=90.0)
    assert len(eps) == 1


def test_missing_capacity_column_falls_back_to_voltage_only():
    # Without a capacity column there is nothing to corroborate against; the
    # detector reports the episode with an unknown drop instead of nothing.
    eps = detect_on_battery_episodes(_df([120, 0.5, 0.5, 120]))
    assert len(eps) == 1
    assert np.isnan(eps["capacity_drop_pct"].iloc[0])


def test_empty_frame():
    assert detect_on_battery_episodes(pd.DataFrame()).empty
