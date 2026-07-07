"""Statistics, anomaly detection, energy/cost, runtime estimation, and
cross-validation over the loaded PCSS data."""
from __future__ import annotations

import calendar
from datetime import datetime

import numpy as np
import pandas as pd

from pcss import config


def datalog_stats(df: pd.DataFrame, file_size: int) -> dict:
    if df.empty:
        return {}
    n = len(df)
    if n >= 2:
        span_seconds = max((df["ts"].iloc[-1] - df["ts"].iloc[0]).total_seconds(), 1)
        span_days = span_seconds / 86400
    else:
        # A single row gives no interval to project from. Returning a real
        # span here (the old max(..., 1) second) would divide the full file
        # size by ~1 second and project tens of GB/day. NaN says "unknown".
        span_days = float("nan")
    valid_span = span_days == span_days and span_days > 0  # NaN-safe
    daily_bytes = file_size / span_days if valid_span else float("nan")
    deltas = df["ts"].diff().dt.total_seconds().dropna()
    return {
        "first": df["ts"].iloc[0],
        "last": df["ts"].iloc[-1],
        "n_entries": n,
        "file_size": file_size,
        "bytes_per_entry": file_size / n if n else 0,
        "median_interval_sec": float(deltas.median()) if not deltas.empty else float("nan"),
        "span_days": span_days,
        "daily_entries": n / span_days if span_days > 0 else float("nan"),
        "daily_bytes": daily_bytes,
        "minute_bytes": daily_bytes / 1440 if daily_bytes else float("nan"),
        "monthly_bytes": daily_bytes * 30 if daily_bytes else float("nan"),
        "yearly_bytes": daily_bytes * 365 if daily_bytes else float("nan"),
    }


def compute_stats_summary(df: pd.DataFrame) -> pd.DataFrame:
    """
    Per-column min/mean/median/p95/max for numeric DataLog columns.
    Skips columns that are entirely N/A.
    """
    if df.empty:
        return pd.DataFrame()
    cols_of_interest = [
        "Line Voltage", "Battery Voltage", "Output Voltage",
        "UPS Load", "Battery Capacity", "Output Frequency", "Input Frequency",
        "Maximum Line Voltage", "Minimum Line Voltage",
        "UPS Internal Temperature", "Probe 1 Temperature", "Probe 1 Humidity",
    ]
    rows = []
    for c in cols_of_interest:
        if c not in df.columns:
            continue
        s = df[c].dropna()
        if s.empty:
            continue
        rows.append({
            "Metric": c,
            "Min": f"{s.min():.2f}",
            "Mean": f"{s.mean():.2f}",
            "Median": f"{s.median():.2f}",
            "p95": f"{s.quantile(0.95):.2f}",
            "Max": f"{s.max():.2f}",
            "Samples": int(len(s)),
        })
    return pd.DataFrame(rows)


def detect_gaps(df: pd.DataFrame, expected_interval_min: float | None = None) -> pd.DataFrame:
    """Find DataLog timestamp gaps > 2× expected interval (likely PC off / PCSS down)."""
    if expected_interval_min is None:
        expected_interval_min = config.DATALOG_EXPECTED_INTERVAL_MIN
    if df.empty or len(df) < 2:
        return pd.DataFrame()
    ts = df["ts"].to_numpy()
    deltas_min = df["ts"].diff().dt.total_seconds().to_numpy() / 60  # [0] is NaN
    # Positional indices of the gap rows — robust regardless of the frame's
    # index (NaN comparisons are False, so position 0 is never selected).
    gap_pos = np.where(deltas_min > (expected_interval_min * 2))[0]
    gaps = [
        {"from": ts[i - 1], "to": ts[i], "duration_min": float(deltas_min[i])}
        for i in gap_pos
    ]
    return pd.DataFrame(gaps)


