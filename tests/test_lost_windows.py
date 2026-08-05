"""Unit tests for lost-telemetry detection and reconstruction (no browser).

The detector must be exact: null-power runs are the only trigger, window
boundaries land on the null rows themselves, and a fully healthy frame can
never produce a window (the false-positive guard). The reconstruction must
match hand-computed arithmetic on constant-power fixtures, and the
reconciliation note must never change a measured number.
"""
from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

import pcss.config as cfg
from pcss.stats import (
    compute_energy_summary,
    detect_lost_windows,
    hourly_profile_with_std,
    reconcile_bills,
    reconstruct_lost_windows,
)


def _frame(segments):
    """An energylog-shaped frame from (start, n_rows, power) segments at the
    5-minute cadence; power None means null rows (NaN power and load), the
    signature PCSS writes while the UPS link is down."""
    rows = []
    for start, n, power in segments:
        for t in pd.date_range(start, periods=n, freq="5min"):
            rows.append({"ts": t,
                         "power_w": np.nan if power is None else float(power),
                         "load_pct": np.nan if power is None else 20.0,
                         "interval_sec": 300})
    if not rows:
        return pd.DataFrame(columns=["ts", "power_w", "load_pct", "interval_sec"])
    return pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)


def _datalog(n=72, start="2026-05-01 00:00", lv=120.0, ul=18.0):
    ts = pd.date_range(start, periods=n, freq="20min")
    return pd.DataFrame({"ts": ts,
                         "Line Voltage": np.full(n, lv),
                         "UPS Load": np.full(n, ul)})


# ---------------------------------------------------------------- detector
def test_healthy_frame_has_no_windows():
    """The false-positive guard: a frame with no null-power rows can never
    produce a lost window, whatever its gaps look like."""
    df = _frame([("2026-05-01 00:00", 864, 240.0)])
    assert detect_lost_windows(df).empty
    # A frame with big row holes (the PC off) but no nulls stays clean too.
    holey = _frame([("2026-05-01 00:00", 100, 240.0),
                    ("2026-05-02 12:00", 100, 240.0)])
    assert detect_lost_windows(holey).empty


def test_empty_and_missing_columns():
    assert detect_lost_windows(pd.DataFrame()).empty
    assert detect_lost_windows(None).empty


def test_null_run_boundaries_are_exact():
    df = _frame([("2026-05-01 00:00", 100, 240.0),
                 ("2026-05-01 08:20", 30, None),
                 ("2026-05-01 10:50", 100, 240.0)])
    out = detect_lost_windows(df)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["from"] == pd.Timestamp("2026-05-01 08:20")
    assert row["to"] == pd.Timestamp("2026-05-01 10:45")
    assert row["n_rows"] == 30
    assert row["hours"] == pytest.approx(30 * 300 / 3600)
    assert row["incident"] == 0


def test_short_null_run_is_ignored():
    """A run below the 30-minute floor (a stray startup null) never
    registers; one at 35 minutes does."""
    short = _frame([("2026-05-01 00:00", 50, 240.0),
                    ("2026-05-01 04:10", 5, None),
                    ("2026-05-01 04:35", 50, 240.0)])
    assert detect_lost_windows(short).empty
    long = _frame([("2026-05-01 00:00", 50, 240.0),
                   ("2026-05-01 04:10", 7, None),
                   ("2026-05-01 04:45", 50, 240.0)])
    assert len(detect_lost_windows(long)) == 1


