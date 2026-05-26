"""
PowerChute Serial Shutdown UPS log analyzer.

Reads:
  - DataLog (TSV, sampled measurements, 20-min interval by default)
  - energylog/*.log (5-min interval, watts/load every 300s)
  - EventLog (binary Java serialized — only the size is reported)

Produces:
  - Console summary (sizes, samples, stats, anomalies, projections)
  - output/size_history.csv: persistent file-size snapshots appended each run
  - output/dashboard.html: 14-panel interactive Plotly dashboard

Open the dashboard with run_analyzer.bat or manually:
  start C:\\Users\\xpc\\Desktop\\python\\stateOfUPS\\output\\dashboard.html
"""
from __future__ import annotations

import csv
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ======================================================================
# CONFIG
# ======================================================================
PCSS_AGENT = Path(r"C:\Program Files\APC\PowerChute Serial Shutdown\agent")
DATALOG = PCSS_AGENT / "DataLog"
EVENTLOG = PCSS_AGENT / "EventLog"
ENERGYLOG_DIR = PCSS_AGENT / "energylog"
OUTPUT = Path(__file__).parent / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)
SIZE_HISTORY_CSV = OUTPUT / "size_history.csv"
DASHBOARD_HTML = OUTPUT / "dashboard.html"

# energylog uses seconds since 2010-01-01 LOCAL time (verified empirically:
# the first energylog timestamp aligns with the first DataLog timestamp
# within seconds).
EPOCH_2010 = datetime(2010, 1, 1)

# Coopesantos T-RE Residencial 2026 structural rates (CRC per kWh).
# First 200 kWh/month at LOW_RATE, anything beyond at HIGH_RATE.
COOPESANTOS_TIER_LIMIT_KWH = 200.0
COOPESANTOS_LOW_RATE = 78.17
COOPESANTOS_HIGH_RATE = 126.51
# What PCSS itself uses (single-rate mode — set to the high tier):
PCSS_FLAT_RATE = 126.51
# Costa Rica grid CO2 intensity (Low Carbon Power 2024 dataset):
CO2_KG_PER_KWH = 0.098

# Empirical UPS runtime curve for the BX2000M-LM with current build
# (from ups_profile memory). Used to estimate "if outage happens now,
# how long would the UPS last at this load level?"
RUNTIME_CURVE_W = np.array([0,   100, 150, 250, 500, 600, 800, 1200])
RUNTIME_CURVE_MIN = np.array([60, 40,  30,  15,  5,   3.5, 2,   0])

# Voltage envelope considered normal for 120V grids (NEC ±5%).
VOLTAGE_NORMAL_LOW = 114.0
VOLTAGE_NORMAL_HIGH = 126.0

# Sustained-high-load threshold for anomaly detection (matches PCSS umbral).
HIGH_LOAD_PCT = 80.0

# DataLog default sample interval (PCSS factory default).
DATALOG_EXPECTED_INTERVAL_MIN = 20.0


