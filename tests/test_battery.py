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


# ---------------------------------------------------- annotations
def test_default_battery_fields_are_none_without_annotations():
    """With no annotations argument at all, the new keys are present but
    unset — the dict shape gains fields, but the values say "no boundary"."""
    proj = battery_replace_projection(pd.DataFrame())
    assert proj["battery_installed_on"] is None
    assert proj["battery_age_days"] is None


def test_annotations_none_matches_omitted_argument():
    df = _bv_frame(days=90, slope_per_day=-0.01)
    assert battery_replace_projection(df) == battery_replace_projection(df, annotations=None)


def test_empty_annotations_frame_matches_no_annotations():
    df = _bv_frame(days=90, slope_per_day=-0.01)
    empty = pd.DataFrame(columns=["date", "kind", "label"])
    proj_empty = battery_replace_projection(df, annotations=empty)
    proj_none = battery_replace_projection(df)
    assert proj_empty["battery_installed_on"] is None
    assert proj_empty["battery_age_days"] is None
    assert proj_empty["slope_v_per_day"] == pytest.approx(proj_none["slope_v_per_day"])


def test_boundary_segments_fit_to_post_replacement_samples():
    """A battery_replaced annotation must exclude every sample before it from
    the fit. Here the pre-boundary regime rises (an unrelated, irrelevant
    trend) while the post-boundary regime cleanly declines; a fit over the
    whole history nets a positive slope, but the segmented fit must recover
    the clean negative post-boundary slope instead."""
    days_before, days_after, cadence_min = 60, 90, 20
    n_before = int(days_before * 24 * 60 / cadence_min)
    n_after = int(days_after * 24 * 60 / cadence_min)
    start = pd.Timestamp("2026-01-01")
    boundary = start + pd.Timedelta(days=days_before)

    ts_before = pd.date_range(start, periods=n_before, freq=f"{cadence_min}min")
    v_before = 24.0 + 0.05 * (np.arange(n_before) * cadence_min / (24 * 60))

    ts_after = pd.date_range(boundary, periods=n_after, freq=f"{cadence_min}min")
    t_days_after = np.arange(n_after) * cadence_min / (24 * 60)
    v_after = 27.4 - 0.01 * t_days_after

    df = pd.concat([
        pd.DataFrame({"ts": ts_before, "Battery Voltage": v_before}),
        pd.DataFrame({"ts": ts_after, "Battery Voltage": v_after}),
    ], ignore_index=True)
    annotations = pd.DataFrame({
        "date": [boundary.date()], "kind": ["battery_replaced"], "label": ["new battery"],
    })

    proj = battery_replace_projection(df, annotations=annotations)
    assert proj["status"] == "projected"
    assert proj["slope_v_per_day"] == pytest.approx(-0.01, rel=0.15)
    assert proj["battery_installed_on"] == boundary.date()
    expected_age = (df["ts"].iloc[-1] - boundary).total_seconds() / 86400
    assert proj["battery_age_days"] == pytest.approx(expected_age, rel=0.02)

    # The whole, unsegmented history nets an overall rise (60 days up at
    # 0.05 V/day outweighs 90 days down at 0.01 V/day); the segmented fit
    # above must not be contaminated by it.
    proj_unsegmented = battery_replace_projection(df)
    assert proj_unsegmented["slope_v_per_day"] > 0
    assert proj["slope_v_per_day"] < 0
    assert proj_unsegmented["battery_installed_on"] is None


def test_future_dated_annotation_leaves_fit_unsegmented():
    """A battery_replaced date after the newest sample marks no boundary yet
    (a planned replacement, not one that happened) — the fit must behave
    exactly as if no annotation existed at all."""
    df = _bv_frame(days=90, slope_per_day=-0.01)
    future_date = (df["ts"].iloc[-1] + pd.Timedelta(days=30)).date()
    annotations = pd.DataFrame({
        "date": [future_date], "kind": ["battery_replaced"], "label": ["planned"],
    })
    proj_with_future = battery_replace_projection(df, annotations=annotations)
    proj_without = battery_replace_projection(df)
    assert proj_with_future["battery_installed_on"] is None
    assert proj_with_future["battery_age_days"] is None
    assert proj_with_future["slope_v_per_day"] == pytest.approx(proj_without["slope_v_per_day"])
    assert proj_with_future["status"] == proj_without["status"]


def test_non_battery_replaced_annotation_does_not_segment():
    df = _bv_frame(days=90, slope_per_day=-0.01)
    mid_date = (df["ts"].iloc[0] + pd.Timedelta(days=30)).date()
    annotations = pd.DataFrame({
        "date": [mid_date], "kind": ["ups_moved"], "label": ["moved desks"],
    })
    proj_with = battery_replace_projection(df, annotations=annotations)
    proj_without = battery_replace_projection(df)
    assert proj_with["battery_installed_on"] is None
    assert proj_with["slope_v_per_day"] == pytest.approx(proj_without["slope_v_per_day"])