def assess_staleness(newest_sample: datetime | pd.Timestamp, now: datetime | pd.Timestamp,
                     warn_hours: float | None = None,
                     crit_hours: float | None = None) -> dict:
    """Compare the DataLog's newest sample against the wall clock.

    Both timestamps are naive local time, the same convention as
    ``ts_2010_to_dt`` output, so subtracting them is safe with no timezone
    conversion. The caller (``analyze_ups.py``) reads the wall clock exactly
    once and passes it in here — this function never calls ``datetime.now()``
    itself, which keeps it testable with an injected ``now``.

    A PC being off overnight produces no samples with nothing actually wrong,
    so the default thresholds are generous: below ``stale_warn_hours`` (12)
    reads as fresh, at or beyond it reads as "warn", and at or beyond
    ``stale_crit_hours`` (48) reads as "crit" — a dead serial link, a stopped
    service, or a wedged agent, not just an ordinary quiet evening. This is a
    distinct fact from the historical sampling gaps ``detect_gaps`` reports:
    that function looks for gaps already closed by later samples, while this
    one asks whether the feed is stale right now.

    Returns a dict with ``level`` ("fresh", "warn", or "crit") and
    ``age_hours`` (float, clamped to 0 if ``now`` is somehow earlier than
    ``newest_sample`` — clock skew, not negative staleness).
    """
    if warn_hours is None:
        warn_hours = config.STALE_WARN_HOURS
    if crit_hours is None:
        crit_hours = config.STALE_CRIT_HOURS
    age_hours = max(0.0, (now - newest_sample).total_seconds() / 3600.0)
    if age_hours >= crit_hours:
        level = "crit"
    elif age_hours >= warn_hours:
        level = "warn"
    else:
        level = "fresh"
    return {"level": level, "age_hours": age_hours}


def detect_voltage_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    """Rows where Line Voltage falls outside the 114-126V envelope."""
    if df.empty or "Line Voltage" not in df.columns:
        return pd.DataFrame()
    mask = (df["Line Voltage"] < config.VOLTAGE_NORMAL_LOW) | (df["Line Voltage"] > config.VOLTAGE_NORMAL_HIGH)
    sub = df[mask & df["Line Voltage"].notna()][["ts", "Line Voltage"]]
    return sub.reset_index(drop=True)


def detect_on_battery_episodes(df: pd.DataFrame, voltage_max: float | None = None,
                               min_capacity_drop_pct: float | None = None) -> pd.DataFrame:
    """Infer on-battery episodes from the DataLog without the EventLog.

    A sample whose line voltage sits below ``voltage_max`` is a mains-loss
    candidate; consecutive candidates form one episode. An episode is kept
    only when the battery capacity fell by at least ``min_capacity_drop_pct``
    from just before the episode to its lowest in-episode reading — a lone
    0 V sample with a flat capacity is treated as a logging artifact. When
    the frame has no capacity column at all there is nothing to corroborate
    against, so episodes are reported with an unknown (NaN) drop instead.

    Honest labeling: the 20-minute DataLog cadence misses most short outages
    entirely, so this detects "episodes visible at the sampling cadence",
    not all outages. Returns one row per episode: start, end, duration_min,
    min_voltage, capacity_drop_pct.
    """
    if voltage_max is None:
        voltage_max = config.ON_BATTERY_VOLTAGE_V
    if min_capacity_drop_pct is None:
        min_capacity_drop_pct = config.ON_BATTERY_CAPACITY_DROP_PCT
    if df.empty or "Line Voltage" not in df.columns:
        return pd.DataFrame()
    lv = df["Line Voltage"]
    candidate = lv.notna() & (lv < voltage_max)
    if not candidate.any():
        return pd.DataFrame()
    has_capacity = "Battery Capacity" in df.columns and df["Battery Capacity"].notna().any()
    runs = candidate.ne(candidate.shift()).cumsum()
    episodes = []
    for _, group in df[candidate].groupby(runs[candidate]):
        start, end = group["ts"].iloc[0], group["ts"].iloc[-1]
        # Like the high-load detector, each sample stands for one sampling
        # interval, so a k-sample episode spans k intervals, not k-1.
        duration_min = ((end - start).total_seconds() / 60
                        + config.DATALOG_EXPECTED_INTERVAL_MIN)
        drop = float("nan")
        if has_capacity:
            first_pos = df.index.get_loc(group.index[0])
            before = df["Battery Capacity"].iloc[:first_pos].dropna()
            base = float(before.iloc[-1]) if not before.empty else None
            in_ep = group["Battery Capacity"].dropna()
            if base is None and not in_ep.empty:
                base = float(in_ep.iloc[0])
            if base is not None and not in_ep.empty:
                drop = base - float(in_ep.min())
            if not (drop >= min_capacity_drop_pct):
                continue        # not corroborated (also skips NaN drops)
        episodes.append({
            "start": start, "end": end, "duration_min": duration_min,
            "min_voltage": float(group["Line Voltage"].min()),
            "capacity_drop_pct": drop,
        })
    return pd.DataFrame(episodes)