def test_night_split_stretches_share_an_incident():
    """Two null runs separated only by a row-less hole (the PC off
    overnight) are two stretches but one incident; a healthy row between
    them splits the incidents."""
    same = _frame([("2026-05-01 00:00", 100, 240.0),
                   ("2026-05-01 08:20", 100, None),
                   ("2026-05-02 10:00", 100, None),
                   ("2026-05-02 18:20", 50, 240.0)])
    out = detect_lost_windows(same)
    assert len(out) == 2
    assert list(out["incident"]) == [0, 0]

    split = _frame([("2026-05-01 00:00", 100, 240.0),
                    ("2026-05-01 08:20", 100, None),
                    ("2026-05-02 08:00", 24, 240.0),
                    ("2026-05-02 10:00", 100, None),
                    ("2026-05-02 18:20", 50, 240.0)])
    out2 = detect_lost_windows(split)
    assert len(out2) == 2
    assert list(out2["incident"]) == [0, 1]


# ---------------------------------------------------------------- profiles
def test_hourly_profile_mean_and_std():
    edf = _frame([("2026-05-04 00:00", 288, 240.0)])   # a Monday
    prof = hourly_profile_with_std(edf, "power_w")
    assert "weekday" in prof and "weekend" not in prof
    assert prof["weekday"]["mean"].loc[3] == pytest.approx(240.0)
    assert prof["weekday"]["std"].loc[3] == pytest.approx(0.0)


# ---------------------------------------------------------------- reconstruction
def test_reconstruction_constant_power_arithmetic():
    """With a flat 240 W healthy profile, the estimated energy for a null
    run is exactly rows x 240 W x 5 min, and the band collapses onto the
    mean (std 0)."""
    df = _frame([("2026-05-01 00:00", 100, 240.0),
                 ("2026-05-01 08:20", 12, None),
                 ("2026-05-01 09:20", 100, 240.0)])
    stretches = detect_lost_windows(df)
    lost = reconstruct_lost_windows(stretches, _datalog(), df)
    expected_kwh = 12 * 240.0 / 1000.0 * (300 / 3600)
    assert lost["total_est_kwh"] == pytest.approx(expected_kwh)
    assert lost["total_hours"] == pytest.approx(1.0)
    assert len(lost["incidents"]) == 1
    assert lost["incidents"]["est_kwh"].iloc[0] == pytest.approx(expected_kwh)
    # The pw band spans the stretch exactly and collapses onto the mean.
    seg = lost["recon"]["pw"][0]
    assert seg["ts"][0] == stretches["from"].iloc[0]
    assert seg["ts"][-1] == stretches["to"].iloc[0]
    assert seg["mean"][0] == pytest.approx(240.0)
    assert seg["lo"][0] == pytest.approx(240.0)
    assert seg["hi"][0] == pytest.approx(240.0)
    # The DataLog channels reconstruct from their own profiles.
    assert lost["recon"]["lv"][0]["mean"][0] == pytest.approx(120.0)
    assert lost["recon"]["ul"][0]["mean"][0] == pytest.approx(18.0)


def test_reconstruction_band_is_two_sigma():
    """Alternating 200/280 W (population std 40) gives a 200-360 W band."""
    rows = []
    for i, t in enumerate(pd.date_range("2026-05-04 00:00", periods=288, freq="5min")):
        rows.append({"ts": t, "power_w": 200.0 if i % 2 else 280.0,
                     "load_pct": 20.0, "interval_sec": 300})
    base = pd.DataFrame(rows)
    null_part = _frame([("2026-05-05 10:00", 12, None)])
    tail = _frame([("2026-05-05 11:00", 24, 240.0)])
    df = pd.concat([base, null_part, tail], ignore_index=True)
    lost = reconstruct_lost_windows(detect_lost_windows(df), _datalog(), df)
    seg = lost["recon"]["pw"][0]
    assert seg["mean"][0] == pytest.approx(240.0)
    assert seg["hi"][0] - seg["mean"][0] == pytest.approx(2 * 40.0)
    assert seg["mean"][0] - seg["lo"][0] == pytest.approx(2 * 40.0)


def test_empty_stretches_reconstruct_to_empty():
    lost = reconstruct_lost_windows(pd.DataFrame(), _datalog(), _frame([]))
    assert lost["total_est_kwh"] == 0.0
    assert lost["incidents"].empty
    assert lost["recon"] == {}


