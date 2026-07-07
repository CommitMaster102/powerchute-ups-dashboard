"""Unit tests for billing-cycle alignment.

Coopesantos bills on a cycle that does not necessarily start on the first of
the month. With [tariff] billing_cycle_start_day set, the energy summary
groups by billing period instead of calendar month, the tier limit applies
per period, and partial periods at the ends of the recorded span are labeled
as such instead of presenting a misleading tier split.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from pcss import config
from pcss.config import TariffPeriod
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


# ------------------------------------------------------- tariff history
def test_no_tariff_history_matches_pre_feature_behavior(monkeypatch):
    """With TARIFF_HISTORY empty (the default), every period is still priced
    with the flat [tariff] keys, exactly as before this feature existed —
    no rate_tag column, no per-period rate lookup."""
    monkeypatch.setattr(config, "TARIFF_HISTORY", [])
    es = compute_energy_summary(_edf([
        ("2026-05-14 12:00", 1200.0),
        ("2026-05-15 12:00", 1200.0),
    ]))
    kwh = float(es["monthly"]["kwh"].iloc[0])
    assert es["monthly"]["cost_pcss"].iloc[0] == pytest.approx(kwh * config.PCSS_FLAT_RATE)
    assert es["total_cost_pcss"] == pytest.approx(es["total_kwh"] * config.PCSS_FLAT_RATE)
    assert es["tariff_history_active"] is False
    assert "rate_tag" not in es["monthly"].columns


def test_history_prices_each_period_with_its_own_rates(monkeypatch):
    """A rate boundary between two billing periods prices each period with
    the rates that were in force on its own start date, not today's rates."""
    monkeypatch.setattr(config, "TARIFF_HISTORY", [
        TariffPeriod(date(2026, 1, 1), coopesantos_low=70.0, coopesantos_high=110.0,
                     tier_limit_kwh=200.0, pcss_flat=110.0),
        TariffPeriod(date(2026, 6, 1), coopesantos_low=80.0, coopesantos_high=130.0,
                     tier_limit_kwh=200.0, pcss_flat=130.0),
    ])
    es = compute_energy_summary(_edf([
        ("2026-05-15 12:00", 1200.0),   # May period: priced with the Jan 1 entry
        ("2026-06-15 12:00", 1200.0),   # June period: priced with the Jun 1 entry
    ]))
    monthly = es["monthly"].set_index("month")
    may_kwh = float(monthly.loc["2026-05", "kwh"])
    jun_kwh = float(monthly.loc["2026-06", "kwh"])
    assert monthly.loc["2026-05", "cost_pcss"] == pytest.approx(may_kwh * 110.0)
    assert monthly.loc["2026-06", "cost_pcss"] == pytest.approx(jun_kwh * 130.0)
    assert monthly.loc["2026-05", "cost_tiered"] == pytest.approx(may_kwh * 70.0)
    assert monthly.loc["2026-06", "cost_tiered"] == pytest.approx(jun_kwh * 80.0)
    assert monthly.loc["2026-05", "rate_tag"] == "rates from 2026-01-01"
    assert monthly.loc["2026-06", "rate_tag"] == "rates from 2026-06-01"
    assert es["tariff_history_active"] is True


def test_tier_and_rate_change_straddle_period_boundary(monkeypatch):
    """A boundary where BOTH the tier limit and the rates change (e.g. a rate
    revision that also narrows the low-tier allowance): each period must
    price with its OWN tier_limit as well as its own rates, not the other
    period's. Hand-computed against the two rate sets below (polish item
    A2d)."""
    monkeypatch.setattr(config, "TARIFF_HISTORY", [
        TariffPeriod(date(2026, 1, 1), coopesantos_low=70.0, coopesantos_high=110.0,
                     tier_limit_kwh=0.15, pcss_flat=110.0),
        TariffPeriod(date(2026, 6, 1), coopesantos_low=90.0, coopesantos_high=140.0,
                     tier_limit_kwh=0.05, pcss_flat=140.0),
    ])
    es = compute_energy_summary(_edf([
        ("2026-05-15 12:00", 1200.0),   # May: 0.1 kWh, priced under the Jan entry
        ("2026-06-15 12:00", 2400.0),   # June: 0.2 kWh, priced under the Jun entry
    ]))
    monthly = es["monthly"].set_index("month")
    may_kwh = float(monthly.loc["2026-05", "kwh"])
    jun_kwh = float(monthly.loc["2026-06", "kwh"])
    assert may_kwh == pytest.approx(0.1)
    assert jun_kwh == pytest.approx(0.2)
    # May: 0.1 kWh is entirely inside its own 0.15 tier limit -> all at 70.
    assert monthly.loc["2026-05", "cost_tiered"] == pytest.approx(0.1 * 70.0)
    assert monthly.loc["2026-05", "cost_pcss"] == pytest.approx(0.1 * 110.0)
    # June: 0.2 kWh is over its own (narrower) 0.05 tier limit -> 0.05 kWh at
    # the June low rate (90) plus the remaining 0.15 kWh at the June high
    # rate (140): 0.05*90 + 0.15*140 = 4.5 + 21.0 = 25.5.
    assert monthly.loc["2026-06", "cost_tiered"] == pytest.approx(0.05 * 90.0 + 0.15 * 140.0)
    assert monthly.loc["2026-06", "cost_pcss"] == pytest.approx(0.2 * 140.0)


def test_period_before_earliest_history_uses_flat_keys(monkeypatch):
    """A period that starts before the earliest [[tariff.history]] entry
    falls back to the flat [tariff] keys, tagged as 'current rates'."""
    monkeypatch.setattr(config, "TARIFF_HISTORY", [
        TariffPeriod(date(2027, 1, 1), coopesantos_low=90.0, coopesantos_high=140.0,
                     tier_limit_kwh=200.0, pcss_flat=140.0),
    ])
    es = compute_energy_summary(_edf([
        ("2026-05-14 12:00", 1200.0),
        ("2026-05-15 12:00", 1200.0),
    ]))
    monthly = es["monthly"].set_index("month")
    kwh = float(monthly.loc["2026-05", "kwh"])
    assert monthly.loc["2026-05", "rate_tag"] == "current rates"
    assert monthly.loc["2026-05", "cost_pcss"] == pytest.approx(kwh * config.PCSS_FLAT_RATE)
