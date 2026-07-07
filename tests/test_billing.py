"""Unit tests for billing-cycle alignment.

Coopesantos bills on a cycle that does not necessarily start on the first of
the month. With [tariff] billing_cycle_start_day set, the energy summary
groups by billing period instead of calendar month, the tier limit applies
per period, and partial periods at the ends of the recorded span are labeled
as such instead of presenting a misleading tier split.
"""
from __future__ import annotations

import pandas as pd
import pytest

from pcss import config
from pcss.stats import compute_energy_summary


def _edf(rows):
    """(ts, power_w) tuples -> energylog-shaped frame at 300 s intervals."""
    df = pd.DataFrame(rows, columns=["ts", "power_w"])
    df["ts"] = pd.to_datetime(df["ts"])
    df["interval_sec"] = 300
    return df.sort_values("ts").reset_index(drop=True)


def test_default_start_day_reproduces_calendar_months():
    es = compute_energy_summary(_edf([
        ("2026-05-14 12:00", 1200.0),
        ("2026-05-15 12:00", 1200.0),
    ]))
    assert list(es["monthly"]["month"]) == ["2026-05"]


def test_start_day_splits_mid_month(monkeypatch):
    monkeypatch.setattr(config, "BILLING_CYCLE_START_DAY", 15)
    es = compute_energy_summary(_edf([
        ("2026-05-14 12:00", 1200.0),   # belongs to the 2026-04-15 period
        ("2026-05-15 12:00", 1200.0),   # starts the 2026-05-15 period
    ]))
    assert list(es["monthly"]["month"]) == ["2026-04-15", "2026-05-15"]


def test_tier_limit_applies_per_billing_period(monkeypatch):
    monkeypatch.setattr(config, "BILLING_CYCLE_START_DAY", 15)
    monkeypatch.setattr(config, "COOPESANTOS_TIER_LIMIT_KWH", 0.05)
    # Two samples of 0.1 kWh each land in different billing periods, so each
    # period pays 0.05 kWh low + 0.05 kWh high. Grouped as one period the
    # split would be 0.05 low + 0.15 high — a different total.
    es = compute_energy_summary(_edf([
        ("2026-05-14 12:00", 1200.0),
        ("2026-05-15 12:00", 1200.0),
    ]))
    low, high = config.COOPESANTOS_LOW_RATE, config.COOPESANTOS_HIGH_RATE
    expected = 2 * (0.05 * low + 0.05 * high)
    assert es["total_cost_tiered"] == pytest.approx(expected)


def test_partial_periods_are_labeled(monkeypatch):
    monkeypatch.setattr(config, "BILLING_CYCLE_START_DAY", 15)
    # Data from May 20 to July 20: the 2026-05-15 period starts before the
    # data does and the 2026-07-15 period ends after it — both partial; the
    # 2026-06-15 period is fully covered.
    rows = [(ts, 300.0) for ts in pd.date_range("2026-05-20", "2026-07-20", freq="6h")]
    es = compute_energy_summary(_edf(rows))
    monthly = es["monthly"].set_index("month")
    assert bool(monthly.loc["2026-05-15", "partial"]) is True
    assert bool(monthly.loc["2026-06-15", "partial"]) is False
    assert bool(monthly.loc["2026-07-15", "partial"]) is True


def test_start_day_clamps_to_short_months(monkeypatch):
    monkeypatch.setattr(config, "BILLING_CYCLE_START_DAY", 31)
    es = compute_energy_summary(_edf([
        ("2026-02-20 12:00", 1200.0),   # Feb has 28 days -> period began Jan 31
        ("2026-03-01 12:00", 1200.0),   # before Mar 31 -> period began Feb 28
    ]))
    assert list(es["monthly"]["month"]) == ["2026-01-31", "2026-02-28"]


def test_calendar_months_have_partial_column_too():
    es = compute_energy_summary(_edf([("2026-05-14 12:00", 1200.0)]))
    assert "partial" in es["monthly"].columns