def test_by_period_pricing_flat_and_marginal_tiered():
    """The flat estimate is kWh x flat rate; the tiered estimate is the
    marginal cost on top of the measured period total (all inside the low
    tier here)."""
    df = _frame([("2026-05-01 00:00", 100, 240.0),
                 ("2026-05-01 08:20", 12, None),
                 ("2026-05-01 09:20", 100, 240.0)])
    summary = compute_energy_summary(df)
    lost = reconstruct_lost_windows(detect_lost_windows(df), _datalog(), df, summary)
    per = lost["by_period"]
    assert list(per["period"]) == ["2026-05"]
    est = float(per["est_kwh"].iloc[0])
    assert est == pytest.approx(lost["total_est_kwh"])
    assert float(per["est_cost_pcss"].iloc[0]) == pytest.approx(est * cfg.PCSS_FLAT_RATE)
    # Measured (4 kWh) plus estimate stays under the 200 kWh tier limit, so
    # the marginal tiered cost is the low rate exactly.
    assert float(per["est_cost_tiered"].iloc[0]) == pytest.approx(est * cfg.COOPESANTOS_LOW_RATE)


def test_measured_statistics_unchanged_by_lost_windows():
    """The measured summary is computed from the same frame whether or not
    null rows exist in it: nulls contribute nothing and the estimate is
    never folded in."""
    healthy = _frame([("2026-05-01 00:00", 100, 240.0),
                      ("2026-05-01 09:20", 100, 240.0)])
    with_nulls = _frame([("2026-05-01 00:00", 100, 240.0),
                         ("2026-05-01 08:20", 12, None),
                         ("2026-05-01 09:20", 100, 240.0)])
    assert (compute_energy_summary(healthy)["total_kwh"]
            == pytest.approx(compute_energy_summary(with_nulls)["total_kwh"]))


# ---------------------------------------------------------------- reconciliation note
def test_reconcile_bill_gets_lost_note_and_measured_numbers():
    df = _frame([("2026-05-01 00:00", 100, 240.0),
                 ("2026-05-01 08:20", 12, None),
                 ("2026-05-01 09:20", 100, 240.0)])
    summary = compute_energy_summary(df)
    lost = reconstruct_lost_windows(detect_lost_windows(df), _datalog(), df, summary)
    bills = pd.DataFrame([{"period_start": date(2026, 5, 1),
                           "kwh": 100.0, "amount_crc": 8000.0}])
    reconciled, notes = reconcile_bills(bills, summary, lost=lost)
    assert len(reconciled) == 1
    # The reconciled kWh stays the measured figure, untouched by the estimate.
    assert reconciled["ups_kwh"].iloc[0] == pytest.approx(summary["total_kwh"])
    lost_notes = [n for n in notes if "telemetry" in n]
    assert len(lost_notes) == 1
    assert "1.0 h" in lost_notes[0]
    assert "estimated" in lost_notes[0]


def test_reconcile_without_lost_has_no_note():
    df = _frame([("2026-05-01 00:00", 200, 240.0)])
    summary = compute_energy_summary(df)
    bills = pd.DataFrame([{"period_start": date(2026, 5, 1),
                           "kwh": 100.0, "amount_crc": 8000.0}])
    _reconciled, notes = reconcile_bills(bills, summary)
    assert not any("telemetry" in n for n in notes)


# ---------------------------------------------------------------- once-per-incident alerts
def _incidents(rows):
    return pd.DataFrame(rows, columns=["from", "to", "n_stretches", "hours", "est_kwh"])