# ======================================================================
# Helpers
# ======================================================================
def fmt_bytes(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_crc(n: float) -> str:
    """Format Costa Rican colones."""
    if pd.isna(n):
        return "—"
    return f"CRC {n:,.2f}"


def parse_es_number(x):
    """Parse Spanish-locale number (1.234,56 -> 1234.56)."""
    if isinstance(x, str):
        x = x.strip()
        if x in ("N/A", "", "NaN", "null"):
            return np.nan
        x = x.replace(".", "").replace(",", ".")
    try:
        return float(x)
    except (ValueError, TypeError):
        return np.nan


def parse_pcss_number(x):
    """Parse PCSS energylog number (1234.567 dot decimal, may be 'null')."""
    if isinstance(x, str):
        x = x.strip()
        if x in ("null", "N/A", "", "NaN"):
            return np.nan
    try:
        return float(x)
    except (ValueError, TypeError):
        return np.nan


def ts_2010_to_dt(seconds: float) -> datetime:
    return EPOCH_2010 + timedelta(seconds=float(seconds))


# ======================================================================
# Loaders
# ======================================================================
def load_datalog() -> pd.DataFrame:
    if not DATALOG.exists():
        return pd.DataFrame()
    df = pd.read_csv(DATALOG, sep="\t", engine="python", encoding="utf-8", on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]
    df["ts"] = pd.to_datetime(df["Date and Time"], format="%m/%d/%Y %H:%M:%S", errors="coerce")
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    for c in [c for c in df.columns if c not in ("Date and Time", "ts")]:
        df[c] = df[c].apply(parse_es_number)
    return df


@dataclass
class EnergyLogMeta:
    month: str
    interval_sec: int
    model: str
    firmware: str
    max_load_w: float
    n_samples: int


def load_energylog() -> tuple[pd.DataFrame, list[EnergyLogMeta]]:
    """
    Parse all energylog/*.log files into a single DataFrame:
      ts, power_w, load_pct, real_w, source_file
    Plus a list of per-file metadata.
    """
    all_rows: list[dict] = []
    metas: list[EnergyLogMeta] = []
    if not ENERGYLOG_DIR.exists():
        return pd.DataFrame(), metas

    for f in sorted(ENERGYLOG_DIR.glob("*.log")):
        meta = {"month": f.stem, "interval_sec": 300, "model": "?", "firmware": "?", "max_load_w": 1400.0}
        n_samples = 0
        try:
            content = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                # parse metadata of form "# $key=value"
                if "$month=" in line:
                    meta["month"] = line.split("=", 1)[1]
                elif "$interval=" in line:
                    try:
                        meta["interval_sec"] = int(line.split("=", 1)[1])
                    except ValueError:
                        pass
                elif "$modelname=" in line:
                    meta["model"] = line.split("=", 1)[1]
                elif "$firmware=" in line:
                    meta["firmware"] = line.split("=", 1)[1]
                elif "$calculatedMaxLoad=" in line:
                    meta["max_load_w"] = parse_pcss_number(line.split("=", 1)[1])
                continue
            parts = line.split(";")
            if len(parts) < 4:
                continue
            try:
                raw_ts = float(parts[0])
            except ValueError:
                continue
            real_w = parse_pcss_number(parts[1])
            load_pct = parse_pcss_number(parts[2])
            calc_w = parse_pcss_number(parts[3])
            all_rows.append({
                "ts": ts_2010_to_dt(raw_ts),
                "power_w": calc_w,
                "load_pct": load_pct,
                "real_w": real_w,
                "source_file": f.name,
                "interval_sec": int(meta["interval_sec"]),
            })
            n_samples += 1
        metas.append(EnergyLogMeta(
            month=meta["month"],
            interval_sec=int(meta["interval_sec"]),
            model=meta["model"],
            firmware=meta["firmware"],
            max_load_w=float(meta["max_load_w"]),
            n_samples=n_samples,
        ))

    if not all_rows:
        return pd.DataFrame(), metas
    df = pd.DataFrame(all_rows).sort_values("ts").reset_index(drop=True)
    return df, metas


# ======================================================================
# Analysis
# ======================================================================
def datalog_stats(df: pd.DataFrame, file_size: int) -> dict:
    if df.empty:
        return {}
    span_seconds = max((df["ts"].iloc[-1] - df["ts"].iloc[0]).total_seconds(), 1)
    span_days = span_seconds / 86400
    n = len(df)
    daily_bytes = file_size / span_days if span_days > 0 else float("nan")
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


def detect_gaps(df: pd.DataFrame, expected_interval_min: float = DATALOG_EXPECTED_INTERVAL_MIN) -> pd.DataFrame:
    """Find DataLog timestamp gaps > 2× expected interval (likely PC off / PCSS down)."""
    if df.empty or len(df) < 2:
        return pd.DataFrame()
    deltas_min = df["ts"].diff().dt.total_seconds() / 60
    gap_mask = deltas_min > (expected_interval_min * 2)
    gaps = []
    for idx in df.index[gap_mask]:
        gaps.append({
            "from": df["ts"].iloc[idx - 1],
            "to": df["ts"].iloc[idx],
            "duration_min": float(deltas_min.iloc[idx]),
        })
    return pd.DataFrame(gaps)


def detect_voltage_anomalies(df: pd.DataFrame) -> pd.DataFrame:
    """Rows where Line Voltage falls outside the 114-126V envelope."""
    if df.empty or "Line Voltage" not in df.columns:
        return pd.DataFrame()
    mask = (df["Line Voltage"] < VOLTAGE_NORMAL_LOW) | (df["Line Voltage"] > VOLTAGE_NORMAL_HIGH)
    sub = df[mask & df["Line Voltage"].notna()][["ts", "Line Voltage"]]
    return sub.reset_index(drop=True)


def detect_high_load_episodes(edf: pd.DataFrame, threshold_pct: float = HIGH_LOAD_PCT,
                              min_duration_sec: int = 600) -> pd.DataFrame:
    """
    Find sustained high-load episodes from energylog (>= threshold for >= min_duration).
    Returns one row per episode: start, end, duration_min, peak_pct, peak_w.
    """
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
        duration_sec = (end - start).total_seconds()
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
    if "interval_sec" in s.columns:
        sample_interval = s["interval_sec"].astype(float)
    else:
        sample_interval = float(interval_sec)
    s["wh"] = s["power_w"] * (sample_interval / 3600.0)
    s["kwh"] = s["wh"] / 1000.0
    s["date"] = s["ts"].dt.date
    s["month"] = s["ts"].dt.to_period("M").astype(str)

    total_kwh = float(s["kwh"].sum())
    daily = s.groupby("date")["kwh"].sum().reset_index()
    monthly = s.groupby("month")["kwh"].sum().reset_index()
    monthly["cost_pcss"] = monthly["kwh"] * PCSS_FLAT_RATE
    monthly["cost_tiered"] = monthly["kwh"].apply(compute_tiered_cost)
    monthly["co2_kg"] = monthly["kwh"] * CO2_KG_PER_KWH

    return {
        "samples": s,
        "total_kwh": total_kwh,
        "total_cost_pcss": total_kwh * PCSS_FLAT_RATE,
        "total_cost_tiered": float(monthly["cost_tiered"].sum()),
        "total_co2_kg": total_kwh * CO2_KG_PER_KWH,
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
    if kwh <= COOPESANTOS_TIER_LIMIT_KWH:
        return kwh * COOPESANTOS_LOW_RATE
    return (COOPESANTOS_TIER_LIMIT_KWH * COOPESANTOS_LOW_RATE
            + (kwh - COOPESANTOS_TIER_LIMIT_KWH) * COOPESANTOS_HIGH_RATE)


def estimate_runtime(power_w: float) -> float:
    """Estimated UPS runtime (minutes) at given load. Empirical curve."""
    if pd.isna(power_w) or power_w <= 0:
        return float(RUNTIME_CURVE_MIN[0])
    return float(np.interp(power_w, RUNTIME_CURVE_W, RUNTIME_CURVE_MIN))


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


# ======================================================================
# Snapshot tracking (persistent file-size growth log)
# ======================================================================
def record_size_snapshot(sizes: dict) -> pd.DataFrame:
    now = datetime.now().replace(microsecond=0)
    row = {
        "timestamp": now.isoformat(),
        "datalog_bytes": sizes.get("DataLog", 0),
        "eventlog_bytes": sizes.get("EventLog (binary)", 0),
        "energylog_bytes": sizes.get("energylog/", 0),
        "total_bytes": sum(sizes.values()),
    }
    write_header = not SIZE_HISTORY_CSV.exists()
    with SIZE_HISTORY_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            w.writeheader()
        w.writerow(row)
    return pd.read_csv(SIZE_HISTORY_CSV, parse_dates=["timestamp"])


def history_summary(hist: pd.DataFrame) -> dict:
    if len(hist) < 2:
        return {}
    first, last = hist.iloc[0], hist.iloc[-1]
    elapsed = (last["timestamp"] - first["timestamp"]).total_seconds()
    if elapsed <= 0:
        return {}
    delta = last["total_bytes"] - first["total_bytes"]
    return {
        "snapshots": len(hist),
        "first_ts": first["timestamp"],
        "last_ts": last["timestamp"],
        "elapsed_seconds": elapsed,
        "first_total": int(first["total_bytes"]),
        "last_total": int(last["total_bytes"]),
        "delta_bytes": int(delta),
        "bytes_per_second": delta / elapsed,
        "bytes_per_minute": delta / elapsed * 60,
        "bytes_per_hour": delta / elapsed * 3600,
        "bytes_per_day": delta / elapsed * 86400,
    }


# ======================================================================
# Plotly dashboard
# ======================================================================
def add_timeseries(fig, df, col, row, col_idx, color, ylabel,
                   animated: list | None = None):
    """Add a time-series trace. If `animated` is a list, append
    (trace_index, x_array, y_array) so the caller can build animation frames."""
    if col not in df.columns:
        return
    series = df[col].dropna()
    if series.empty:
        return
    x = df.loc[series.index, "ts"].to_numpy()
    y = series.to_numpy()
    fig.add_trace(
        go.Scatter(
            x=x, y=y,
            mode="lines+markers",
            name=ylabel,
            line=dict(color=color, width=2),
            marker=dict(size=5),
            hovertemplate=f"<b>{ylabel}</b><br>%{{x|%Y-%m-%d %H:%M}}<br>%{{y}}<extra></extra>",
            legendgroup=ylabel,
        ),
        row=row, col=col_idx,
    )
    if animated is not None:
        animated.append((len(fig.data) - 1, x, y))


# ----------------------------------------------------------------------
# Animations
#
# Each animation is independent: its own frames (prefixed with a unique
# name like "ts0".."ts39"), its own slider, its own Play/Pause buttons.
# Plotly stores all frames in a single `fig.frames` tuple, but the
# controls are filtered by name so they don't interact with each other.
# ----------------------------------------------------------------------

def _replay_metadata(animated: list, n_frames: int = 30):
    """Pure metadata for a cumulative-reveal animation. JS will slice the
    live trace data on Play click — no go.Frame objects are created here,
    so fig.frames stays empty and the initial render shows full data."""
    if not animated:
        return None
    all_x = np.concatenate([np.asarray(a[1]) for a in animated])
    if all_x.size < 5:
        return None
    t = pd.to_datetime(all_x)
    t_min, t_max = t.min(), t.max()
    if t_min == t_max:
        return None
    cutoffs = pd.date_range(start=t_min, end=t_max, periods=n_frames)
    return {
        "type": "cumulative",
        "trace_indices": [int(a[0]) for a in animated],
        "cutoffs_ms": [int(c.value // 1_000_000) for c in cutoffs],
        "labels": [c.strftime("%m-%d %H:%M") for c in cutoffs],
        "n_frames": int(n_frames),
    }


def _runtime_metadata(trace_idx: int, energy_df: pd.DataFrame,
                      n_frames: int = 30):
    """Pure metadata for the runtime-marker animation."""
    if trace_idx is None or energy_df.empty:
        return None
    s = energy_df.dropna(subset=["power_w"])
    if s.empty:
        return None
    t = pd.to_datetime(s["ts"])
    t_min, t_max = t.min(), t.max()
    if t_min == t_max:
        return None
    cutoffs = pd.date_range(t_min, t_max, periods=n_frames)
    marker_data = []
    for cutoff in cutoffs:
        mask = t <= cutoff
        w = 0.0 if not mask.any() else float(s.loc[mask.to_numpy(), "power_w"].iloc[-1])
        marker_data.append({"w": w, "rt": float(estimate_runtime(w))})
    return {
        "type": "marker",
        "trace_idx": int(trace_idx),
        "marker_data": marker_data,
        "labels": [c.strftime("%m-%d %H:%M") for c in cutoffs],
        "n_frames": int(n_frames),
    }


def _heatmap_metadata(trace_idx: int, pivot: pd.DataFrame):
    """Pure metadata for the heatmap day-by-day reveal."""
    if trace_idx is None or pivot is None or pivot.empty:
        return None
    n = len(pivot)
    if n < 2:
        return None
    z_full = pivot.to_numpy(dtype=float)
    z_list = [[(None if np.isnan(v) else float(v)) for v in row] for row in z_full]
    return {
        "type": "heatmap_reveal",
        "trace_idx": int(trace_idx),
        "z_full": z_list,
        "labels": [d.isoformat() for d in pivot.index],
        "n_frames": int(n),
    }


def _build_custom_controls_html(animations: list[dict]) -> str:
    """One floating ▶/⏸ pair per animation, anchored to its own chart.
    Each pair calls Plotly.animate() with an explicit frame list — no shared
    queue, no auto-play, fully independent."""
    if not animations:
        return ""
    import json as _json
    # Layout constants — must match make_subplots(rows=7, vertical_spacing=0.080,
    # horizontal_spacing=0.10) and fig.update_layout(height=2400, margin=...).
    N_ROWS = 7
    V_SPACE = 0.080
    H_SPACE = 0.10
    HEIGHT = 2400
    MARGIN_T = 90
    MARGIN_B = 80
    MARGIN_L = 60
    MARGIN_R = 60
    panel_h = (1 - (N_ROWS - 1) * V_SPACE) / N_ROWS
    plot_h = HEIGHT - MARGIN_T - MARGIN_B

    def _row_top_px(row: int) -> float:
        # paper-y of the top of row R, converted to pixels-from-top-of-fig.
        # The `- 0.1` shifts the anchor DOWN into each panel (~0.1 of plot
        # paper height ≈ 223px). Placing the overlay below the subplot
        # title — inside the plot area — turned out to be the only
        # reliable way to keep alignment consistent across all rows;
        # anchoring above the panel collided with the figure's MARGIN_T
        # on row 1 and with the V_SPACE gap inconsistently on later rows.
        paper_top = 1 - (row - 1) * (panel_h + V_SPACE) - 0.1
        return MARGIN_T + (1 - paper_top) * plot_h

    def _col_right_paper(col: int) -> float:
        # paper-x of the right edge of the column (1 or 2)
        return 0.5 - H_SPACE / 2 if col == 1 else 1.0

    # Each animation gets pinned to one panel.
    POSITIONS = {
        "lv": (1, 1),   # Line Voltage
        "bv": (1, 2),   # Battery Voltage
        "ul": (2, 1),   # UPS Load
        "bc": (2, 2),   # Battery Capacity
        "pw": (3, 1),   # Power consumption
        "hm": (3, 2),   # Heatmap
        "kw": (4, 1),   # Cumulative kWh
        "rt": (5, 1),   # Runtime
    }

    overlays_html = []
    for a in animations:
        g = a["group"]
        if g not in POSITIONS:
            continue
        row, col = POSITIONS[g]
        top_px = (_row_top_px(row))
        rp = _col_right_paper(col)
        # CSS `right` from the right edge of the figure container. The
        # column-dependent inset (4*MARGIN_R for col=2, 2*MARGIN_R for
        # col=1) keeps each overlay tucked inside its own panel's data
        # area: col=2 needs a larger inset to clear the heatmap colorbar
        # on row 3, while col=1 only needs to clear the column gap.
        right_css = (f"calc((100% - {MARGIN_L + MARGIN_R}px) * {1 - rp:.4f}"
                     f" + {MARGIN_R * (4 if col == 2 else 2)}px)")
        first_label = a["labels"][0] if a["labels"] else ""
        overlays_html.append(f"""
<div class="anim-overlay" data-group="{g}" style="top: {top_px:.0f}px; right: {right_css};">
  <button class="anim-btn anim-play" data-group="{g}" type="button" title="Reproducir">▶</button>
  <button class="anim-btn anim-pause" data-group="{g}" type="button" title="Sin animación que pausar" disabled>⏸</button>
  <select class="anim-speed" data-group="{g}" title="Velocidad">
    <option value="0.25">0.25x</option>
    <option value="0.5">0.5x</option>
    <option value="1" selected>1x</option>
    <option value="2">2x</option>
    <option value="4">4x</option>
  </select>
  <select class="anim-easing" data-group="{g}" title="Curva (easing)">
    <option value="linear" selected>linear</option>
    <option value="smooth">smooth</option>
    <option value="ease-in">slow-in</option>
    <option value="ease-out">slow-out</option>
    <option value="ease-in-out">slow-in-out</option>
    <option value="cubic-in">cubic-in</option>
    <option value="cubic-out">cubic-out</option>
    <option value="cubic-in-out">cubic-in-out</option>
  </select>
  <span class="anim-time" data-group="{g}">{first_label}</span>
</div>""")

    overlays_block = "\n".join(overlays_html)
    # Pass the full per-animation metadata to JS — type, trace indices,
    # cutoffs/marker_data/z_full, labels, speed. JS uses this to rebuild
    # frames on each Play click.
    anim_data = {a["group"]: {k: v for k, v in a.items() if k != "title"} for a in animations}

    css = """
<style>
.anim-overlay { position: absolute; z-index: 1000; display: flex;
  align-items: center; gap: 4px; padding: 3px 6px;
  background: rgba(255,255,255,0.92); border: 1px solid #c8c8c8;
  border-radius: 5px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  font-family: Arial, Helvetica, sans-serif; pointer-events: auto; }
.anim-overlay .anim-btn { width: 26px; height: 22px; font-size: 11px;
  cursor: pointer; background: #fff; border: 1px solid #bbb;
  border-radius: 3px; color: #333; padding: 0; line-height: 1; }
.anim-overlay .anim-btn:hover { background: #eef; border-color: #88a; }
.anim-overlay .anim-btn:active { background: #ccd; }
.anim-overlay .anim-btn.is-active { background: #1f77b4; color: #fff; border-color: #1f77b4; }
.anim-overlay .anim-btn.is-resume { background: #2ca02c; color: #fff; border-color: #2ca02c; }
.anim-overlay .anim-btn:disabled { background: #f0f0f0; color: #bbb; border-color: #ddd;
  cursor: not-allowed; box-shadow: none; }
.anim-overlay .anim-btn:disabled:hover { background: #f0f0f0; border-color: #ddd; }
.anim-overlay .anim-speed,
.anim-overlay .anim-easing { height: 22px; font-size: 10px; padding: 0 2px;
  border: 1px solid #bbb; border-radius: 3px; background: #fff; color: #333;
  cursor: pointer; }
.anim-overlay .anim-time { font-size: 10px; color: #555;
  font-family: 'Consolas', 'Monaco', monospace; min-width: 78px; text-align: left; }
</style>"""

    js = f"""
<script>
(function() {{
  const ANIM_DATA = {_json.dumps(anim_data)};
  const STEPPERS = {{}};   // legacy alias kept for the test harness
  const RAF = {{}};        // group -> requestAnimationFrame id
  const SPEED = {{}};      // group -> playback rate (1.0 default)
  const EASING = {{}};     // group -> easing function name
  const ORIG = {{}};       // original trace data, captured once
  // Easing curves on [0,1]. Apply to the linear time `t` from rAF before
  // feeding into applyAtT — only the *visual rate* changes; the underlying
  // data and the total animation duration stay untouched. The user can
  // pick any of these per panel from the easing dropdown.
  const EASINGS = {{
    "linear":       t => t,
    "ease-in":      t => t * t,
    "ease-out":     t => 1 - (1 - t) * (1 - t),
    "ease-in-out":  t => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2,
    "cubic-in":     t => t * t * t,
    "cubic-out":    t => 1 - Math.pow(1 - t, 3),
    "cubic-in-out": t => t < 0.5 ? 4*t*t*t : 1 - Math.pow(-2*t + 2, 3) / 2,
    "smooth":       t => t * t * (3 - 2 * t),  // smoothstep, classic
  }};
  // Per-group state machine.
  //   STATE[g] : 'idle' | 'playing' | 'paused' | 'ended'
  //   SAVED_T[g] : last virtual t (0..1) when paused — used to resume
  // Used by the play/pause buttons to decide what action to take and
  // which buttons are enabled. The pause button doubles as resume:
  // playing → pause, paused → resume. When the animation is at the end
  // (t === 1) pause is disabled — you can't pause nothing, and per
  // user request we don't auto-restart.
  const STATE = {{}};
  const SAVED_T = {{}};
  let target = null;

  function gd() {{ return document.getElementsByClassName("plotly-graph-div")[0]; }}
  function toMs(x) {{
    // Plotly serializes pandas datetimes as ISO strings with NO timezone
    // suffix ("2026-04-27T23:18:52.000000"). Per ECMAScript spec, that
    // form is parsed as LOCAL time. But our Python-side cutoffs use
    // pd.Timestamp.value (naive treated as UTC). Force UTC interpretation
    // by appending 'Z' so both sides line up — otherwise we get a
    // tz-offset mismatch (6h in CR) that empties every cumulative trace.
    if (x == null) return NaN;
    if (typeof x === "number") return x;
    if (x instanceof Date) return x.getTime();
    if (typeof x === "string") {{
      const hasTz = /[zZ]$|[+-]\\d\\d:?\\d\\d$/.test(x);
      return Date.parse(hasTz ? x : x + "Z");
    }}
    return NaN;
  }}
  function setTime(g, idx) {{
    const lbl = document.querySelector('.anim-time[data-group="'+g+'"]');
    if (lbl) lbl.textContent = ANIM_DATA[g].labels[idx];
  }}
  function clearStepper(g) {{
    // Cancel both legacy interval and rAF — defensive in case of mixed state.
    if (STEPPERS[g]) {{ clearInterval(STEPPERS[g]); STEPPERS[g] = null; }}
    if (RAF[g]) {{ cancelAnimationFrame(RAF[g]); RAF[g] = null; }}
    const playBtn = document.querySelector('.anim-play[data-group="'+g+'"]');
    if (playBtn) playBtn.classList.remove("is-active");
  }}
  function refreshButtons(g) {{
    const playBtn = document.querySelector('.anim-play[data-group="'+g+'"]');
    const pauseBtn = document.querySelector('.anim-pause[data-group="'+g+'"]');
    if (!playBtn || !pauseBtn) return;
    const st = STATE[g] || "idle";
    // Pause button title text reflects what next click will do
    if (st === "paused") {{
      pauseBtn.disabled = false;
      pauseBtn.classList.add("is-resume");
      pauseBtn.title = "Continuar";
    }} else if (st === "playing") {{
      pauseBtn.disabled = false;
      pauseBtn.classList.remove("is-resume");
      pauseBtn.title = "Pausar";
    }} else {{
      // idle or ended → nothing to pause / resume
      pauseBtn.disabled = true;
      pauseBtn.classList.remove("is-resume");
      pauseBtn.title = "Sin animación que pausar";
    }}
    // Play is enabled except while actively playing (it would just
    // restart, which would feel jarring; let the user pause first).
    playBtn.disabled = (st === "playing");
    if (st === "playing") playBtn.classList.add("is-active");
    else playBtn.classList.remove("is-active");
  }}
  function setState(g, next) {{
    STATE[g] = next;
    refreshButtons(g);
  }}
  function safeArray(v) {{
    if (v == null) return [];
    if (Array.isArray(v)) return v.slice();
    if (typeof v === "string") return [v];
    if (typeof v.length === "number") return Array.from(v);
    return [];
  }}
  function readTraceArr(idx, key) {{
    // Plotly 3.x binary-encodes large numeric trace arrays as
    // {{dtype, bdata, _inputArray}} on gd.data[i]. The encoded blob has no
    // .length so safeArray() returns []. Three fallbacks, in order:
    //   1) plain Array (string datetimes, small numeric arrays)
    //   2) _inputArray on the blob (Plotly preserves the original)
    //   3) gd._fullData[i][key] (decoded Float64Array)
    const tr = target.data[idx];
    const raw = tr && tr[key];
    if (Array.isArray(raw)) return raw.slice();
    if (raw && raw._inputArray && Array.isArray(raw._inputArray)) {{
      return raw._inputArray.slice();
    }}
    const ft = target._fullData && target._fullData[idx];
    const fv = ft && ft[key];
    if (Array.isArray(fv)) return fv.slice();
    if (fv && typeof fv.length === "number") return Array.from(fv);
    return [];
  }}
  function stashOriginals() {{
    Object.entries(ANIM_DATA).forEach(([g, a]) => {{
      try {{
        if (a.type === "cumulative") {{
          ORIG[g] = a.trace_indices.map(idx => {{
            const xs = readTraceArr(idx, "x");
            const ys = readTraceArr(idx, "y");
            return {{ x: xs, y: ys, xms: xs.map(toMs) }};
          }});
        }} else if (a.type === "heatmap_reveal") {{
          ORIG[g] = {{ z: a.z_full }};
        }} else if (a.type === "marker") {{
          ORIG[g] = {{
            x: readTraceArr(a.trace_idx, "x"),
            y: readTraceArr(a.trace_idx, "y"),
            text: readTraceArr(a.trace_idx, "text"),
          }};
        }}
      }} catch (e) {{
        console.warn("stashOriginals failed for", g, e);
      }}
    }});
  }}
  function applyAtT(g, t) {{
    // Continuous interpolation in [0, 1]. Cumulative reveal computes a
    // cutoff in real time (not snapped to one of n_frames buckets), so
    // each rAF frame slides the line forward by sub-frame increments.
    // That's what makes the motion smooth — discrete setInterval ticks
    // were what gave it the "jumpy" feel.
    const a = ANIM_DATA[g];
    if (a.type === "cumulative") {{
      const tMin = a.cutoffs_ms[0];
      const tMax = a.cutoffs_ms[a.cutoffs_ms.length - 1];
      const cutoff = tMin + (tMax - tMin) * t;
      const xs = [], ys = [];
      for (let ti = 0; ti < a.trace_indices.length; ti++) {{
        const orig = ORIG[g][ti];
        const xms = orig.xms;
        let end = 0;
        for (let j = 0; j < xms.length; j++) {{
          if (xms[j] > cutoff) break;
          end = j + 1;
        }}
        if (end === 0 && orig.x.length > 0) end = 1;
        xs.push(orig.x.slice(0, end));
        ys.push(orig.y.slice(0, end));
      }}
      Plotly.restyle(target, {{x: xs, y: ys}}, a.trace_indices);
    }} else if (a.type === "heatmap_reveal") {{
      const i = Math.min(Math.floor(t * a.n_frames), a.n_frames - 1);
      const z = a.z_full.map((row, ridx) =>
        ridx <= i ? row.slice() : new Array(row.length).fill(null));
      Plotly.restyle(target, {{ z: [z] }}, [a.trace_idx]);
    }} else if (a.type === "marker") {{
      const i = Math.min(Math.floor(t * a.n_frames), a.n_frames - 1);
      const d = a.marker_data[i];
      Plotly.restyle(target, {{
        x: [[d.w]], y: [[d.rt]],
        text: [["  " + d.w.toFixed(0) + "W \\u2192 " + d.rt.toFixed(1) + "min"]]
      }}, [a.trace_idx]);
    }}
  }}
  function setLabelByT(g, t) {{
    const a = ANIM_DATA[g];
    const idx = Math.min(Math.floor(t * a.labels.length), a.labels.length - 1);
    setTime(g, idx);
  }}
  function axisNamesFor(traceIndices) {{
    const seen = new Set();
    const out = [];
    traceIndices.forEach(i => {{
      const tr = target.data[i] || {{}};
      const xa = (tr.xaxis || "x").replace("x", "xaxis");
      const ya = (tr.yaxis || "y").replace("y", "yaxis");
      [xa, ya].forEach(ax => {{
        if (!seen.has(ax)) {{ seen.add(ax); out.push(ax); }}
      }});
    }});
    return out;
  }}
  function lockAxes(g) {{
    // Snapshot the *currently displayed* axis bounds AND type, then
    // pin both. plotly.js #2546/#2823: restyling a date-typed trace
    // with x=[] makes Plotly switch the axis to a numeric default
    // (-1..6) and the saved date range is silently dropped. Pinning
    // .type='date' alongside .range keeps the axis date-typed even
    // when a transient empty frame goes through.
    const a = ANIM_DATA[g];
    const idxs = a.trace_indices || [a.trace_idx];
    const upd = {{}};
    axisNamesFor(idxs).forEach(ax => {{
      const cur = target._fullLayout && target._fullLayout[ax];
      if (cur && cur.range) {{
        upd[ax + ".range"] = cur.range.slice();
        upd[ax + ".autorange"] = false;
        if (cur.type) upd[ax + ".type"] = cur.type;
      }}
    }});
    if (Object.keys(upd).length) Plotly.relayout(target, upd);
  }}
  function restoreOriginals(g) {{
    // Note: axes are already locked (lockAxes ran on Play), so we
    // don't need to call autorange here — Plotly will keep the
    // pre-animation bounds, and the now-full data will fit inside them.
    const a = ANIM_DATA[g];
    if (!ORIG[g]) return;
    if (a.type === "cumulative") {{
      Plotly.restyle(target, {{
        x: ORIG[g].map(o => o.x),
        y: ORIG[g].map(o => o.y),
      }}, a.trace_indices);
    }} else if (a.type === "heatmap_reveal") {{
      Plotly.restyle(target, {{z: [ORIG[g].z]}}, [a.trace_idx]);
    }} else if (a.type === "marker") {{
      Plotly.restyle(target, {{
        x: [ORIG[g].x], y: [ORIG[g].y], text: [ORIG[g].text]
      }}, [a.trace_idx]);
    }}
  }}
  function setup() {{
    target = gd();
    if (!target || !window.Plotly) {{ setTimeout(setup, 100); return; }}
    if (!target.data || target.data.length === 0) {{ setTimeout(setup, 100); return; }}
    // Wait until Plotly has actually populated traces, then snapshot.
    stashOriginals();
    const parent = target.parentElement;
    parent.style.position = "relative";
    document.querySelectorAll('.anim-overlay').forEach(ov => {{
      if (ov.parentElement !== parent) parent.appendChild(ov);
    }});
    document.querySelectorAll('.anim-speed').forEach(sel => {{
      SPEED[sel.dataset.group] = parseFloat(sel.value) || 1;
      sel.addEventListener("change", () => {{
        SPEED[sel.dataset.group] = parseFloat(sel.value) || 1;
      }});
    }});
    document.querySelectorAll('.anim-easing').forEach(sel => {{
      // Easing changes apply to the NEXT play. Same reasoning as speed.
      EASING[sel.dataset.group] = sel.value || "linear";
      sel.addEventListener("change", () => {{
        EASING[sel.dataset.group] = sel.value || "linear";
      }});
    }});
    const GEN = {{}};   // per-group generation token to fence stale rAF ticks
    function startPlayback(g, fromT) {{
      const a = ANIM_DATA[g];
      clearStepper(g);
      lockAxes(g);
      const speed = SPEED[g] || 1;
      const totalMs = (a.n_frames * a.speed_ms) / speed;
      const t0 = Math.max(0, Math.min(1, fromT || 0));
      const tStart = performance.now() - t0 * totalMs;
      // Each startPlayback bumps the generation. In-flight tick closures
      // from a prior call check this and bail — a previously-queued tick
      // that survives cancelAnimationFrame can otherwise reschedule itself
      // and overwrite RAF[g], producing two parallel chains with stale
      // tStarts and ultimately negative elapsed times.
      const myGen = (GEN[g] || 0) + 1;
      GEN[g] = myGen;
      applyAtT(g, t0);
      setLabelByT(g, t0);
      setState(g, "playing");
      const easeFn = EASINGS[EASING[g] || "linear"] || EASINGS["linear"];
      const tick = (now) => {{
        if (GEN[g] !== myGen) return;       // superseded; let the new chain run
        const elapsed = Math.max(0, now - tStart);
        const tLinear = Math.min(elapsed / totalMs, 1);
        // Save linear t so resume picks up the actual elapsed point, not
        // the eased visual position. Visual easing applies to applyAtT.
        SAVED_T[g] = tLinear;
        const tEased = easeFn(tLinear);
        applyAtT(g, tEased);
        setLabelByT(g, tEased);
        if (tLinear < 1) {{
          RAF[g] = requestAnimationFrame(tick);
        }} else {{
          RAF[g] = null;
          restoreOriginals(g);
          setTime(g, ANIM_DATA[g].labels.length - 1);
          SAVED_T[g] = 1;
          setState(g, "ended");
        }}
      }};
      RAF[g] = requestAnimationFrame(tick);
    }}
    function pausePlayback(g) {{
      // Stop the timer at the current virtual t, leave panel where it is.
      // SAVED_T already updated by the most recent tick.
      clearStepper(g);
      setState(g, "paused");
    }}
    document.querySelectorAll('.anim-play').forEach(btn => {{
      btn.addEventListener("click", () => {{
        const g = btn.dataset.group;
        const st = STATE[g] || "idle";
        if (st === "playing") return;          // play is no-op while playing
        // From idle / paused / ended → start fresh from t=0.
        // (User explicitly asked: at end, do NOT auto-restart unless they
        // press Play again — which they're doing right now, so honour it.)
        startPlayback(g, 0);
      }});
    }});
    document.querySelectorAll('.anim-pause').forEach(btn => {{
      btn.addEventListener("click", () => {{
        const g = btn.dataset.group;
        const st = STATE[g] || "idle";
        if (st === "playing") {{
          pausePlayback(g);
        }} else if (st === "paused") {{
          // Resume from where we paused — explicitly NOT a restart.
          startPlayback(g, SAVED_T[g] || 0);
        }}
        // idle / ended → button is disabled so this branch shouldn't
        // fire, but no-op defensively.
      }});
    }});
    // Initialise every group as idle so the disabled state is correct
    // before the user touches anything.
    Object.keys(ANIM_DATA).forEach(g => {{
      STATE[g] = "idle";
      SAVED_T[g] = 0;
      refreshButtons(g);
    }});
  }}
  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", setup);
  }} else {{
    setup();
  }}
  // Test/debug surface — lets the E2E suite peek at what stashOriginals
  // captured. Removing this hook would not change behaviour for users.
  window.__animDebug = {{
    getOrig: (g) => ORIG[g],
    // Both naming conventions: legacy (stepper) for old tests, current
    // (raf) for the rAF engine. Either returns true while the group
    // animation is in progress.
    getStepperKeys: () => Object.keys(RAF).filter(k => RAF[k] != null)
      .concat(Object.keys(STEPPERS).filter(k => STEPPERS[k] != null)),
    isStepping: (g) => (RAF[g] != null) || (STEPPERS[g] != null),
    isPlaying: (g) => (RAF[g] != null) || (STEPPERS[g] != null),
    getSpeed: (g) => SPEED[g] || 1,
    getState: (g) => STATE[g] || "idle",
    getSavedT: (g) => SAVED_T[g] || 0,
  }};
}})();
</script>"""

    return css + overlays_block + js


def _inject_controls_into_html(html: str, animations: list[dict]) -> str:
    """Insert the custom controls just before </body>."""
    block = _build_custom_controls_html(animations)
    if not block:
        return html
    if "</body>" in html:
        return html.replace("</body>", block + "\n</body>", 1)
    return html + block


def _register_animation(*, group, title, speed_ms, build_data):
    """Compose the per-animation metadata dict consumed by the HTML+JS
    post-processor. Crucially we DO NOT touch fig.frames — keeping it empty
    means the initial render shows fig.data (full data) immediately, with
    zero animation interference. JS reconstructs frames on each Play click."""
    if not build_data:
        return None
    return {
        "group": group,
        "title": title,
        "speed_ms": speed_ms,
        **build_data,
    }


def build_dashboard(datalog_df: pd.DataFrame, energy_df: pd.DataFrame, hist: pd.DataFrame,
                    dl_stats: dict, hist_stats: dict, sizes: dict, energy_summary: dict,
                    stats_table: pd.DataFrame, gaps: pd.DataFrame,
                    voltage_anomalies: pd.DataFrame, high_load_episodes: pd.DataFrame,
                    crossval: dict):
    fig = make_subplots(
        rows=7, cols=2,
        subplot_titles=(
            "Line Voltage (V)",                              "Battery Voltage (V)",
            "UPS Load (%)",                                  "Battery Capacity (%)",
            "Power consumption (W) — energylog 5-min",       "Hour-of-day power heatmap (W)",
            "Cumulative kWh and cost",                       "Daily kWh consumption",
            "Estimated runtime if outage now (min)",         "Sample interval distribution (s)",
            "Log file size growth (KB)",                     "Projected DataLog size — current rate",
            "Per-metric statistics",                         "Latest readings + cost summary",
        ),
        specs=[
            [{"type": "scatter"}, {"type": "scatter"}],
            [{"type": "scatter"}, {"type": "scatter"}],
            [{"type": "scatter"}, {"type": "heatmap"}],
            [{"secondary_y": True}, {"type": "bar"}],
            [{"type": "scatter"}, {"type": "histogram"}],
            [{"type": "scatter"}, {"type": "scatter"}],
            [{"type": "table"}, {"type": "table"}],
        ],
        vertical_spacing=0.080,
        horizontal_spacing=0.10,
    )

    # One animated-trace bucket per panel — each gets its own play/pause
    # button. The voltage-anomaly markers ride along with Line Voltage
    # since they share that subplot.
    lv_animated: list = []
    bv_animated: list = []
    ul_animated: list = []
    bc_animated: list = []

    # ----- Row 1-2: DataLog time series -----
    if not datalog_df.empty:
        add_timeseries(fig, datalog_df, "Line Voltage", 1, 1, "#1f77b4", "Line Voltage", lv_animated)
        # Mark voltage anomalies on top
        if not voltage_anomalies.empty:
            fig.add_trace(
                go.Scatter(
                    x=voltage_anomalies["ts"], y=voltage_anomalies["Line Voltage"],
                    mode="markers", name="V anomaly",
                    marker=dict(color="red", size=11, symbol="x"),
                    hovertemplate="ANOMALY<br>%{x|%Y-%m-%d %H:%M}<br>%{y} V<extra></extra>",
                ),
                row=1, col=1,
            )
            lv_animated.append((len(fig.data) - 1,
                                voltage_anomalies["ts"].to_numpy(),
                                voltage_anomalies["Line Voltage"].to_numpy()))
        # Normal envelope shading
        fig.add_hrect(y0=VOLTAGE_NORMAL_LOW, y1=VOLTAGE_NORMAL_HIGH,
                      fillcolor="green", opacity=0.05, line_width=0, row=1, col=1)

        add_timeseries(fig, datalog_df, "Battery Voltage", 1, 2, "#2ca02c", "Battery Voltage", bv_animated)
        add_timeseries(fig, datalog_df, "UPS Load", 2, 1, "#d62728", "UPS Load", ul_animated)
        # 80% threshold line on UPS Load
        fig.add_hline(y=HIGH_LOAD_PCT, line_dash="dash", line_color="orange",
                      annotation_text=f"{HIGH_LOAD_PCT}% threshold",
                      annotation_position="top right", row=2, col=1)
        add_timeseries(fig, datalog_df, "Battery Capacity", 2, 2, "#bcbd22", "Battery %", bc_animated)

    # ----- Row 3 col 1: Power consumption from energylog -----
    if not energy_df.empty:
        s = energy_df.dropna(subset=["power_w"])
        fig.add_trace(
            go.Scatter(
                x=s["ts"], y=s["power_w"],
                mode="lines", name="Power (W)",
                line=dict(color="#e377c2", width=1.5),
                hovertemplate="<b>Power</b><br>%{x|%Y-%m-%d %H:%M}<br>%{y:.0f} W<extra></extra>",
            ),
            row=3, col=1,
        )
        # Track Power consumption SEPARATELY so it gets its own overlay /
        # play button. Folding it into `animated` would re-couple panel 5
        # with the datalog replay (and force the user to wait through the
        # 5-min energylog density when they only want to see the volts/load).
        power_animated: list = [(len(fig.data) - 1, s["ts"].to_numpy(), s["power_w"].to_numpy())]
    else:
        power_animated = []

    # ----- Row 3 col 2: Hour-of-day power heatmap -----
    heatmap_pivot = None
    heatmap_idx = None
    if not energy_df.empty and "power_w" in energy_df.columns:
        hm = energy_df.dropna(subset=["power_w"]).copy()
        if not hm.empty:
            hm["hour"] = hm["ts"].dt.hour
            hm["date"] = hm["ts"].dt.date
            pivot = hm.pivot_table(index="date", columns="hour", values="power_w", aggfunc="mean")
            # Make sure all 24 hours are present
            for h in range(24):
                if h not in pivot.columns:
                    pivot[h] = np.nan
            pivot = pivot.reindex(sorted(pivot.columns), axis=1)
            heatmap_pivot = pivot
            fig.add_trace(
                go.Heatmap(
                    z=pivot.to_numpy(),
                    x=[f"{h:02d}" for h in pivot.columns],
                    y=[d.isoformat() for d in pivot.index],
                    colorscale="Inferno", colorbar=dict(title="W", x=1.02, len=0.12, y=0.65),
                    hovertemplate="Day %{y}<br>Hour %{x}h<br>%{z:.0f} W<extra></extra>",
                ),
                row=3, col=2,
            )
            heatmap_idx = len(fig.data) - 1

    # ----- Row 4 col 1: Cumulative kWh + cost (dual-axis) -----
    kwh_animated: list = []
    if energy_summary and "samples" in energy_summary:
        s = energy_summary["samples"].copy().sort_values("ts")
        s["cum_kwh"] = s["kwh"].cumsum()
        s["cum_cost_pcss"] = s["cum_kwh"] * PCSS_FLAT_RATE
        s_ts = s["ts"].to_numpy()
        fig.add_trace(
            go.Scatter(
                x=s_ts, y=s["cum_kwh"].to_numpy(),
                mode="lines", name="Cumulative kWh",
                line=dict(color="#17becf", width=2),
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>%{y:.4f} kWh<extra></extra>",
            ),
            row=4, col=1, secondary_y=False,
        )
        kwh_animated.append((len(fig.data) - 1, s_ts, s["cum_kwh"].to_numpy()))
        fig.add_trace(
            go.Scatter(
                x=s_ts, y=s["cum_cost_pcss"].to_numpy(),
                mode="lines", name="Cumulative cost (PCSS flat)",
                line=dict(color="#ff7f0e", width=2, dash="dot"),
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>CRC %{y:,.2f}<extra></extra>",
            ),
            row=4, col=1, secondary_y=True,
        )
        kwh_animated.append((len(fig.data) - 1, s_ts, s["cum_cost_pcss"].to_numpy()))

    # ----- Row 4 col 2: Daily kWh bar chart -----
    if energy_summary and "daily" in energy_summary and not energy_summary["daily"].empty:
        d = energy_summary["daily"]
        fig.add_trace(
            go.Bar(
                x=[str(x) for x in d["date"]], y=d["kwh"],
                marker_color="#9467bd",
                hovertemplate="%{x}<br>%{y:.4f} kWh<extra></extra>",
                name="Daily kWh",
            ),
            row=4, col=2,
        )

    # ----- Row 5 col 1: Runtime estimate curve + current point -----
    w_axis = np.linspace(0, 1300, 200)
    rt_axis = [estimate_runtime(w) for w in w_axis]
    fig.add_trace(
        go.Scatter(
            x=w_axis, y=rt_axis, mode="lines",
            name="Runtime curve",
            line=dict(color="#8c564b", width=2),
            hovertemplate="%{x:.0f} W → %{y:.1f} min<extra></extra>",
        ),
        row=5, col=1,
    )
    runtime_marker_idx = None
    if not energy_df.empty and energy_df["power_w"].notna().any():
        latest_w = float(energy_df["power_w"].dropna().iloc[-1])
        latest_rt = estimate_runtime(latest_w)
        fig.add_trace(
            go.Scatter(
                x=[latest_w], y=[latest_rt],
                mode="markers+text", name="Latest reading",
                marker=dict(color="red", size=14, symbol="star"),
                text=[f"  Now: {latest_w:.0f}W → {latest_rt:.1f}min"],
                textposition="middle right",
                hovertemplate=f"Now: {latest_w:.0f} W<br>Est. runtime: {latest_rt:.1f} min<extra></extra>",
            ),
            row=5, col=1,
        )
        runtime_marker_idx = len(fig.data) - 1

    # ----- Row 5 col 2: Sample interval histogram -----
    if not datalog_df.empty:
        deltas = datalog_df["ts"].diff().dt.total_seconds().dropna()
        if not deltas.empty:
            fig.add_trace(
                go.Histogram(x=deltas, nbinsx=30, marker_color="#7f7f7f", name="Intervals"),
                row=5, col=2,
            )

    # ----- Row 6 col 1: Size history multi-line -----
    if not hist.empty:
        for col, label, color in [
            ("datalog_bytes", "DataLog", "#1f77b4"),
            ("eventlog_bytes", "EventLog", "#d62728"),
            ("energylog_bytes", "energylog/", "#2ca02c"),
            ("total_bytes", "TOTAL", "#9467bd"),
        ]:
            if col in hist.columns:
                fig.add_trace(
                    go.Scatter(
                        x=hist["timestamp"], y=hist[col] / 1024,
                        mode="lines+markers", name=label,
                        line=dict(color=color, width=2), marker=dict(size=7),
                        hovertemplate=f"<b>{label}</b><br>%{{x|%Y-%m-%d %H:%M}}<br>%{{y:.2f}} KB<extra></extra>",
                    ),
                    row=6, col=1,
                )

    # ----- Row 6 col 2: Projection -----
    if dl_stats and dl_stats.get("daily_bytes"):
        days = np.arange(0, 366)
        kb = (dl_stats["daily_bytes"] * days) / 1024
        fig.add_trace(
            go.Scatter(
                x=days, y=kb, mode="lines",
                line=dict(color="#ff7f0e", width=2),
                name="Projected DataLog size",
                hovertemplate="Day %{x}<br>%{y:.1f} KB<extra></extra>",
            ),
            row=6, col=2,
        )
        for d, lbl in [(30, "1 mo"), (90, "3 mo"), (180, "6 mo"), (365, "1 yr")]:
            v = (dl_stats["daily_bytes"] * d) / 1024
            fig.add_annotation(
                x=d, y=v, xref=f"x{12}", yref=f"y{12}",
                text=f"{lbl}<br>{v:.0f} KB",
                showarrow=True, arrowhead=2, ax=20, ay=-25,
                font=dict(size=9),
            )

    # ----- Row 7 col 1: Stats table -----
    if not stats_table.empty:
        fig.add_trace(
            go.Table(
                header=dict(values=list(stats_table.columns),
                            fill_color="#444", font=dict(color="white"), align="left"),
                cells=dict(values=[stats_table[c].tolist() for c in stats_table.columns],
                           align="left", height=22),
            ),
            row=7, col=1,
        )

    # ----- Row 7 col 2: Latest readings + cost summary -----
    rows = []
    if not datalog_df.empty:
        latest = datalog_df.iloc[-1]
        rows.append(["Last sample", str(latest["ts"])])
        for c in ["Line Voltage", "Battery Voltage", "UPS Load", "Battery Capacity",
                  "Output Frequency", "Input Frequency"]:
            if c in datalog_df.columns:
                v = latest[c]
                if not (isinstance(v, float) and np.isnan(v)):
                    rows.append([c, f"{v}"])
    if energy_summary:
        rows.append(["", ""])
        rows.append(["═ ENERGY ═", ""])
        rows.append(["Total kWh", f"{energy_summary['total_kwh']:.4f}"])
        rows.append(["Cost (PCSS flat ₡126.51)", fmt_crc(energy_summary["total_cost_pcss"])])
        rows.append(["Cost (Coopesantos tiered)", fmt_crc(energy_summary["total_cost_tiered"])])
        rows.append(["CO₂ emitted", f"{energy_summary['total_co2_kg']:.4f} kg"])
        rows.append(["Span", f"{energy_summary['first']} → {energy_summary['last']}"])
        rows.append(["Samples (5-min)", f"{energy_summary['n_samples']}"])
    if crossval:
        rows.append(["", ""])
        rows.append(["═ CROSS-VAL ═", ""])
        rows.append(["DataLog vs energylog (load %)", f"MAE {crossval['mean_abs_error_pct']:.2f}%, n={crossval['n_pairs']}"])
    rows.append(["", ""])
    rows.append(["═ FILES ═", ""])
    rows.append(["DataLog", fmt_bytes(sizes.get("DataLog", 0))])
    rows.append(["EventLog", fmt_bytes(sizes.get("EventLog (binary)", 0))])
    rows.append(["energylog/", fmt_bytes(sizes.get("energylog/", 0))])
    rows.append(["TOTAL", fmt_bytes(sum(sizes.values()))])
    if hist_stats:
        rows.append(["", ""])
        rows.append(["═ GROWTH ═", ""])
        rows.append(["Per hour", fmt_bytes(hist_stats["bytes_per_hour"])])
        rows.append(["Per day", fmt_bytes(hist_stats["bytes_per_day"])])
        rows.append(["Snapshots", str(hist_stats["snapshots"])])
    rows.append(["", ""])
    rows.append(["═ ANOMALIES ═", ""])
    rows.append(["Voltage out-of-range", f"{len(voltage_anomalies)} samples"])
    rows.append(["Sustained high-load episodes", f"{len(high_load_episodes)}"])
    rows.append(["DataLog gaps (>40 min)", f"{len(gaps)}"])

    fig.add_trace(
        go.Table(
            header=dict(values=["Metric", "Value"],
                        fill_color="#444", font=dict(color="white"), align="left"),
            cells=dict(values=list(zip(*rows)) if rows else [[], []],
                       align="left", height=22),
        ),
        row=7, col=2,
    )

    # Axes labels
    fig.update_xaxes(title_text="Time", row=1, col=1); fig.update_yaxes(title_text="Volts", row=1, col=1)
    fig.update_xaxes(title_text="Time", row=1, col=2); fig.update_yaxes(title_text="Volts", row=1, col=2)
    fig.update_xaxes(title_text="Time", row=2, col=1); fig.update_yaxes(title_text="%", row=2, col=1)
    fig.update_xaxes(title_text="Time", row=2, col=2); fig.update_yaxes(title_text="%", row=2, col=2)
    fig.update_xaxes(title_text="Time", row=3, col=1); fig.update_yaxes(title_text="W", row=3, col=1)
    fig.update_xaxes(title_text="Hour", row=3, col=2); fig.update_yaxes(title_text="Date", row=3, col=2)
    fig.update_xaxes(title_text="Time", row=4, col=1)
    fig.update_yaxes(title_text="kWh (cum.)", row=4, col=1, secondary_y=False)
    fig.update_yaxes(title_text="CRC (cum.)", row=4, col=1, secondary_y=True)
    fig.update_xaxes(title_text="Date", row=4, col=2); fig.update_yaxes(title_text="kWh", row=4, col=2)
    fig.update_xaxes(title_text="Power (W)", row=5, col=1); fig.update_yaxes(title_text="Runtime (min)", row=5, col=1)
    fig.update_xaxes(title_text="Seconds", row=5, col=2); fig.update_yaxes(title_text="Count", row=5, col=2)
    fig.update_xaxes(title_text="Snapshot time", row=6, col=1); fig.update_yaxes(title_text="KB", row=6, col=1)
    fig.update_xaxes(title_text="Days from today", row=6, col=2); fig.update_yaxes(title_text="KB", row=6, col=2)

    fig.update_layout(
        title=dict(
            text=f"<b>PowerChute UPS Dashboard</b> — {datetime.now():%Y-%m-%d %H:%M:%S}",
            x=0.5, xanchor="center", font=dict(size=22),
        ),
        height=2400,
        margin=dict(b=80, t=90, l=60, r=60),
        showlegend=True,
        template="plotly_white",
        hovermode="closest",
        legend=dict(orientation="h", y=1.04, x=0.5,
                    xanchor="center", yanchor="bottom"),
    )

    # ------------------------------------------------------------------
    # We collect *metadata only* — fig.frames is left empty so Plotly's
    # animation engine has nothing to play through on initial render. The
    # injected JS rebuilds frames lazily, only when the user clicks Play.
    # ------------------------------------------------------------------
    animations: list[dict] = []
    for meta in [
        _register_animation(group="lv", speed_ms=40,
            title="Line Voltage replay",
            build_data=_replay_metadata(lv_animated, n_frames=60)),
        _register_animation(group="bv", speed_ms=40,
            title="Battery Voltage replay",
            build_data=_replay_metadata(bv_animated, n_frames=60)),
        _register_animation(group="ul", speed_ms=40,
            title="UPS Load replay",
            build_data=_replay_metadata(ul_animated, n_frames=60)),
        _register_animation(group="bc", speed_ms=40,
            title="Battery Capacity replay",
            build_data=_replay_metadata(bc_animated, n_frames=60)),
        _register_animation(group="pw", speed_ms=40,
            title="Power consumption replay",
            build_data=_replay_metadata(power_animated, n_frames=60)),
        _register_animation(group="hm", speed_ms=180,
            title="Heatmap flipbook",
            build_data=_heatmap_metadata(heatmap_idx, heatmap_pivot)),
        _register_animation(group="kw", speed_ms=40,
            title="kWh acumulado",
            build_data=_replay_metadata(kwh_animated, n_frames=60)),
        _register_animation(group="rt", speed_ms=40,
            title="Punto operativo",
            build_data=_runtime_metadata(runtime_marker_idx, energy_df, n_frames=60)),
    ]:
        if meta:
            animations.append(meta)

    return fig, animations


# ======================================================================
# Console reporter
# ======================================================================
def banner(s: str):
    print()
    print("=" * 72)
    print(s)
    print("=" * 72)


def main():
    sizes = {
        "DataLog": DATALOG.stat().st_size if DATALOG.exists() else 0,
        "EventLog (binary)": EVENTLOG.stat().st_size if EVENTLOG.exists() else 0,
        "energylog/": sum(f.stat().st_size for f in ENERGYLOG_DIR.glob("*")) if ENERGYLOG_DIR.exists() else 0,
    }

    banner("PCSS LOG FILES — CURRENT SIZES")
    for name, sz in sizes.items():
        print(f"  {name:25s} {fmt_bytes(sz):>10s}  ({sz:,} bytes)")
    print(f"  {'TOTAL':25s} {fmt_bytes(sum(sizes.values())):>10s}")

    hist = record_size_snapshot(sizes)
    hist_stats = history_summary(hist)

    banner("SIZE-HISTORY SNAPSHOTS")
    print(f"  History file: {SIZE_HISTORY_CSV}")
    print(f"  Snapshots so far: {len(hist)}")
    if hist_stats:
        print(f"  First snapshot : {hist_stats['first_ts']}")
        print(f"  Last snapshot  : {hist_stats['last_ts']}")
        print(f"  Total grew     : {fmt_bytes(hist_stats['delta_bytes'])} "
              f"({hist_stats['first_total']:,} -> {hist_stats['last_total']:,} bytes)")
        print(f"  Rate           : {fmt_bytes(hist_stats['bytes_per_minute'])}/min, "
              f"{fmt_bytes(hist_stats['bytes_per_hour'])}/hour, "
              f"{fmt_bytes(hist_stats['bytes_per_day'])}/day")
    else:
        print("  Run again later to see growth between snapshots.")

    datalog_df = load_datalog()
    dl_stats = datalog_stats(datalog_df, sizes["DataLog"])

    banner("DATALOG SUMMARY")
    if datalog_df.empty:
        print("  No DataLog rows yet.")
    else:
        print(f"  First sample   : {dl_stats['first']}")
        print(f"  Last sample    : {dl_stats['last']}")
        print(f"  Entries        : {dl_stats['n_entries']}")
        print(f"  Median interval: {dl_stats['median_interval_sec']:.0f} s "
              f"(~{dl_stats['median_interval_sec']/60:.1f} min)")
        print(f"  Bytes/entry    : {dl_stats['bytes_per_entry']:.1f}")
        print()
        print(f"  Projected disk usage (over {dl_stats['span_days']:.2f} days of data):")
        print(f"    Per minute  : {fmt_bytes(dl_stats['minute_bytes'])}")
        print(f"    Per day     : {fmt_bytes(dl_stats['daily_bytes'])}")
        print(f"    Per month   : {fmt_bytes(dl_stats['monthly_bytes'])}")
        print(f"    Per year    : {fmt_bytes(dl_stats['yearly_bytes'])}")

    energy_df, energy_metas = load_energylog()
    energy_summary = compute_energy_summary(energy_df) if not energy_df.empty else {}

    banner("ENERGY LOG SUMMARY")
    if energy_df.empty:
        print("  energylog/ empty or unparseable.")
    else:
        for m in energy_metas:
            print(f"  {m.month}: {m.n_samples:>5d} samples, interval={m.interval_sec}s, max_load={m.max_load_w:.0f}W")
        print()
        print(f"  Total samples       : {energy_summary['n_samples']}")
        print(f"  Span                : {energy_summary['first']} -> {energy_summary['last']}")
        print(f"  Total energy        : {energy_summary['total_kwh']:.4f} kWh")
        print(f"  Cost @ PCSS flat    : CRC {energy_summary['total_cost_pcss']:,.2f}")
        print(f"  Cost @ Coop. tiered : CRC {energy_summary['total_cost_tiered']:,.2f}")
        print(f"  CO2 emitted         : {energy_summary['total_co2_kg']:.4f} kg")
        if energy_summary["monthly"] is not None and not energy_summary["monthly"].empty:
            print()
            print("  Monthly breakdown:")
            for _, r in energy_summary["monthly"].iterrows():
                print(f"    {r['month']}: {r['kwh']:>9.4f} kWh   "
                      f"PCSS=CRC {r['cost_pcss']:>10,.2f}   "
                      f"Tiered=CRC {r['cost_tiered']:>10,.2f}   "
                      f"CO2={r['co2_kg']:>7.4f} kg")

    banner("ANOMALIES & EVENTS")
    voltage_anomalies = detect_voltage_anomalies(datalog_df)
    print(f"  Voltage out of {VOLTAGE_NORMAL_LOW}-{VOLTAGE_NORMAL_HIGH}V envelope: "
          f"{len(voltage_anomalies)} samples")
    if not voltage_anomalies.empty:
        for _, r in voltage_anomalies.head(5).iterrows():
            print(f"    {r['ts']}  {r['Line Voltage']} V")
        if len(voltage_anomalies) > 5:
            print(f"    ... ({len(voltage_anomalies)-5} more)")

    high_load = detect_high_load_episodes(energy_df) if not energy_df.empty else pd.DataFrame()
    print(f"  Sustained high-load episodes (>={HIGH_LOAD_PCT}%, >=10min): {len(high_load)}")
    if not high_load.empty:
        for _, r in high_load.head(5).iterrows():
            print(f"    {r['start']} -> {r['end']}  {r['duration_min']:.1f}min  peak {r['peak_pct']:.0f}% / {r['peak_w']:.0f}W")
        if len(high_load) > 5:
            print(f"    ... ({len(high_load)-5} more)")

    gaps = detect_gaps(datalog_df)
    print(f"  DataLog gaps (>{DATALOG_EXPECTED_INTERVAL_MIN*2:.0f} min): {len(gaps)}")
    if not gaps.empty:
        for _, r in gaps.head(5).iterrows():
            print(f"    {r['from']} -> {r['to']}  ({r['duration_min']:.1f} min)")

    banner("CROSS-VALIDATION (DataLog vs energylog)")
    crossval = cross_validate_load(datalog_df, energy_df) if not energy_df.empty else {}
    if crossval:
        print(f"  Paired samples       : {crossval['n_pairs']}")
        print(f"  DataLog mean load    : {crossval['datalog_mean_pct']:.2f}%")
        print(f"  energylog mean load  : {crossval['energylog_mean_pct']:.2f}%")
        print(f"  Mean abs error       : {crossval['mean_abs_error_pct']:.2f}%")
        print(f"  Max abs error        : {crossval['max_abs_error_pct']:.2f}%")
    else:
        print("  Not enough data to cross-validate.")

    banner("RUNTIME ESTIMATE")
    if not energy_df.empty and energy_df["power_w"].notna().any():
        latest_w = float(energy_df["power_w"].dropna().iloc[-1])
        latest_rt = estimate_runtime(latest_w)
        print(f"  Latest power reading : {latest_w:.0f} W")
        print(f"  Estimated runtime    : {latest_rt:.1f} min if outage happens now")
        for label, w in [("Idle", 150), ("Moderate", 250), ("Gaming", 500), ("Peak", 600)]:
            print(f"  At {label:<10s} ({w} W): {estimate_runtime(w):.1f} min")
    else:
        print("  No power data yet.")

    stats_table = compute_stats_summary(datalog_df)

    banner("PER-METRIC STATISTICS (DataLog)")
    if stats_table.empty:
        print("  No usable numeric columns.")
    else:
        print(stats_table.to_string(index=False))

    banner("DASHBOARD")
    fig, animations = build_dashboard(
        datalog_df, energy_df, hist, dl_stats, hist_stats, sizes, energy_summary,
        stats_table, gaps, voltage_anomalies, high_load, crossval,
    )
    html = fig.to_html(include_plotlyjs="cdn", full_html=True)
    html = _inject_controls_into_html(html, animations)
    DASHBOARD_HTML.write_text(html, encoding="utf-8")
    print(f"  Wrote {DASHBOARD_HTML}")
    print("  Opening in browser...")
    try:
        webbrowser.open(DASHBOARD_HTML.as_uri())
    except Exception as e:
        print(f"  (couldn't auto-open: {e})")


if __name__ == "__main__":
    main()
