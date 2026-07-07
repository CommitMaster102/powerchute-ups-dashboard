"""Statistics, anomaly detection, energy/cost, runtime estimation, and
cross-validation over the loaded PCSS data."""
from __future__ import annotations

import calendar
from datetime import datetime

import numpy as np
import pandas as pd

from pcss import config, eventlog


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


def weekday_weekend_profiles(energy_df: pd.DataFrame) -> dict[str, pd.Series]:
    """Mean power by hour of day, split into weekday and weekend, from the
    energylog's full history — this is the definition of "what a normal day
    looks like" that both the Weekday vs Weekend dashboard card
    (``pcss.dashboard._panel_wk``) and ``detect_baseline_deviations`` below
    share, so the two views of "normal" cannot drift apart.

    Returns a dict with keys ``"weekday"`` and ``"weekend"``, each a pandas
    Series indexed by hour of day (0 through 23) holding the mean
    ``power_w`` for that hour across every day of that type in the frame. A
    key is simply absent when the frame has no days of that type — a
    history shorter than a week can easily have no weekend days yet.
    """
    profiles: dict[str, pd.Series] = {}
    if energy_df.empty or "power_w" not in energy_df.columns:
        return profiles
    s = energy_df.dropna(subset=["power_w"])
    if s.empty:
        return profiles
    dow = s["ts"].dt.dayofweek
    for mask, name in [(dow < 5, "weekday"), (dow >= 5, "weekend")]:
        part = s[mask]
        if part.empty:
            continue
        profiles[name] = part.groupby(part["ts"].dt.hour)["power_w"].mean()
    return profiles


_BASELINE_DEVIATION_COLUMNS = ["date", "day_type", "deviation_pct"]


def detect_baseline_deviations(energy_df: pd.DataFrame, deviation_pct: float | None = None,
                               min_days: int | None = None) -> dict:
    """Flag energylog days whose shape deviates sharply from the recorded
    weekday/weekend baseline (``weekday_weekend_profiles``) — a stuck-on
    appliance, a new always-on load, or a failing power supply shows up as a
    flagged day instead of a chart someone has to remember to read.

    This flags a deviation from the recorded baseline, not a fault: a
    holiday, a guest staying over, or simply not enough history yet can
    produce the same signal, so every surface that shows this result must
    say "deviates from the recorded baseline" and nothing stronger.

    A day qualifies for comparison when it is "complete": every day in the
    frame except the trailing partial one (the most recent calendar date,
    which has not finished accumulating samples yet, and would therefore
    always look anomalous) and any day whose sample count falls far short
    of its peers (a mid-history sampling gap, not a real change in
    behavior). For each qualifying day, its own hourly mean-power profile is
    compared against whichever baseline profile matches the day's type
    (weekday or weekend), computed from the full history including the day
    itself — the same profiles the Weekday vs Weekend dashboard card draws.
    The deviation metric is the mean absolute difference between the day's
    profile and the baseline profile across their shared hours, expressed as
    a percent of the baseline profile's own mean power: the blunter
    mean-absolute-deviation option the roadmap offers, not a per-hour
    z-score. A day is flagged when that percent exceeds ``deviation_pct``
    (``config.BASELINE_DEVIATION_PCT`` by default).

    Below ``min_days`` (``config.BASELINE_MIN_DAYS`` by default) distinct
    calendar days of energylog history in the frame, per-hour variance is
    too noisy to trust and the honest result is ``"insufficient_history"``
    with nothing flagged — the same floor pattern
    ``battery_replace_projection`` and ``forecast_period_cost`` already use.

    Returns a dict with ``status`` (``"insufficient_history"`` or ``"ok"``),
    ``min_days``, ``n_days`` (distinct calendar days present, even below the
    floor), ``deviation_pct`` (the threshold used), and ``flagged`` (a
    DataFrame with columns ``date``, ``day_type``, ``deviation_pct`` — one
    row per flagged day, in date order).
    """
    if deviation_pct is None:
        deviation_pct = config.BASELINE_DEVIATION_PCT
    if min_days is None:
        min_days = config.BASELINE_MIN_DAYS
    out: dict = {
        "status": "insufficient_history", "min_days": min_days, "n_days": 0,
        "deviation_pct": deviation_pct,
        "flagged": pd.DataFrame(columns=_BASELINE_DEVIATION_COLUMNS),
    }
    if energy_df.empty or "power_w" not in energy_df.columns:
        return out
    s = energy_df.dropna(subset=["power_w"]).copy()
    if s.empty:
        return out
    s["date"] = s["ts"].dt.date
    dates = sorted(s["date"].unique())
    out["n_days"] = len(dates)
    if len(dates) < min_days:
        return out
    out["status"] = "ok"

    profiles = weekday_weekend_profiles(s)
    candidate_dates = dates[:-1]        # the trailing day is always excluded
    if not profiles or not candidate_dates:
        return out

    # "Complete" days: close enough to their peers' sample count that a
    # mid-history sampling gap does not masquerade as a behavior change.
    counts = s.groupby("date").size()
    typical = counts.loc[candidate_dates].median()
    complete_dates = [d for d in candidate_dates if counts[d] >= 0.9 * typical]

    records = []
    for d in complete_dates:
        day = s[s["date"] == d]
        day_type = "weekend" if pd.Timestamp(d).dayofweek >= 5 else "weekday"
        baseline = profiles.get(day_type)
        if baseline is None or baseline.empty:
            continue
        day_profile = day.groupby(day["ts"].dt.hour)["power_w"].mean()
        common = day_profile.index.intersection(baseline.index)
        if common.empty:
            continue
        baseline_mean = float(baseline.loc[common].mean())
        if baseline_mean == 0:
            continue
        mad_pct = float((day_profile.loc[common] - baseline.loc[common]).abs().mean()
                        / baseline_mean * 100)
        if mad_pct > deviation_pct:
            records.append({"date": d, "day_type": day_type, "deviation_pct": mad_pct})

    out["flagged"] = pd.DataFrame(records, columns=_BASELINE_DEVIATION_COLUMNS)
    return out