def detect_high_load_episodes(edf: pd.DataFrame, threshold_pct: float | None = None,
                              min_duration_sec: int = 600) -> pd.DataFrame:
    """
    Find sustained high-load episodes from energylog (>= threshold for >= min_duration).
    Returns one row per episode: start, end, duration_min, peak_pct, peak_w.
    """
    if threshold_pct is None:
        threshold_pct = config.HIGH_LOAD_PCT
    if edf.empty or "load_pct" not in edf.columns:
        return pd.DataFrame()
    above = edf["load_pct"] >= threshold_pct
    if not above.any():
        return pd.DataFrame()
    # Group consecutive True runs
    edges = above.ne(above.shift()).cumsum()
    episodes = []
    for _, group in edf[above].groupby(edges[above]):
        start = group["ts"].iloc[0]
        end = group["ts"].iloc[-1]
        # Each sample covers one sampling interval, so a k-sample run spans
        # ~k*interval of high load, not the (k-1)*interval between first and
        # last timestamps. Add one interval so a 2-sample (10-min) run counts.
        interval = float(group["interval_sec"].iloc[0]) if "interval_sec" in group.columns else 0.0
        duration_sec = (end - start).total_seconds() + interval
        if duration_sec >= min_duration_sec:
            episodes.append({
                "start": start, "end": end,
                "duration_min": duration_sec / 60,
                "peak_pct": float(group["load_pct"].max()),
                "peak_w": float(group["power_w"].max()),
            })
    return pd.DataFrame(episodes)


def battery_replace_projection(df: pd.DataFrame, threshold_v: float | None = None,
                               min_days: float | None = None) -> dict:
    """Project when the resting battery voltage crosses the replace threshold.

    Fits a line to the rolling median of Battery Voltage (the median damps
    the sawtooth that capacity self-tests carve into the raw readings) and
    extrapolates to ``threshold_v``. Below ``min_days`` of history the honest
    answer is "not enough history": a slope over a few weeks is noise.

    Returns a dict with ``status`` ("insufficient_history", "stable", or
    "projected"), ``slope_v_per_day``, ``replace_date``, ``days_to_replace``,
    and ``threshold_v``.
    """
    if threshold_v is None:
        threshold_v = config.BATTERY_REPLACE_VOLTAGE_V
    if min_days is None:
        min_days = config.BATTERY_TREND_MIN_DAYS
    out: dict = {"status": "insufficient_history", "slope_v_per_day": None,
                 "replace_date": None, "days_to_replace": None, "threshold_v": threshold_v}
    if df.empty or "Battery Voltage" not in df.columns:
        return out
    bv = df.dropna(subset=["Battery Voltage"])
    if len(bv) < 10:
        return out
    span_days = (bv["ts"].iloc[-1] - bv["ts"].iloc[0]).total_seconds() / 86400
    if span_days < min_days:
        return out
    # Rolling median over roughly eight hours at the 20-minute cadence.
    med = bv["Battery Voltage"].rolling(window=24, min_periods=3, center=True).median()
    mask = med.notna()
    days = (bv["ts"] - bv["ts"].iloc[0]).dt.total_seconds().to_numpy() / 86400.0
    slope, intercept = np.polyfit(days[mask.to_numpy()], med[mask].to_numpy(dtype=float), 1)
    out["slope_v_per_day"] = float(slope)
    # A slope smaller than a millivolt per day would take decades to matter;
    # report the battery as stable rather than projecting a fantasy date.
    if slope >= -1e-3:
        out["status"] = "stable"
        return out
    v_now = intercept + slope * days[-1]
    days_to = max(0.0, (threshold_v - v_now) / slope)
    out["status"] = "projected"
    out["days_to_replace"] = float(days_to)
    out["replace_date"] = bv["ts"].iloc[-1] + pd.Timedelta(days=days_to)
    return out


