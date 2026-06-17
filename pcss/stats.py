"""Statistics, anomaly detection, energy/cost, runtime estimation, and
cross-validation over the loaded PCSS data."""
from __future__ import annotations

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


def detect_voltage_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    """Rows where Line Voltage falls outside the 114-126V envelope."""
    if df.empty or "Line Voltage" not in df.columns:
        return pd.DataFrame()
    mask = (df["Line Voltage"] < config.VOLTAGE_NORMAL_LOW) | (df["Line Voltage"] > config.VOLTAGE_NORMAL_HIGH)
    sub = df[mask & df["Line Voltage"].notna()][["ts", "Line Voltage"]]
    return sub.reset_index(drop=True)


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


def compute_energy_summary(edf: pd.DataFrame, interval_sec: int = 300) -> dict:
    """
    Total kWh, total cost (PCSS flat-rate AND Coopesantos tiered), total CO2
    plus per-day and per-month breakdowns.
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
    s["month"] = s["ts"].dt.to_period("M").astype(str)

    total_kwh = float(s["kwh"].sum())
    daily = s.groupby("date")["kwh"].sum().reset_index()
    monthly = s.groupby("month")["kwh"].sum().reset_index()
    monthly["cost_pcss"] = monthly["kwh"] * config.PCSS_FLAT_RATE
    monthly["cost_tiered"] = monthly["kwh"].apply(compute_tiered_cost)
    monthly["co2_kg"] = monthly["kwh"] * config.CO2_KG_PER_KWH

    return {
        "samples": s,
        "total_kwh": total_kwh,
        "total_cost_pcss": total_kwh * config.PCSS_FLAT_RATE,
        "total_cost_tiered": float(monthly["cost_tiered"].sum()),
        "total_co2_kg": total_kwh * config.CO2_KG_PER_KWH,
        "daily": daily,
        "monthly": monthly,
        "first": s["ts"].iloc[0],
        "last": s["ts"].iloc[-1],
        "n_samples": int(len(s)),
        "interval_sec": int(s["interval_sec"].median()) if "interval_sec" in s.columns else interval_sec,
    }


def compute_tiered_cost(kwh: float) -> float:
    """Coopesantos T-RE Residencial: first 200 kWh at LOW, rest at HIGH."""
    if pd.isna(kwh) or kwh <= 0:
        return 0.0
    if kwh <= config.COOPESANTOS_TIER_LIMIT_KWH:
        return kwh * config.COOPESANTOS_LOW_RATE
    return (config.COOPESANTOS_TIER_LIMIT_KWH * config.COOPESANTOS_LOW_RATE
            + (kwh - config.COOPESANTOS_TIER_LIMIT_KWH) * config.COOPESANTOS_HIGH_RATE)


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