def _last_before_or_at(series: pd.Series, mask) -> float | None:
    """Most recent non-NaN value of series where mask holds, or None."""
    s = series[mask].dropna()
    return float(s.iloc[-1]) if not s.empty else None


def _min_in_window(series: pd.Series, mask) -> float | None:
    """Minimum non-NaN value of series where mask holds, or None."""
    s = series[mask].dropna()
    return float(s.min()) if not s.empty else None


_SELF_TEST_COLUMNS = ["ts", "dip_start", "dip_end", "capacity_drop_pct", "sag_v", "source"]


def _self_test_record(df: pd.DataFrame, ts, dip_start, dip_end, source: str) -> dict:
    """One self-test record's capacity drop and battery-voltage sag, measured
    from the DataLog window [dip_start, dip_end]. Never raises: a window with
    no usable Battery Capacity or Battery Voltage samples yields NaN for that
    field instead of crashing."""
    before = df["ts"] <= dip_start
    in_window = (df["ts"] >= dip_start) & (df["ts"] <= dip_end)
    resting_cap = (_last_before_or_at(df["Battery Capacity"], before)
                  if "Battery Capacity" in df.columns else None)
    min_cap = (_min_in_window(df["Battery Capacity"], in_window)
              if "Battery Capacity" in df.columns else None)
    capacity_drop_pct = (resting_cap - min_cap if resting_cap is not None and min_cap is not None
                         else float("nan"))
    resting_v = (_last_before_or_at(df["Battery Voltage"], before)
                if "Battery Voltage" in df.columns else None)
    min_v = (_min_in_window(df["Battery Voltage"], in_window)
            if "Battery Voltage" in df.columns else None)
    sag_v = resting_v - min_v if resting_v is not None and min_v is not None else float("nan")
    return {"ts": ts, "dip_start": dip_start, "dip_end": dip_end,
            "capacity_drop_pct": capacity_drop_pct, "sag_v": sag_v, "source": source}