def _billing_period_start(ts: pd.Timestamp, start_day: int) -> pd.Timestamp:
    """Date on which ts's billing period began.

    A start day beyond a month's length clamps to that month's last day (a
    cycle anchored on the 31st begins on Feb 28 in February).
    """
    y, m = ts.year, ts.month
    day = min(start_day, calendar.monthrange(y, m)[1])
    if ts.day >= day:
        return pd.Timestamp(y, m, day)
    y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return pd.Timestamp(y, m, min(start_day, calendar.monthrange(y, m)[1]))


def _billing_period_bounds(label: str, start_day: int) -> tuple[pd.Timestamp, pd.Timestamp]:
    """(start, end) of a period label — 'YYYY-MM' calendar or 'YYYY-MM-DD'."""
    if len(label) == 7:
        start = pd.Timestamp(label + "-01")
        return start, start + pd.offsets.MonthBegin(1)
    start = pd.Timestamp(label)
    y, m = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return start, pd.Timestamp(y, m, min(start_day, calendar.monthrange(y, m)[1]))


def compute_energy_summary(edf: pd.DataFrame, interval_sec: int = 300) -> dict:
    """
    Total kWh, total cost (PCSS flat-rate AND Coopesantos tiered), total CO2
    plus per-day and per-billing-period breakdowns. With the default
    billing_cycle_start_day of 1 the periods are calendar months (labeled
    'YYYY-MM'); any other start day groups by Coopesantos billing period
    (labeled by the period's start date) and the tier limit applies per
    period. Periods the recorded span does not fully cover carry
    partial=True so an incomplete tier split is never mistaken for a bill.

    When config.TARIFF_HISTORY holds one or more [[tariff.history]] entries
    (item 17), each period's cost is priced with the rates in force on the
    period's own start date instead of today's flat rates, and the "monthly"
    frame gains a rate_tag column ("current rates" or "rates from
    YYYY-MM-DD") saying which rates priced it — otherwise a rate boundary
    mid-history would look like a consumption change. The result dict's
    tariff_history_active flag mirrors whether that lookup is in play; with
    no history entries, every number here is identical to before this
    feature existed.
    """
    if edf.empty or "power_w" not in edf.columns:
        return {}
    s = edf.dropna(subset=["power_w"]).copy()
    if s.empty:
        return {}
    # Energy per sample (Wh): power_W * (interval_sec / 3600). Prefer the
    # per-sample interval recorded from each energylog header so kWh stays
    # correct even if PCSS is reconfigured to a different energylog sampling
    # interval partway through the history; fall back to the interval_sec arg.
    sample_interval = s["interval_sec"].astype(float) if "interval_sec" in s.columns else float(interval_sec)
    s["wh"] = s["power_w"] * (sample_interval / 3600.0)
    s["kwh"] = s["wh"] / 1000.0
    s["date"] = s["ts"].dt.date
    start_day = int(getattr(config, "BILLING_CYCLE_START_DAY", 1))
    if start_day <= 1:
        s["month"] = s["ts"].dt.to_period("M").astype(str)
    else:
        s["month"] = [f"{_billing_period_start(t, start_day):%Y-%m-%d}" for t in s["ts"]]

    total_kwh = float(s["kwh"].sum())
    daily = s.groupby("date")["kwh"].sum().reset_index()
    monthly = s.groupby("month")["kwh"].sum().reset_index()
    monthly["cost_pcss"] = monthly["kwh"] * config.PCSS_FLAT_RATE
    monthly["cost_tiered"] = monthly["kwh"].apply(compute_tiered_cost)
    monthly["co2_kg"] = monthly["kwh"] * config.CO2_KG_PER_KWH
    # A period is partial when the recorded span starts after it begins or
    # ends before it does (one sampling interval of tolerance).
    first_ts, last_ts = s["ts"].iloc[0], s["ts"].iloc[-1]
    tol = pd.Timedelta(seconds=1.5 * float(np.median(np.asarray(sample_interval))))
    history_active = bool(config.TARIFF_HISTORY)
    partials = []
    rate_tags = []
    period_cost_pcss = []
    period_cost_tiered = []
    for label, kwh_val in zip(monthly["month"], monthly["kwh"], strict=True):
        p_start, p_end = _billing_period_bounds(str(label), start_day)
        partials.append(bool(first_ts > p_start + tol or last_ts < p_end - tol))
        if history_active:
            low, high, tier_limit, flat, tag = config.tariff_rates_for(p_start.date())
            rate_tags.append(tag)
            period_cost_pcss.append(float(kwh_val) * flat)
            period_cost_tiered.append(compute_tiered_cost(
                float(kwh_val), low=low, high=high, tier_limit=tier_limit))
    monthly["partial"] = partials
    if history_active:
        monthly["cost_pcss"] = period_cost_pcss
        monthly["cost_tiered"] = period_cost_tiered
        monthly["rate_tag"] = rate_tags

    return {
        "samples": s,
        "total_kwh": total_kwh,
        "total_cost_pcss": (float(monthly["cost_pcss"].sum()) if history_active
                            else total_kwh * config.PCSS_FLAT_RATE),
        "total_cost_tiered": float(monthly["cost_tiered"].sum()),
        "total_co2_kg": total_kwh * config.CO2_KG_PER_KWH,
        "daily": daily,
        "monthly": monthly,
        "first": s["ts"].iloc[0],
        "last": s["ts"].iloc[-1],
        "n_samples": int(len(s)),
        "interval_sec": int(s["interval_sec"].median()) if "interval_sec" in s.columns else interval_sec,
        "tariff_history_active": history_active,
    }


