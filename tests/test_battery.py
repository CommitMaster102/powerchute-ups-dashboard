"""Unit tests for the battery replace-by projection.

The Battery Voltage card already fits a degradation trend; the projection
extends it to "at the current slope, when does the resting voltage cross the
replace threshold?". A slope over a few weeks is dominated by noise, so the
projection only speaks once the history clears a confidence floor, and the
fit runs on a rolling median so capacity self-test dips do not bias it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from pcss.stats import battery_replace_projection


def _bv_frame(days, start_v=27.4, slope_per_day=0.0, dips=False, cadence_min=20):
    n = int(days * 24 * 60 / cadence_min)
    ts = pd.date_range("2026-01-01", periods=n, freq=f"{cadence_min}min")
    t_days = np.arange(n) * cadence_min / (24 * 60)
    v = start_v + slope_per_day * t_days
    if dips:
        # A capacity self-test roughly every three days: six consecutive
        # samples sag by 2 V (the sawtooth the robust fit must ignore).
        period = int(3 * 24 * 60 / cadence_min)
        for s in range(0, n, period):
            v[s:s + 6] -= 2.0
    return pd.DataFrame({"ts": ts, "Battery Voltage": v})


def test_short_history_is_insufficient():
    proj = battery_replace_projection(_bv_frame(days=10, slope_per_day=-0.01))
    assert proj["status"] == "insufficient_history"
    assert proj["replace_date"] is None


def test_declining_battery_projects_a_date():
    df = _bv_frame(days=90, slope_per_day=-0.01)
    proj = battery_replace_projection(df)
    assert proj["status"] == "projected"
    assert proj["slope_v_per_day"] == pytest.approx(-0.01, rel=0.15)
    # Start 27.4, ends near 26.5; at -0.01 V/day the 25.6 V threshold is
    # roughly 90 days out from the end of the data.
    assert proj["days_to_replace"] == pytest.approx(90, abs=15)
    expected = df["ts"].iloc[-1] + pd.Timedelta(days=90)
    assert abs((proj["replace_date"] - expected).days) <= 15


def test_flat_battery_is_stable():
    proj = battery_replace_projection(_bv_frame(days=90, slope_per_day=0.0))
    assert proj["status"] == "stable"
    assert proj["replace_date"] is None


def test_self_test_dips_do_not_bias_the_fit():
    clean = battery_replace_projection(_bv_frame(days=90, slope_per_day=-0.01))
    dipped = battery_replace_projection(_bv_frame(days=90, slope_per_day=-0.01, dips=True))
    assert dipped["status"] == "projected"
    assert dipped["slope_v_per_day"] == pytest.approx(clean["slope_v_per_day"], rel=0.2)


def test_already_below_threshold():
    proj = battery_replace_projection(_bv_frame(days=90, start_v=25.0, slope_per_day=-0.01))
    assert proj["status"] == "projected"
    assert proj["days_to_replace"] == 0.0


def test_threshold_and_floor_arguments_override():
    df = _bv_frame(days=10, slope_per_day=-0.01)
    proj = battery_replace_projection(df, min_days=5.0)
    assert proj["status"] == "projected"
    proj2 = battery_replace_projection(_bv_frame(days=90, slope_per_day=-0.01),
                                       threshold_v=10.0)
    # A threshold far below any plausible voltage pushes the date far out.
    assert proj2["days_to_replace"] > 1000


def test_empty_or_missing_column():
    assert battery_replace_projection(pd.DataFrame())["status"] == "insufficient_history"
    df = pd.DataFrame({"ts": pd.to_datetime(["2026-01-01"]), "UPS Load": [10.0]})
    assert battery_replace_projection(df)["status"] == "insufficient_history"