def detect_self_tests(datalog_df: pd.DataFrame, dip_pct: float | None = None,
                      recovery_samples: int | None = None,
                      voltage_low: float | None = None,
                      voltage_high: float | None = None,
                      events: pd.DataFrame | None = None) -> pd.DataFrame:
    """Detect UPS self-tests: the periodic exercise that carves the Battery
    Charge card's sawtooth.

    Two routes, in order of preference:

    Event-based (authoritative): when ``events`` is given and
    ``pcss.eventlog.SELF_TEST_EVENT_IDS`` (roadmap item 18) is non-empty,
    every parsed event whose id is in that tuple anchors one self-test record
    at its own timestamp, and this replaces the shape heuristic below
    entirely for this call — the same way the EventLog's own outage spans
    replace the DataLog-inferred on-battery episodes once the log parses. The
    id is still unknown as of this writing; see that constant's own docstring
    in ``pcss/eventlog.py``.

    Shape-based (fallback): a battery-capacity drop of at least ``dip_pct``
    percentage points between two consecutive DataLog samples, followed by a
    recovery to within half that margin of the pre-dip level within
    ``recovery_samples`` samples after the dip begins, with every Line
    Voltage sample across the window inside [``voltage_low``,
    ``voltage_high``]. The voltage requirement is what tells a self-test
    apart from an on-battery episode (``detect_on_battery_episodes``), which
    is the same capacity-dip shape but with line voltage collapsed. Without a
    Line Voltage column there is no way to rule out an on-battery episode, so
    the shape route reports nothing (the event route does not need this
    column at all).

    Either route measures, from the DataLog window bracketing the test: the
    capacity drop, and the voltage sag — the resting Battery Voltage just
    before the dip minus the minimum Battery Voltage inside the window, the
    battery-health signal that complements the resting-voltage slope of
    ``battery_replace_projection``. A window with no usable Battery Voltage
    (or Battery Capacity) samples yields a record with that field NaN rather
    than raising.

    Returns one row per detected test: ``ts`` (the test's own timestamp --
    the event time, or the first sample showing the capacity drop),
    ``dip_start``/``dip_end`` (the DataLog window used for measurement),
    ``capacity_drop_pct``, ``sag_v``, and ``source`` (``"event"`` or
    ``"shape"``).
    """
    if dip_pct is None:
        dip_pct = config.SELFTEST_DIP_PCT
    if recovery_samples is None:
        recovery_samples = config.SELFTEST_RECOVERY_SAMPLES
    if voltage_low is None:
        voltage_low = config.VOLTAGE_NORMAL_LOW
    if voltage_high is None:
        voltage_high = config.VOLTAGE_NORMAL_HIGH

    if datalog_df.empty or "Battery Capacity" not in datalog_df.columns:
        return pd.DataFrame(columns=_SELF_TEST_COLUMNS)
    df = datalog_df.sort_values("ts").reset_index(drop=True)

    if events is not None and not events.empty and eventlog.SELF_TEST_EVENT_IDS:
        test_events = events[events["oid"].isin(eventlog.SELF_TEST_EVENT_IDS)]
        if not test_events.empty:
            records = []
            for evt_ts in test_events["ts"]:
                after = df[df["ts"] >= evt_ts]
                window_end = (after["ts"].iloc[min(recovery_samples, len(after) - 1)]
                             if not after.empty else evt_ts)
                records.append(_self_test_record(df, evt_ts, evt_ts, window_end, "event"))
            return pd.DataFrame(records, columns=_SELF_TEST_COLUMNS)

    if "Line Voltage" not in df.columns:
        return pd.DataFrame(columns=_SELF_TEST_COLUMNS)

    cap = df["Battery Capacity"]
    lv = df["Line Voltage"]
    n = len(df)
    records = []
    i = 1
    while i < n:
        prev_cap, cur_cap = cap.iloc[i - 1], cap.iloc[i]
        if pd.isna(prev_cap) or pd.isna(cur_cap) or (prev_cap - cur_cap) < dip_pct:
            i += 1
            continue
        window_end_idx = min(i + recovery_samples, n - 1)
        recover_level = prev_cap - dip_pct / 2.0
        recovered_idx = None
        for j in range(i + 1, window_end_idx + 1):
            v = cap.iloc[j]
            if pd.notna(v) and v >= recover_level:
                recovered_idx = j
                break
        if recovered_idx is None:
            i += 1
            continue
        window_lv = lv.iloc[i - 1:recovered_idx + 1].dropna()
        if not window_lv.empty and ((window_lv < voltage_low) | (window_lv > voltage_high)).any():
            i = recovered_idx + 1
            continue
        dip_start_ts = df["ts"].iloc[i - 1]
        dip_end_ts = df["ts"].iloc[recovered_idx]
        records.append(_self_test_record(df, df["ts"].iloc[i], dip_start_ts, dip_end_ts, "shape"))
        i = recovered_idx + 1
    return pd.DataFrame(records, columns=_SELF_TEST_COLUMNS)