def test_lost_alert_fires_once_per_incident(tmp_path, monkeypatch):
    import analyze_ups
    monkeypatch.setattr(cfg, "ALERTS_ENABLED", True)
    monkeypatch.setattr(cfg, "ALERTS_LOG", tmp_path / "alerts.log")
    monkeypatch.setattr(cfg, "LOST_ALERT_MARKER", tmp_path / "last_lost_alert.txt")
    incidents = _incidents([
        (pd.Timestamp("2026-07-15 11:54"), pd.Timestamp("2026-07-16 01:24"), 1, 13.5, 3.7),
        (pd.Timestamp("2026-07-25 13:17"), pd.Timestamp("2026-08-02 17:39"), 11, 108.7, 28.8),
    ])
    now = pd.Timestamp("2026-08-05 09:00")
    assert analyze_ups._maybe_write_lost_alerts({"incidents": incidents}, now) is not None
    lines = (tmp_path / "alerts.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all("data_lost" in line for line in lines)
    # A rerun over the same incidents appends nothing (the watermark).
    assert analyze_ups._maybe_write_lost_alerts({"incidents": incidents}, now) is None
    assert len((tmp_path / "alerts.log").read_text(encoding="utf-8").splitlines()) == 2
    # A later incident fires exactly one new line.
    more = pd.concat([incidents, _incidents([
        (pd.Timestamp("2026-08-04 10:00"), pd.Timestamp("2026-08-04 14:00"), 1, 4.0, 1.0),
    ])], ignore_index=True)
    assert analyze_ups._maybe_write_lost_alerts({"incidents": more}, now) is not None
    assert len((tmp_path / "alerts.log").read_text(encoding="utf-8").splitlines()) == 3


def test_lost_alert_gates(tmp_path, monkeypatch):
    import analyze_ups
    monkeypatch.setattr(cfg, "ALERTS_LOG", tmp_path / "alerts.log")
    monkeypatch.setattr(cfg, "LOST_ALERT_MARKER", tmp_path / "last_lost_alert.txt")
    incidents = _incidents([
        (pd.Timestamp("2026-07-15 11:54"), pd.Timestamp("2026-07-16 01:24"), 1, 13.5, 3.7),
    ])
    monkeypatch.setattr(cfg, "ALERTS_ENABLED", False)
    assert analyze_ups._maybe_write_lost_alerts({"incidents": incidents}) is None
    monkeypatch.setattr(cfg, "ALERTS_ENABLED", True)
    assert analyze_ups._maybe_write_lost_alerts(None) is None
    assert analyze_ups._maybe_write_lost_alerts({"incidents": pd.DataFrame()}) is None
    assert not (tmp_path / "alerts.log").exists()


# ---------------------------------------------------------------- dashboard windowing
def test_window_lost_keeps_visible_tail():
    """The max_days cut keeps a stretch whose end reaches into the window
    (the still-visible-tail rule spans follow everywhere) and recomputes
    the totals from what remains."""
    import analyze_ups
    df = _frame([("2026-05-01 00:00", 100, 240.0),
                 ("2026-05-01 08:20", 12, None),
                 ("2026-05-01 09:20", 100, 240.0),
                 ("2026-05-20 00:00", 100, 240.0),
                 ("2026-05-20 08:20", 12, None),
                 ("2026-05-20 09:20", 100, 240.0)])
    lost = reconstruct_lost_windows(detect_lost_windows(df), _datalog(), df)
    assert len(lost["stretches"]) == 2

    cut = analyze_ups._window_lost(lost, pd.Timestamp("2026-05-10"))
    assert len(cut["stretches"]) == 1
    assert cut["stretches"]["from"].iloc[0] == pd.Timestamp("2026-05-20 08:20")
    assert cut["total_hours"] == pytest.approx(1.0)
    for segs in cut["recon"].values():
        assert len(segs) == 1
    # No cutoff: unchanged. Everything cut away: None.
    assert analyze_ups._window_lost(lost, None) is lost
    assert analyze_ups._window_lost(lost, pd.Timestamp("2026-06-01")) is None
    assert analyze_ups._window_lost(None, pd.Timestamp("2026-05-10")) is None