def forecast_period_cost(energy_summary: dict, *, min_days: float | None = None) -> dict:
    """Project the current billing period's recorded kWh to the period's end
    date and price it, both flat and tiered, so "what will this bill be?"
    can be answered before the period closes.

    The current period is the most recent one in energy_summary's per-sample
    frame — the same choice _panel_cmp already makes for the Period
    Comparison card, so a period that has not started accumulating
    energylog samples yet is simply absent rather than silently forecasting
    an already-closed prior period. Evidence is the count of distinct
    calendar days with at least one energylog sample inside that period, not
    the number of calendar days since the period started: a day with no
    samples contributes nothing to the per-day mean, and this keeps the
    floor honest even when the analyzed window itself lags behind today.
    Below min_days (config.FORECAST_MIN_DAYS, default 5) the honest result
    carries no numbers — the same floor pattern as
    battery_replace_projection's minimum-history guard.

    The projection itself is the plain per-day mean: the period's recorded
    kWh divided by its evidence days, multiplied by the number of days in
    the whole period, then priced with the rates in force for the period's
    start date (config.tariff_rates_for, item 17) — both the PCSS flat rate
    and the Coopesantos tiered rate (compute_tiered_cost). If the projected
    total would cross the tier limit before the period ends, the crossing
    date is the same linear rate solved backward from the period start; if
    the kWh already recorded this period has already crossed the limit, that
    fact is reported directly (already_crossed) instead of a projected
    crossing date — a measurement, not a projection.

    Returns a dict with: status ("insufficient_evidence" or "projected"),
    period_start, period_end (date or None), min_days, evidence_days (int),
    projected_kwh, projected_cost_pcss, projected_cost_tiered (float or
    None), tier_cross_date (date or None), already_crossed (bool), and
    rate_tag (str or None — item 17's "which rates priced this" label).
    """
    if min_days is None:
        min_days = config.FORECAST_MIN_DAYS
    out: dict = {
        "status": "insufficient_evidence",
        "period_start": None, "period_end": None,
        "min_days": min_days, "evidence_days": 0,
        "projected_kwh": None, "projected_cost_pcss": None,
        "projected_cost_tiered": None, "tier_cross_date": None,
        "already_crossed": False, "rate_tag": None,
    }
    if not energy_summary or "samples" not in energy_summary:
        return out
    s = energy_summary["samples"]
    if s.empty:
        return out
    labels = list(dict.fromkeys(s["month"]))    # in time order, deduplicated
    label = labels[-1]
    start_day = int(getattr(config, "BILLING_CYCLE_START_DAY", 1))
    p_start, p_end = _billing_period_bounds(str(label), start_day)
    out["period_start"] = p_start.date()
    out["period_end"] = p_end.date()
    part = s[s["month"] == label]
    evidence_days = int(part["ts"].dt.date.nunique())
    out["evidence_days"] = evidence_days
    if evidence_days < min_days:
        return out
    kwh_so_far = float(part["kwh"].sum())
    period_days = (p_end - p_start).total_seconds() / 86400.0
    per_day_kwh = kwh_so_far / evidence_days
    projected_kwh = per_day_kwh * period_days
    low, high, tier_limit, flat, rate_tag = config.tariff_rates_for(p_start.date())
    out["status"] = "projected"
    out["projected_kwh"] = projected_kwh
    out["projected_cost_pcss"] = projected_kwh * flat
    out["projected_cost_tiered"] = compute_tiered_cost(
        projected_kwh, low=low, high=high, tier_limit=tier_limit)
    out["rate_tag"] = rate_tag
    if kwh_so_far >= tier_limit:
        out["already_crossed"] = True
    elif per_day_kwh > 0:
        days_to_cross = tier_limit / per_day_kwh
        if days_to_cross <= period_days:
            out["tier_cross_date"] = (p_start + pd.Timedelta(days=days_to_cross)).date()
    return out