def self_test_sag_trend(tests: pd.DataFrame | None, min_days: float | None = None) -> dict:
    """Fit the self-test voltage sag (``detect_self_tests``' ``sag_v``)
    against time — the load-based battery-health signal that complements
    ``battery_replace_projection``'s resting-voltage slope.

    States its confidence the same way that projection does: the median sag
    is reported whenever at least one test carries a usable sag value, but
    the trend itself — the slope — is fit only once the tests with a usable
    sag span at least ``min_days`` (``config.BATTERY_TREND_MIN_DAYS``, the
    same floor the replace-by projection uses; a slope over a handful of
    tests is noise). Below that floor the honest result is
    ``"insufficient_history"``, exactly like that projection's own status.

    Returns a dict with ``status`` (``"insufficient_history"`` or
    ``"trended"``), ``slope_v_per_day``, ``n_tests``, ``n_with_sag``,
    ``median_sag_v``, and ``span_days`` (the last four ``None``/0 when there
    is nothing to report).
    """
    if min_days is None:
        min_days = config.BATTERY_TREND_MIN_DAYS
    out: dict = {"status": "insufficient_history", "slope_v_per_day": None,
                 "n_tests": 0, "n_with_sag": 0, "median_sag_v": None, "span_days": None}
    if tests is None or tests.empty:
        return out
    out["n_tests"] = int(len(tests))
    if "sag_v" not in tests.columns:
        return out
    sagged = tests.dropna(subset=["sag_v"]).sort_values("ts")
    out["n_with_sag"] = int(len(sagged))
    if sagged.empty:
        return out
    out["median_sag_v"] = float(sagged["sag_v"].median())
    span_days = (sagged["ts"].iloc[-1] - sagged["ts"].iloc[0]).total_seconds() / 86400.0
    out["span_days"] = span_days
    if len(sagged) < 2 or span_days < min_days:
        return out
    days = (sagged["ts"] - sagged["ts"].iloc[0]).dt.total_seconds().to_numpy() / 86400.0
    slope, _intercept = np.polyfit(days, sagged["sag_v"].to_numpy(dtype=float), 1)
    out["slope_v_per_day"] = float(slope)
    out["status"] = "trended"
    return out


def latest_battery_replacement(annotations: pd.DataFrame | None,
                               as_of: pd.Timestamp | datetime) -> pd.Timestamp | None:
    """The newest `battery_replaced` entry in a user-owned annotations.csv
    (roadmap item 26) whose date is at or before `as_of` — typically the
    newest sample in the frame being fit. A `battery_replaced` entry dated
    later than the data being analyzed marks no boundary yet (a battery
    replacement planned for next month must not truncate today's fit down to
    nothing), so it is excluded rather than treated as the newest one.

    Reused as-is by `battery_replace_projection`'s fit segmentation below,
    and intended for roadmap item 16's runtime calibration to reuse the same
    boundary once it lands. Returns None when `annotations` is empty/None or
    holds no qualifying `battery_replaced` entry.
    """
    if annotations is None or annotations.empty:
        return None
    if "kind" not in annotations.columns or "date" not in annotations.columns:
        return None
    replaced = annotations.loc[annotations["kind"] == "battery_replaced", "date"]
    if replaced.empty:
        return None
    dates = pd.to_datetime(replaced)
    dates = dates[dates <= pd.Timestamp(as_of)]
    if dates.empty:
        return None
    return dates.max()


def battery_replace_projection(df: pd.DataFrame, threshold_v: float | None = None,
                               min_days: float | None = None,
                               annotations: pd.DataFrame | None = None,
                               self_tests: pd.DataFrame | None = None) -> dict:
    """Project when the resting battery voltage crosses the replace threshold.

    Fits a line to the rolling median of Battery Voltage (the median damps
    the sawtooth that capacity self-tests carve into the raw readings) and
    extrapolates to ``threshold_v``. Below ``min_days`` of history the honest
    answer is "not enough history": a slope over a few weeks is noise.

    ``annotations`` is a user-owned annotations.csv frame (roadmap item 26,
    ``pcss.loaders.load_annotations``). When it holds a ``battery_replaced``
    entry that is not dated later than the newest sample here
    (``latest_battery_replacement``), the fit runs only on samples at or
    after that date — a trend fit spanning a battery replacement would
    otherwise blend two different batteries' degradation into one
    meaningless slope — and the result carries the replaced-on date plus the
    battery's age in days. With no qualifying annotation, behavior is
    unchanged from before this feature existed.

    ``self_tests`` is a ``detect_self_tests``-shaped frame (roadmap item 18):
    when given, every sample whose timestamp falls inside a detected test's
    ``[dip_start, dip_end]`` window is dropped before the fit runs. The
    rolling-median fit below already damps the self-test sawtooth on its
    own, but excluding the windows outright is the roadmap's own "excluding
    them explicitly" — belt and braces, not a replacement for the median.

    Returns a dict with ``status`` ("insufficient_history", "stable", or
    "projected"), ``slope_v_per_day``, ``replace_date``, ``days_to_replace``,
    ``threshold_v``, ``battery_installed_on`` (date or None), and
    ``battery_age_days`` (float or None).
    """
    if threshold_v is None:
        threshold_v = config.BATTERY_REPLACE_VOLTAGE_V
    if min_days is None:
        min_days = config.BATTERY_TREND_MIN_DAYS
    out: dict = {"status": "insufficient_history", "slope_v_per_day": None,
                 "replace_date": None, "days_to_replace": None, "threshold_v": threshold_v,
                 "battery_installed_on": None, "battery_age_days": None}
    if df.empty or "Battery Voltage" not in df.columns:
        return out
    bv = df.dropna(subset=["Battery Voltage"])
    if bv.empty:
        return out
    boundary = latest_battery_replacement(annotations, bv["ts"].iloc[-1])
    if boundary is not None:
        bv = bv[bv["ts"] >= boundary].reset_index(drop=True)
        out["battery_installed_on"] = boundary.date()
        if not bv.empty:
            out["battery_age_days"] = float(
                (bv["ts"].iloc[-1] - boundary).total_seconds() / 86400)
    if (self_tests is not None and not self_tests.empty
            and {"dip_start", "dip_end"} <= set(self_tests.columns)):
        windows = self_tests.dropna(subset=["dip_start", "dip_end"])
        mask_out = pd.Series(False, index=bv.index)
        for dip_start, dip_end in windows[["dip_start", "dip_end"]].itertuples(index=False, name=None):
            mask_out |= (bv["ts"] >= dip_start) & (bv["ts"] <= dip_end)
        bv = bv[~mask_out].reset_index(drop=True)
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


_BILL_RECONCILE_COLUMNS = [
    "period", "ups_kwh", "billed_kwh", "share_pct", "ups_cost_tiered",
    "billed_amount_crc", "implied_rate_crc_per_kwh",
    "tariff_low", "tariff_high", "tariff_flat", "rate_tag", "partial",
]