def compute_tiered_cost(kwh: float, *, low: float | None = None, high: float | None = None,
                        tier_limit: float | None = None) -> float:
    """Coopesantos T-RE Residencial: first tier_limit kWh at low, rest at
    high. Defaults to the current flat [tariff] config keys; a per-period
    historical lookup (item 17) passes explicit rates instead."""
    if pd.isna(kwh) or kwh <= 0:
        return 0.0
    low = config.COOPESANTOS_LOW_RATE if low is None else low
    high = config.COOPESANTOS_HIGH_RATE if high is None else high
    tier_limit = config.COOPESANTOS_TIER_LIMIT_KWH if tier_limit is None else tier_limit
    if kwh <= tier_limit:
        return kwh * low
    return tier_limit * low + (kwh - tier_limit) * high


def estimate_runtime(power_w: float) -> float:
    """Estimated UPS runtime (minutes) at given load. Empirical curve."""
    if pd.isna(power_w) or power_w <= 0:
        return float(config.RUNTIME_CURVE_MIN[0])
    return float(np.interp(power_w, config.RUNTIME_CURVE_W, config.RUNTIME_CURVE_MIN))


def cross_validate_load(datalog_df: pd.DataFrame, energylog_df: pd.DataFrame) -> dict:
    """
    For each DataLog 'UPS Load' sample, find the nearest energylog sample
    and compare load_pct. Report mean abs error.
    """
    if datalog_df.empty or energylog_df.empty:
        return {}
    if "UPS Load" not in datalog_df.columns:
        return {}
    dl = datalog_df.dropna(subset=["UPS Load"])[["ts", "UPS Load"]].copy()
    el = energylog_df.dropna(subset=["load_pct"])[["ts", "load_pct"]].copy()
    if dl.empty or el.empty:
        return {}
    dl = dl.sort_values("ts").reset_index(drop=True)
    el = el.sort_values("ts").reset_index(drop=True)
    merged = pd.merge_asof(dl, el, on="ts", direction="nearest", tolerance=pd.Timedelta(minutes=10))
    merged = merged.dropna(subset=["load_pct"])
    if merged.empty:
        return {}
    diff = (merged["UPS Load"] - merged["load_pct"]).abs()
    return {
        "n_pairs": int(len(merged)),
        "mean_abs_error_pct": float(diff.mean()),
        "max_abs_error_pct": float(diff.max()),
        "datalog_mean_pct": float(merged["UPS Load"].mean()),
        "energylog_mean_pct": float(merged["load_pct"].mean()),
    }