def reconcile_bills(bills_df: pd.DataFrame, energy_summary: dict) -> tuple[pd.DataFrame, list[str]]:
    """Join a user-owned bills.csv (roadmap item 29) against the analyzer's
    own per-billing-period UPS kWh, to answer what share of a real,
    whole-house bill the UPS's own outlets account for — the UPS-metered
    share of the billed consumption, never a household total, since the UPS
    has no visibility into any circuit but its own.

    Each bill's period_start must equal, exactly, the billing-period start
    that `_billing_period_start` computes for `config.BILLING_CYCLE_START_DAY`.
    A mismatch is reported — naming the entry and the truly nearest valid
    boundary, whichever side of the entry it falls on (the preceding
    period's start or the following period's start, computed via
    `_billing_period_bounds`; a tie is reported as the earlier boundary) —
    and excluded rather than silently joined to the wrong period. A bill
    that aligns but has no matching row in
    `compute_energy_summary`'s "monthly" frame (no UPS energy data recorded
    for that period) is likewise reported and excluded. Neither case raises;
    the analyzer never crashes on this file.

    Returns (reconciled, warnings). reconciled has one row per successfully
    joined bill: period, ups_kwh, billed_kwh, share_pct (the UPS-metered
    share of the billed consumption, in percent), ups_cost_tiered (the
    analyzer's own Coopesantos-tiered cost for the period), billed_amount_crc,
    implied_rate_crc_per_kwh (amount_crc / kwh — the bill's own blended
    rate), the tariff's own effective rates for the period (tariff_low,
    tariff_high, tariff_flat, rate_tag — config.tariff_rates_for, item 17,
    reused rather than duplicated), and partial (whether the analyzer's own
    coverage of that period was incomplete, from compute_energy_summary).
    """
    empty = pd.DataFrame(columns=_BILL_RECONCILE_COLUMNS)
    warnings: list[str] = []
    if bills_df is None or bills_df.empty:
        return empty, warnings

    start_day = int(getattr(config, "BILLING_CYCLE_START_DAY", 1))
    monthly = energy_summary.get("monthly") if energy_summary else None
    if monthly is None or monthly.empty:
        for row in bills_df.itertuples(index=False):
            warnings.append(
                f"bill for {row.period_start}: the analyzer has no UPS "
                "energy data for this period; excluded from reconciliation")
        return empty, warnings

    monthly = monthly.set_index("month")
    records: list[dict] = []
    for row in bills_df.itertuples(index=False):
        period_start = row.period_start
        preceding = _billing_period_start(pd.Timestamp(period_start), start_day).date()
        if preceding != period_start:
            following = _billing_period_bounds(f"{preceding:%Y-%m-%d}", start_day)[1].date()
            nearest = (
                following if (following - period_start) < (period_start - preceding)
                else preceding
            )
            warnings.append(
                f"bill entry {period_start}: does not align to a "
                f"billing-period start (nearest valid boundary is "
                f"{nearest}); excluded from reconciliation")
            continue
        label = f"{period_start:%Y-%m}" if start_day <= 1 else f"{period_start:%Y-%m-%d}"
        if label not in monthly.index:
            warnings.append(
                f"bill for {period_start}: the analyzer has no UPS energy "
                "data for this period; excluded from reconciliation")
            continue
        m = monthly.loc[label]
        billed_kwh = float(row.kwh)
        ups_kwh = float(m["kwh"])
        low, high, _tier_limit, flat, rate_tag = config.tariff_rates_for(period_start)
        records.append({
            "period": label,
            "ups_kwh": ups_kwh,
            "billed_kwh": billed_kwh,
            "share_pct": (ups_kwh / billed_kwh * 100.0) if billed_kwh else float("nan"),
            "ups_cost_tiered": float(m["cost_tiered"]),
            "billed_amount_crc": float(row.amount_crc),
            "implied_rate_crc_per_kwh": (
                float(row.amount_crc) / billed_kwh if billed_kwh else float("nan")),
            "tariff_low": low,
            "tariff_high": high,
            "tariff_flat": flat,
            "rate_tag": rate_tag,
            "partial": bool(m["partial"]),
        })
    if not records:
        return empty, warnings
    return pd.DataFrame(records, columns=_BILL_RECONCILE_COLUMNS), warnings


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


def calibrate_runtime_curve(spans: pd.DataFrame, datalog_df: pd.DataFrame,
                            energy_df: pd.DataFrame,
                            annotations: pd.DataFrame | None = None,
                            min_episodes: int | None = None,
                            min_capacity_drop_pct: float = 1.0,
                            power_tolerance: pd.Timedelta | None = None) -> dict:
    """Fit a measured runtime curve from observed on-battery discharges.

    ``spans`` is an ``on_battery_spans``-shaped frame (start, end,
    duration_min, open) — the authoritative EventLog outage spans, not the
    DataLog-inferred fallback, so durations are exact to the millisecond.
    Each closed span (``open`` False, ``end`` known) becomes a candidate
    observation:

    - capacity consumed: the last DataLog Battery Capacity sample at or
      before the span's start, minus the first sample at or after its end.
      A span whose drop is below ``min_capacity_drop_pct`` (one percentage
      point by default) drained no measurable capacity — most real outages
      last only seconds and fall here — so it is discarded rather than
      counted as a zero-drain observation.
    - duration: the span's own start/end, used as-is (no cadence padding,
      unlike the DataLog-only episode inference).
    - power: the energylog sample nearest the span's midpoint
      (``merge_asof``, within ``power_tolerance``, 15 minutes by default —
      three energylog intervals). A span with no power sample within
      tolerance cannot be priced in watts and is discarded.

    The kept observations give one (power, drain-rate) pair each, where
    drain rate is the capacity drop divided by the duration in minutes.
    Below ``min_episodes`` usable observations
    (``config.CALIBRATION_MIN_EPISODES`` by default, mirroring the
    ``battery_trend_min_days`` honesty floor), the result reports
    ``"insufficient_evidence"`` and carries no curve. Otherwise a
    through-origin least-squares fit recovers ``k``, the drain rate in
    capacity percent per minute per watt, and a measured watts-to-minutes
    curve is evaluated at the configured ``[runtime_curve]`` watt points
    (excluding 0 W, where ``100 / (k * W)`` is undefined) as
    ``100 / (k * W)`` — the runtime a full battery would give at each load.

    ``annotations`` reuses ``latest_battery_replacement`` (roadmap item 26):
    when a ``battery_replaced`` entry is not dated later than the newest
    DataLog sample here, only spans starting at or after that boundary feed
    the fit, so a replaced battery's discharges do not contaminate the
    current one's curve — the same boundary ``battery_replace_projection``
    already applies to its own trend fit.

    Returns a dict with ``status`` (``"insufficient_evidence"`` or
    ``"calibrated"``), ``n_episodes`` (usable observations found, even below
    the floor), ``min_episodes``, ``k``, ``watts``, and ``minutes`` (the
    latter three ``None`` unless calibrated).
    """
    if min_episodes is None:
        min_episodes = config.CALIBRATION_MIN_EPISODES
    if power_tolerance is None:
        power_tolerance = pd.Timedelta(minutes=15)
    out: dict = {"status": "insufficient_evidence", "n_episodes": 0,
                 "min_episodes": min_episodes, "k": None, "watts": None, "minutes": None}

    if spans is None or spans.empty or datalog_df.empty or energy_df.empty:
        return out
    if "Battery Capacity" not in datalog_df.columns or "power_w" not in energy_df.columns:
        return out

    closed = spans if "open" not in spans.columns else spans[~spans["open"].fillna(False)]
    closed = closed.dropna(subset=["start", "end"])
    if closed.empty:
        return out

    dl = datalog_df.dropna(subset=["Battery Capacity"]).sort_values("ts")
    edf = energy_df.dropna(subset=["power_w"]).sort_values("ts")
    if dl.empty or edf.empty:
        return out

    boundary = latest_battery_replacement(annotations, dl["ts"].iloc[-1])
    if boundary is not None:
        closed = closed[closed["start"] >= boundary]
    if closed.empty:
        return out

    records = []
    for start, end in closed[["start", "end"]].itertuples(index=False, name=None):
        duration_min = (end - start).total_seconds() / 60.0
        if duration_min <= 0:
            continue
        before = dl[dl["ts"] <= start]
        after = dl[dl["ts"] >= end]
        if before.empty or after.empty:
            continue
        drop = float(before["Battery Capacity"].iloc[-1] - after["Battery Capacity"].iloc[0])
        if not (drop >= min_capacity_drop_pct):
            continue
        records.append({"drop": drop, "duration_min": duration_min,
                        "midpoint": start + (end - start) / 2})
    if not records:
        return out

    mids = pd.DataFrame(records).sort_values("midpoint").reset_index(drop=True)
    joined = pd.merge_asof(
        mids, edf[["ts", "power_w"]].rename(columns={"ts": "midpoint"}),
        on="midpoint", direction="nearest", tolerance=power_tolerance)
    joined = joined.dropna(subset=["power_w"])
    joined = joined[joined["power_w"] > 0]

    out["n_episodes"] = int(len(joined))
    if len(joined) < min_episodes:
        return out

    watts_obs = joined["power_w"].to_numpy(dtype=float)
    rates_obs = (joined["drop"] / joined["duration_min"]).to_numpy(dtype=float)
    k = float(np.sum(watts_obs * rates_obs) / np.sum(watts_obs ** 2))

    curve_w = np.asarray(config.RUNTIME_CURVE_W, dtype=float)
    mask = curve_w > 0
    watts_out = curve_w[mask]
    minutes_out = 100.0 / (k * watts_out)

    out["status"] = "calibrated"
    out["k"] = k
    out["watts"] = watts_out.tolist()
    out["minutes"] = minutes_out.tolist()
    return out


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
