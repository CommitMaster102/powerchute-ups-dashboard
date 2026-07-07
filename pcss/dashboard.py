"""Dashboard page construction.

Python computes, the browser renders: ``build_dashboard()`` assembles one JSON
payload (per-panel series, gap spans, KPI cards, health state, theme palette)
plus the design shell HTML, and embeds ``pcss/charts.js`` — a dependency-free
SVG chart engine ported from the Claude Design mockup — by substituting the
single ``__DASH_DATA__`` token. The page is fully offline: no chart library,
no CDN.

Timestamps cross the Python/JS boundary as epoch-milliseconds integers where
the log's naive local wall-clock time is encoded as if it were UTC; charts.js
formats labels with UTC getters only, so labels always match the log
regardless of the viewer's browser timezone (``tests/test_chart_payload.py``
pins this contract).
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from pcss import config
from pcss.common import fmt_bytes, fmt_crc
from pcss.stats import estimate_runtime

_CHARTS_JS_TEMPLATE = (Path(__file__).resolve().parent / "charts.js").read_text(encoding="utf-8")

# The dark palette mirrors the C dict in the design mockup; the light palette
# derives the same hues for a white card scheme.
PALETTES = {
    "dark": {
        "bg": "#101216", "bg2": "#161a22", "panel": "#181b21",
        "border": "rgba(255,255,255,.07)", "borderHover": "rgba(255,255,255,.16)",
        "text": "#e8eaef", "title": "#f3f5f9", "mut": "#9aa0ad", "faint": "#5c6470",
        "grid": "rgba(255,255,255,.06)", "rule": "rgba(255,255,255,.08)",
        "rowline": "rgba(255,255,255,.05)", "foot": "#454b56",
        "blue": "#5aa9f0", "teal": "#3fd0c9", "green": "#4cd08a",
        "amber": "#f3c14b", "red": "#ef6a6a", "violet": "#a78bfa",
    },
    "light": {
        "bg": "#f4f6f9", "bg2": "#e9edf3", "panel": "#ffffff",
        "border": "rgba(16,20,28,.09)", "borderHover": "rgba(16,20,28,.22)",
        "text": "#1c2530", "title": "#101820", "mut": "#5a6472", "faint": "#8a93a2",
        "grid": "rgba(16,20,28,.07)", "rule": "rgba(16,20,28,.10)",
        "rowline": "rgba(16,20,28,.06)", "foot": "#a2aab6",
        "blue": "#2b7cd3", "teal": "#0f9e97", "green": "#1f9d5b",
        "amber": "#b8830f", "red": "#d64545", "violet": "#7c5cd6",
    },
}

_SEV_LABEL = {"ok": "OK", "warn": "WARN", "crit": "ALERT", "info": "LIVE"}
_SEV_COLOR = {"ok": "green", "warn": "amber", "crit": "red", "info": "blue"}


# ======================================================================
# Payload helpers
# ======================================================================
def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


def _ms_list(ts) -> list[int]:
    """Naive local wall-clock timestamps -> epoch-ms ints encoded as if UTC."""
    arr = pd.to_datetime(pd.Series(ts)).to_numpy().astype("datetime64[ms]").astype("int64")
    return [int(v) for v in arr]


def _vals(values, dec: int = 3) -> list[float]:
    return [round(float(v), dec) for v in np.asarray(values, dtype=float)]


def _xy(df: pd.DataFrame, col: str) -> tuple[list[int], list[float]] | None:
    """(epoch-ms, values) for one DataLog column, NaNs dropped."""
    if df.empty or col not in df.columns:
        return None
    s = df.dropna(subset=[col])
    if s.empty:
        return None
    return _ms_list(s["ts"]), _vals(s[col])


def _line_series(name: str, color: str, x: list[int] | list[float], y: list[float], **style) -> dict:
    """One series dict in the shape charts.js consumes. x is epoch-ms ints
    for time panels or plain numbers for linear-axis panels (watts, days)."""
    return {"name": name, "color": color, "x": x, "y": y, **style}


def _gap_spans(gaps: pd.DataFrame) -> list[list[int]]:
    if gaps is None or gaps.empty:
        return []
    return [[a, b] for a, b in zip(_ms_list(gaps["from"]), _ms_list(gaps["to"]), strict=True)]


# ======================================================================
# Panel builders (one per chart card; None -> empty-state note)
# ======================================================================
def _panel_lv(datalog_df, voltage_anomalies, pal) -> dict | None:
    xy = _xy(datalog_df, "Line Voltage")
    if xy is None:
        return None
    nominal = (config.VOLTAGE_NORMAL_LOW + config.VOLTAGE_NORMAL_HIGH) / 2
    markers = []
    if voltage_anomalies is not None and not voltage_anomalies.empty:
        ax = _ms_list(voltage_anomalies["ts"])
        ay = _vals(voltage_anomalies["Line Voltage"])
        markers = [{"x": x, "y": y, "type": "x"} for x, y in zip(ax, ay, strict=True)]
    return {
        "kind": "line", "unit": "V", "dec": 1, "sync": True, "gaps": True, "vb": [820, 250],
        "series": [_line_series("Line Voltage", pal["blue"], *xy, width=1.8, fill=True)],
        "band": [config.VOLTAGE_NORMAL_LOW, config.VOLTAGE_NORMAL_HIGH],
        "hlines": [{"y": nominal, "color": "rgba(128,128,128,.45)", "dash": "2 4"}],
        "markers": markers,
    }


def _panel_ul(datalog_df, pal) -> dict | None:
    xy = _xy(datalog_df, "UPS Load")
    if xy is None:
        return None
    return {
        "kind": "line", "unit": "%", "dec": 0, "sync": True, "gaps": True, "vb": [460, 250],
        "series": [_line_series("UPS Load", pal["amber"], *xy, width=1.6, fill=True)],
        "yDomain": [0, 100], "yFixed": True,
        "hlines": [{"y": config.HIGH_LOAD_PCT, "color": pal["red"], "dash": "5 4",
                    "label": f"{config.HIGH_LOAD_PCT:g}%"}],
    }


def _panel_pw(energy_df, pal) -> dict | None:
    if energy_df.empty or "power_w" not in energy_df.columns:
        return None
    s = energy_df.dropna(subset=["power_w"])
    if s.empty:
        return None
    return {
        "kind": "line", "unit": "W", "dec": 0, "sync": True, "vb": [460, 250],
        "series": [_line_series("Power", pal["blue"], _ms_list(s["ts"]), _vals(s["power_w"]),
                                width=1.4, fill=True)],
    }


def _heatmap_pivot(energy_df) -> pd.DataFrame | None:
    if energy_df.empty or "power_w" not in energy_df.columns:
        return None
    hm = energy_df.dropna(subset=["power_w"]).copy()
    if hm.empty:
        return None
    hm["hour"] = hm["ts"].dt.hour
    hm["date"] = hm["ts"].dt.date
    pivot = hm.pivot_table(index="date", columns="hour", values="power_w", aggfunc="mean")
    for h in range(24):
        if h not in pivot.columns:
            pivot[h] = np.nan
    return pivot.reindex(sorted(pivot.columns), axis=1)


def _panel_hm(pivot) -> dict | None:
    if pivot is None or pivot.empty:
        return None
    z = [[(None if pd.isna(v) else round(float(v), 1)) for v in row] for row in pivot.to_numpy()]
    days = [f"{d.month}/{d.day}" for d in pivot.index]
    return {"kind": "heatmap", "unit": "W", "z": z, "days": days, "vb": [460, 250]}


def _panel_bv(datalog_df, pal) -> tuple[dict | None, float | None]:
    """Battery voltage panel (raw + rolling mean + linear trend). Returns
    (panel, slope_v_per_day) — the slope also feeds the card subtitle and
    the health pill."""
    xy = _xy(datalog_df, "Battery Voltage")
    if xy is None:
        return None, None
    series = [_line_series("reading", pal["teal"], *xy, width=1.2, opacity=0.5)]
    slope_day = None
    bv = datalog_df.dropna(subset=["Battery Voltage"])
    if len(bv) >= 5:
        # Rolling mean (~8h window at the 20-min default cadence) damps noise.
        roll = bv["Battery Voltage"].rolling(window=24, min_periods=3).mean()
        rmask = roll.notna()
        series.append(_line_series("8h mean", pal["blue"],
                                   _ms_list(bv.loc[rmask, "ts"]), _vals(roll[rmask]), width=2))
        # Linear fit of voltage against days elapsed: the slope is the
        # degradation rate — a slow downward slope is an aging battery.
        ts = bv["ts"]
        days = (ts - ts.iloc[0]).dt.total_seconds().to_numpy() / 86400.0
        volts = bv["Battery Voltage"].to_numpy(dtype=float)
        slope, intercept = np.polyfit(days, volts, 1)
        slope_day = float(slope)
        x0, x1 = _ms_list(ts.iloc[[0, -1]])
        series.append(_line_series("trend", pal["amber"], [x0, x1],
                                   _vals([intercept, intercept + slope * days[-1]]),
                                   width=2, dash="6 4"))
    panel = {
        "kind": "line", "unit": "V", "dec": 2, "sync": True, "gaps": True,
        "legend": True, "vb": [820, 250], "series": series,
    }
    return panel, slope_day


def _panel_bc(datalog_df, pal) -> dict | None:
    xy = _xy(datalog_df, "Battery Capacity")
    if xy is None:
        return None
    lo = min(min(xy[1]), 96)
    return {
        "kind": "line", "unit": "%", "dec": 0, "sync": True, "gaps": True, "vb": [460, 250],
        "series": [_line_series("Battery %", pal["green"], *xy, width=1.8, fill=True)],
        "yDomain": [max(0.0, lo - 4), 103],
    }


def _panel_rt(energy_df, pal) -> tuple[dict | None, float | None, float | None]:
    """Runtime curve + the latest operating point. Returns (panel, latest_w,
    latest_runtime_min); the latter two also feed the KPI row."""
    w_max = float(np.max(config.RUNTIME_CURVE_W))
    w_axis = np.linspace(0, w_max, 120)
    rt_axis = np.interp(w_axis, config.RUNTIME_CURVE_W, config.RUNTIME_CURVE_MIN)
    latest_w = latest_rt = None
    markers = []
    if not energy_df.empty and "power_w" in energy_df.columns and energy_df["power_w"].notna().any():
        latest_w = float(energy_df["power_w"].dropna().iloc[-1])
        latest_rt = float(estimate_runtime(latest_w))
        markers = [{"x": round(latest_w, 1), "y": round(latest_rt, 1), "type": "star",
                    "color": pal["red"], "label": f"Now {latest_rt:.0f}m"}]
    panel = {
        "kind": "line", "unit": "min", "dec": 1, "xkind": "linear", "xunit": "W",
        "vb": [460, 250], "xDomain": [0, w_max],
        "series": [_line_series("runtime", pal["violet"], _vals(w_axis, 1), _vals(rt_axis, 1),
                                width=2, fill=True)],
        "markers": markers,
    }
    return panel, latest_w, latest_rt


def _panel_kw(energy_summary, pal) -> dict | None:
    if not energy_summary or "samples" not in energy_summary:
        return None
    s = energy_summary["samples"].copy().sort_values("ts")
    if s.empty:
        return None
    x = _ms_list(s["ts"])
    cum_kwh = s["kwh"].cumsum()
    return {
        "kind": "line", "unit": "kWh", "y2unit": "CRC", "dec": 2, "sync": True,
        "legend": True, "vb": [820, 250], "y2fmt": "crc-k",
        "series": [
            _line_series("kWh", pal["teal"], x, _vals(cum_kwh, 4), width=2, fill=True),
            _line_series("cost ₡", pal["violet"], x,
                         _vals(cum_kwh * config.PCSS_FLAT_RATE, 2), width=2, right=True),
        ],
    }


def _panel_daily(energy_summary, pal) -> dict | None:
    if not energy_summary or energy_summary.get("daily") is None or energy_summary["daily"].empty:
        return None
    d = energy_summary["daily"]
    return {
        "kind": "bar", "unit": "kWh", "dec": 2, "vb": [460, 250], "barName": "energy",
        "color": pal["blue"],
        "data": [{"label": f"{dt.month}/{dt.day}", "y": round(float(k), 4)}
                 for dt, k in zip(d["date"], d["kwh"], strict=True)],
    }


def _panel_growth(hist, pal) -> dict | None:
    if hist is None or hist.empty:
        return None
    x = _ms_list(hist["timestamp"])
    series = []
    for col, label, color, width in [
        ("total_bytes", "total", pal["green"], 2),
        ("datalog_bytes", "data", pal["blue"], 1.8),
        ("energylog_bytes", "energy", pal["amber"], 1.5),
        ("eventlog_bytes", "event", pal["teal"], 1.5),
    ]:
        if col in hist.columns:
            series.append(_line_series(label, color, x, _vals(hist[col] / 1024, 1), width=width))
    if not series:
        return None
    return {
        "kind": "line", "unit": "KB", "dec": 1, "legend": True,
        "vb": [460, 250], "series": series,
    }


def _panel_proj(dl_stats, pal) -> tuple[dict | None, float | None]:
    if not dl_stats or not dl_stats.get("daily_bytes") or not np.isfinite(dl_stats["daily_bytes"]):
        return None, None
    daily_kb = dl_stats["daily_bytes"] / 1024
    days = list(range(0, 366, 5))
    proj_1yr_kb = daily_kb * 365
    panel = {
        "kind": "line", "unit": "KB", "dec": 0, "xkind": "linear", "xunit": "d",
        "vb": [460, 250], "xDomain": [0, 365],
        "series": [_line_series("projected", pal["blue"], days,
                                _vals([daily_kb * d for d in days], 1), width=2, fill=True)],
        "markers": [{"x": d, "y": round(daily_kb * d, 1), "type": "dot", "color": pal["amber"],
                     "label": lbl}
                    for d, lbl in [(30, "1mo"), (90, "3mo"), (180, "6mo"), (365, "1yr")]],
    }
    return panel, proj_1yr_kb


def _panel_cad(datalog_df, pal) -> dict | None:
    if datalog_df.empty or len(datalog_df) < 3:
        return None
    deltas = datalog_df["ts"].diff().dt.total_seconds().dropna() / 60.0
    if deltas.empty:
        return None
    e = config.DATALOG_EXPECTED_INTERVAL_MIN
    edges = [0, 0.95 * e, 1.05 * e, 1.25 * e, 2 * e, float("inf")]
    labels = [f"<{0.95 * e:g}m", f"{0.95 * e:g}-{1.05 * e:g}m", f"{1.05 * e:g}-{1.25 * e:g}m",
              f"{1.25 * e:g}-{2 * e:g}m", f">{2 * e:g}m"]
    counts = pd.cut(deltas, bins=edges, labels=labels, include_lowest=True).value_counts()
    return {
        "kind": "bar", "unit": "samples", "dec": 0, "vb": [460, 250], "barName": "intervals",
        "data": [{"label": lbl, "y": int(counts.get(lbl, 0)),
                  "color": pal["teal"] if i == 1 else pal["faint"]}
                 for i, lbl in enumerate(labels)],
    }


# ======================================================================
# KPI row + health pill
# ======================================================================
def _spark(df: pd.DataFrame, col: str, color: str, transform=None) -> dict | None:
    """Last ~3 days of a column as sparkline data (<=120 points)."""
    if df.empty or col not in df.columns:
        return None
    s = df.dropna(subset=[col])
    if len(s) < 2:
        return None
    cutoff = s["ts"].iloc[-1] - pd.Timedelta(days=3)
    s = s[s["ts"] >= cutoff]
    if len(s) < 2:
        return None
    stride = max(1, len(s) // 120)
    s = s.iloc[::stride]
    y = s[col].to_numpy(dtype=float)
    if transform is not None:
        y = transform(y)
    return {"color": color, "x": _ms_list(s["ts"]), "y": _vals(y, 2)}


def _build_kpis(datalog_df, energy_df, latest_w, latest_rt, pal):
    """The five header cards. Returns (cards, sparks, severities) where
    severities feed the health pill (info does not count against health)."""
    cards: list[dict] = []
    sparks: list[dict | None] = []
    sevs: list[str] = []

    def add(label, value, unit, sub, sev, spark):
        cards.append({
            "label": label, "value": value, "unit": unit, "sub": sub,
            "status": _SEV_LABEL[sev], "color": pal[_SEV_COLOR[sev]],
        })
        sparks.append(spark)
        if sev != "info":
            sevs.append(sev)

    latest = datalog_df.iloc[-1] if not datalog_df.empty else None

    v = None if latest is None or "Line Voltage" not in datalog_df.columns else latest["Line Voltage"]
    if v is not None and pd.notna(v):
        sev = "ok" if config.VOLTAGE_NORMAL_LOW <= v <= config.VOLTAGE_NORMAL_HIGH else "crit"
        add("Line Voltage", f"{v:.1f}", "V",
            f"envelope {config.VOLTAGE_NORMAL_LOW:g}–{config.VOLTAGE_NORMAL_HIGH:g} V",
            sev, _spark(datalog_df, "Line Voltage", pal["blue"]))
    else:
        add("Line Voltage", "—", "", "no data", "info", None)

    u = None if latest is None or "UPS Load" not in datalog_df.columns else latest["UPS Load"]
    if u is not None and pd.notna(u):
        sev = "ok" if u < 0.875 * config.HIGH_LOAD_PCT else ("warn" if u < config.HIGH_LOAD_PCT else "crit")
        add("UPS Load", f"{u:.0f}", "%", f"high-load > {config.HIGH_LOAD_PCT:g}%",
            sev, _spark(datalog_df, "UPS Load", pal["amber"]))
    else:
        add("UPS Load", "—", "", "no data", "info", None)

    c = None if latest is None or "Battery Capacity" not in datalog_df.columns else latest["Battery Capacity"]
    if c is not None and pd.notna(c):
        sev = ("ok" if c >= config.BATTERY_CHARGE_WARN_PCT
               else ("warn" if c >= config.BATTERY_CHARGE_CRIT_PCT else "crit"))
        bv = latest.get("Battery Voltage") if latest is not None else None
        sub = f"{bv:.1f} V bus" if bv is not None and pd.notna(bv) else "% capacity"
        add("Battery Charge", f"{c:.0f}", "%", sub, sev,
            _spark(datalog_df, "Battery Capacity", pal["green"]))
    else:
        add("Battery Charge", "—", "", "no data", "info", None)

    if latest_rt is not None:
        sev = ("ok" if latest_rt >= config.RUNTIME_WARN_MIN
               else ("warn" if latest_rt >= config.RUNTIME_CRIT_MIN else "crit"))
        rt_spark = _spark(energy_df, "power_w", pal["teal"],
                          transform=lambda w: np.interp(w, config.RUNTIME_CURVE_W,
                                                        config.RUNTIME_CURVE_MIN))
        add("Est. Runtime", f"{latest_rt:.0f}", "min", f"at {latest_w:.0f} W", sev, rt_spark)
    else:
        add("Est. Runtime", "—", "", "no data", "info", None)

    if latest_w is not None:
        add("Power Draw", f"{latest_w:.0f}", "W", "5-min sample", "info",
            _spark(energy_df, "power_w", pal["blue"]))
    else:
        add("Power Draw", "—", "", "no data", "info", None)

    return cards, sparks, sevs


def _build_health(sevs, bv_slope, voltage_anomalies, high_load_episodes, gaps, pal) -> dict:
    n_crit = sevs.count("crit")
    n_warn = sevs.count("warn")
    counts = (f"{_count(len(voltage_anomalies), 'anomaly', 'anomalies')} · "
              f"{len(high_load_episodes)} high-load · "
              f"{_count(len(gaps), 'gap', 'gaps')} in window")
    if n_crit:
        label, color = ("Multiple alerts" if n_crit > 1 else "Attention needed"), pal["red"]
        sub = f"{n_crit + n_warn} metric(s) outside normal range · {counts}"
    elif n_warn:
        label, color = "Advisory", pal["amber"]
        sub = f"{n_warn} metric(s) near limits · {counts}"
    else:
        label, color = "All systems nominal", pal["green"]
        sub = (f"battery trend {bv_slope:+.4f} V/day · {counts}"
               if bv_slope is not None else counts)
    return {"label": label, "color": color, "sub": sub}


# ======================================================================
# Shell HTML
# ======================================================================
def _tools_html(key: str, zoomable: bool) -> str:
    reset = (f'<button class="tool-btn tool-reset" data-panel="{key}" hidden '
             f'title="Reset zoom">reset</button>') if zoomable else ""
    return (f'<div class="card-tools">{reset}'
            f'<button class="tool-btn tool-png" data-panel="{key}" title="Export PNG">png</button>'
            f'<button class="tool-btn tool-csv" data-panel="{key}" title="Export CSV">csv</button>'
            f'<button class="tool-btn tool-expand" data-panel="{key}" title="Expand">⤢</button>'
            f'</div>')


def _chart_card(key: str, span: int, title: str, sub: str, zoomable: bool,
                sub_color: str | None = None) -> str:
    sub_style = f' style="color:{sub_color}"' if sub_color else ""
    return f"""
<div class="card chart-card s{span}">
  <div class="card-head">
    <div class="card-title">{_esc(title)}</div>
    <div class="card-side"><span class="card-sub"{sub_style}>{_esc(sub)}</span>{_tools_html(key, zoomable)}</div>
  </div>
  <div class="chart-box" id="panel-{key}" data-title="{_esc(title)}"></div>
</div>"""


def _section_head(title: str, note: str, presets: bool = False) -> str:
    pills = ""
    if presets:
        pills = ('<span class="presets">'
                 '<button class="preset-pill" data-days="all">All</button>'
                 '<button class="preset-pill" data-days="30">30 d</button>'
                 '<button class="preset-pill" data-days="7">7 d</button>'
                 '<button class="preset-pill" data-days="1">24 h</button>'
                 '</span>')
    return (f'<div class="sec-head"><h2>{_esc(title)}</h2><div class="sec-rule"></div>'
            f'{pills}<span class="sec-note">{_esc(note)}</span></div>')


def _kpi_row_html(cards: list[dict]) -> str:
    out = ['<div class="kpis">']
    for i, k in enumerate(cards):
        out.append(f"""
<div class="card kpi-card">
  <div class="kpi-accent" style="background:{k['color']}"></div>
  <div class="kpi-top"><span class="kpi-label">{_esc(k['label'])}</span>
    <span class="kpi-pill" style="color:{k['color']};background:color-mix(in srgb, {k['color']} 14%, transparent)">{_esc(k['status'])}</span></div>
  <div class="kpi-value-row"><span class="kpi-value">{_esc(k['value'])}</span><span class="kpi-unit">{_esc(k['unit'])}</span></div>
  <div class="kpi-spark" data-idx="{i}"></div>
  <div class="kpi-sub">{_esc(k['sub'])}</div>
</div>""")
    out.append("</div>")
    return "".join(out)


def _stats_table_html(stats_table: pd.DataFrame) -> str:
    if stats_table.empty:
        return '<div class="chart-empty">no data in the analyzed window</div>'
    cols = list(stats_table.columns)
    head = "".join(f'<th class="{"tl" if i == 0 else "tr"}">{_esc(c)}</th>' for i, c in enumerate(cols))
    rows = []
    for row in stats_table.itertuples(index=False, name=None):
        cells = "".join(
            f'<td class="{"tl" if i == 0 else "tr"}{" hi" if i in (0, 2, len(cols) - 2) else ""}">{_esc(v)}</td>'
            for i, v in enumerate(row))
        rows.append(f"<tr>{cells}</tr>")
    return (f'<table class="stats-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _summary_list_html(rows: list[tuple[str, str, str | None]]) -> str:
    """rows: (key, value, color-or-None). A key of '#' renders a mini section
    label instead of a data row."""
    out = []
    for k, v, color in rows:
        if k == "#":
            out.append(f'<div class="mini-head">{_esc(v)}</div>')
        else:
            style = f' style="color:{color}"' if color else ""
            out.append(f'<div class="sum-row"><span class="sum-k">{_esc(k)}</span>'
                       f'<span class="sum-v"{style}>{_esc(v)}</span></div>')
    return "".join(out)


def _summary_rows(datalog_df, energy_summary, crossval, sizes, hist_stats,
                  voltage_anomalies, high_load_episodes, gaps, pal):
    """The two Reference lists — every field the classic tables reported."""
    latest_rows: list[tuple[str, str, str | None]] = []
    if not datalog_df.empty:
        latest = datalog_df.iloc[-1]
        latest_rows.append(("Timestamp", str(latest["ts"]), None))
        for c in ["Line Voltage", "Battery Voltage", "UPS Load", "Battery Capacity",
                  "Output Frequency", "Input Frequency"]:
            if c in datalog_df.columns:
                v = latest[c]
                if not (isinstance(v, float) and np.isnan(v)):
                    latest_rows.append((c, f"{v}", None))
    else:
        latest_rows.append(("DataLog", "no rows", None))

    run_rows: list[tuple[str, str, str | None]] = []
    if energy_summary:
        run_rows.append(("#", "Energy", None))
        run_rows.append(("Total energy", f"{energy_summary['total_kwh']:.4f} kWh", None))
        run_rows.append((f"Cost (PCSS flat ₡{config.PCSS_FLAT_RATE:g})",
                         fmt_crc(energy_summary["total_cost_pcss"]), None))
        run_rows.append(("Cost (Coopesantos tiered)",
                         fmt_crc(energy_summary["total_cost_tiered"]), pal["violet"]))
        run_rows.append(("CO₂ emitted", f"{energy_summary['total_co2_kg']:.4f} kg", None))
        run_rows.append(("Span", f"{energy_summary['first']} → {energy_summary['last']}", None))
        run_rows.append(("Samples (5-min)", f"{energy_summary['n_samples']}", None))
    if crossval:
        run_rows.append(("#", "Cross-validation", None))
        run_rows.append(("DataLog vs energylog (load %)",
                         f"MAE {crossval['mean_abs_error_pct']:.2f}%, n={crossval['n_pairs']}", None))
    run_rows.append(("#", "Files", None))
    run_rows.append(("DataLog", fmt_bytes(sizes.get("DataLog", 0)), None))
    run_rows.append(("EventLog", fmt_bytes(sizes.get("EventLog (binary)", 0)), None))
    run_rows.append(("energylog/", fmt_bytes(sizes.get("energylog/", 0)), None))
    run_rows.append(("TOTAL", fmt_bytes(sum(sizes.values())), None))
    if hist_stats:
        run_rows.append(("#", "Growth", None))
        run_rows.append(("Per hour", fmt_bytes(hist_stats["bytes_per_hour"]), None))
        run_rows.append(("Per day", fmt_bytes(hist_stats["bytes_per_day"]), None))
        run_rows.append(("Snapshots", str(hist_stats["snapshots"]), None))
    run_rows.append(("#", "Anomalies", None))
    run_rows.append(("Voltage out-of-range", f"{len(voltage_anomalies)} samples",
                     pal["red"] if len(voltage_anomalies) else None))
    run_rows.append(("Sustained high-load episodes", f"{len(high_load_episodes)}",
                     pal["amber"] if len(high_load_episodes) else None))
    run_rows.append((f"DataLog gaps (>{config.DATALOG_EXPECTED_INTERVAL_MIN * 2:.0f} min)",
                     f"{len(gaps)}", pal["amber"] if len(gaps) else None))
    return latest_rows, run_rows


def _shell_css(pal: dict) -> str:
    return f"""
:root {{ --bg: {pal['bg']}; --bg2: {pal['bg2']}; --panel: {pal['panel']}; --border: {pal['border']};
  --border-hover: {pal['borderHover']}; --text: {pal['text']}; --title: {pal['title']};
  --mut: {pal['mut']}; --faint: {pal['faint']}; --rule: {pal['rule']}; --rowline: {pal['rowline']};
  --foot: {pal['foot']}; --blue: {pal['blue']}; --green: {pal['green']}; --amber: {pal['amber']};
  --red: {pal['red']}; --violet: {pal['violet']}; --teal: {pal['teal']}; }}
* {{ box-sizing: border-box; }}
html, body {{ margin: 0; background: var(--bg); }}
body {{ min-height: 100vh; background: radial-gradient(1200px 600px at 75% -10%, var(--bg2) 0%, var(--bg) 60%);
  color: var(--text); font-family: system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
  padding: 26px 30px 64px; }}
.mono, .card-sub, .sec-note, .kpi-value, .kpi-unit, .kpi-sub, .sum-v, .presets, .tool-btn, .brand, .header-sub, .health-sub {{
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}
.wrap {{ max-width: 1440px; margin: 0 auto; display: flex; flex-direction: column; gap: 14px; }}
::selection {{ background: rgba(90,169,240,.3); }}

header {{ display: flex; justify-content: space-between; align-items: flex-end; gap: 24px;
  flex-wrap: wrap; padding: 4px 2px 6px; }}
.brand {{ font-size: 12px; letter-spacing: .22em; color: var(--blue); font-weight: 700; }}
h1 {{ font-size: 27px; font-weight: 600; margin: 5px 0 0; color: var(--title); letter-spacing: -.015em; }}
.header-sub {{ font-size: 12.5px; color: var(--mut); margin-top: 6px; }}
#staleness {{ color: var(--faint); }}
#staleness.is-stale {{ color: var(--amber); }}
.health-wrap {{ text-align: right; }}
.health-pill {{ display: inline-flex; align-items: center; gap: 10px; padding: 11px 18px;
  border-radius: 13px; background: color-mix(in srgb, var(--health) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--health) 30%, transparent); }}
.health-dot {{ width: 9px; height: 9px; border-radius: 50%; background: var(--health);
  box-shadow: 0 0 12px var(--health); }}
.health-label {{ font-size: 15px; font-weight: 600; color: var(--health); }}
.health-sub {{ font-size: 12px; color: var(--mut); margin-top: 8px; }}

.kpis {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 14px; }}
.card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 14px;
  transition: border-color .18s ease; }}
.card:hover {{ border-color: var(--border-hover); }}
.kpi-card {{ position: relative; padding: 15px 16px 14px; overflow: hidden; }}
.kpi-accent {{ position: absolute; left: 0; top: 0; bottom: 0; width: 3px; }}
.kpi-top {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; }}
.kpi-label {{ font-size: 11px; letter-spacing: .09em; text-transform: uppercase; color: var(--mut); font-weight: 700; }}
.kpi-pill {{ font-size: 10px; font-weight: 700; letter-spacing: .05em; padding: 2px 8px; border-radius: 999px; }}
.kpi-value-row {{ display: flex; align-items: baseline; gap: 5px; margin-top: 9px; }}
.kpi-value {{ font-size: 34px; font-weight: 600; color: var(--title); line-height: 1; letter-spacing: -.025em; }}
.kpi-unit {{ font-size: 14px; color: var(--mut); }}
.kpi-spark {{ margin-top: 9px; height: 34px; }}
.kpi-sub {{ font-size: 11.5px; color: var(--faint); margin-top: 8px; }}

.sec-head {{ display: flex; align-items: baseline; gap: 12px; margin: 22px 2px 0; }}
.sec-head h2 {{ font-size: 12.5px; letter-spacing: .16em; text-transform: uppercase;
  color: var(--title); font-weight: 700; margin: 0; }}
.sec-rule {{ flex: 1; height: 1px; background: var(--rule); }}
.sec-note {{ font-size: 11.5px; color: var(--faint); }}
.presets {{ display: inline-flex; gap: 4px; }}
.preset-pill {{ font-size: 10.5px; padding: 2px 9px; border-radius: 999px; cursor: pointer;
  background: transparent; border: 1px solid var(--border); color: var(--mut);
  font-family: inherit; }}
.preset-pill:hover {{ border-color: var(--border-hover); color: var(--text); }}
.preset-pill.is-active {{ color: var(--blue); border-color: color-mix(in srgb, var(--blue) 45%, transparent);
  background: color-mix(in srgb, var(--blue) 10%, transparent); }}

.grid12 {{ display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }}
.s3 {{ grid-column: span 3; }} .s4 {{ grid-column: span 4; }} .s5 {{ grid-column: span 5; }}
.s6 {{ grid-column: span 6; }} .s7 {{ grid-column: span 7; }} .s8 {{ grid-column: span 8; }}
.s12 {{ grid-column: span 12; }}
.chart-card {{ padding: 16px 16px 12px; }}
.card-head {{ display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 6px; gap: 10px; flex-wrap: wrap; }}
.card-title {{ font-size: 14.5px; font-weight: 600; color: var(--title); }}
.card-side {{ display: inline-flex; align-items: center; gap: 8px; }}
.card-sub {{ font-size: 12px; color: var(--mut); }}
.card-tools {{ display: inline-flex; gap: 4px; opacity: 0; transition: opacity .15s ease; }}
.chart-card:hover .card-tools, .card-tools:focus-within {{ opacity: 1; }}
.tool-btn {{ font-size: 10px; padding: 2px 7px; border-radius: 6px; cursor: pointer;
  background: transparent; border: 1px solid var(--border); color: var(--faint); }}
.tool-btn:hover {{ color: var(--text); border-color: var(--border-hover); }}
.tool-reset {{ color: var(--blue); border-color: color-mix(in srgb, var(--blue) 45%, transparent); }}
.chart-box {{ position: relative; touch-action: pan-y;
  user-select: none; -webkit-user-select: none; }}
.chart-box svg {{ cursor: crosshair; }}
.chart-empty {{ padding: 40px 0 48px; text-align: center; color: var(--faint); font-size: 12.5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }}

.table-card {{ padding: 16px 18px 14px; }}
.stats-table {{ width: 100%; border-collapse: collapse;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 13px; }}
.stats-table th {{ color: var(--mut); font-weight: 600; font-size: 10.5px; letter-spacing: .08em;
  text-transform: uppercase; padding: 0 0 9px; }}
.stats-table td {{ padding: 8px 0; border-top: 1px solid var(--rowline); color: var(--mut); }}
.stats-table td.hi {{ color: var(--text); }}
.tl {{ text-align: left; }} .tr {{ text-align: right; padding-left: 10px !important; }}
.mini-head {{ font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase; color: var(--blue);
  font-weight: 700; margin: 10px 0 4px; }}
.sum-col > .mini-head:first-child {{ margin-top: 0; }}
.sum-row {{ display: flex; justify-content: space-between; gap: 10px; padding: 6px 0;
  border-bottom: 1px solid var(--rowline); }}
.sum-k {{ color: var(--mut); font-size: 12px; }}
.sum-v {{ color: var(--text); font-size: 12px; font-weight: 600; text-align: right; }}
.sum-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 22px; }}

footer {{ margin-top: 24px; text-align: center; font-size: 11.5px; color: var(--faint);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height: 1.7; }}
footer .dim {{ color: var(--foot); }}

.chart-tooltip {{ position: fixed; z-index: 3000; pointer-events: none; max-width: 340px;
  background: var(--panel); border: 1px solid var(--border-hover); border-radius: 9px;
  padding: 8px 11px; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; color: var(--text); box-shadow: 0 6px 24px rgba(0,0,0,.35); }}
.chart-tooltip.is-pinned {{ border-style: dashed; border-color: var(--blue); }}
.tt-ts {{ color: var(--mut); font-size: 11px; margin-bottom: 4px; }}
.tt-row {{ display: flex; align-items: center; gap: 6px; padding: 1.5px 0; }}
.tt-dot {{ width: 8px; height: 8px; border-radius: 50%; flex: none; }}
.tt-name {{ color: var(--mut); }}
.tt-val {{ margin-left: auto; font-weight: 600; padding-left: 14px; }}

#lightbox {{ position: fixed; inset: 0; z-index: 2000; background: rgba(0,0,0,.55);
  display: flex; align-items: center; justify-content: center; padding: 4vh 4vw; }}
#lightbox[hidden] {{ display: none; }}
.lightbox-card {{ background: var(--panel); border: 1px solid var(--border-hover); border-radius: 14px;
  padding: 16px 18px 12px; width: min(1280px, 96vw); }}
.lightbox-close {{ background: transparent; border: 1px solid var(--border); border-radius: 6px;
  color: var(--mut); cursor: pointer; font-size: 12px; padding: 2px 9px; }}
.lightbox-close:hover {{ color: var(--text); border-color: var(--border-hover); }}

@media (max-width: 1080px) {{ .grid12 > * {{ grid-column: 1 / -1 !important; }} }}
@media (max-width: 780px) {{ .kpis {{ grid-template-columns: repeat(2, 1fr) !important; }} }}
"""


# ======================================================================
# build_dashboard — the public entry point
# ======================================================================
def build_dashboard(datalog_df: pd.DataFrame, energy_df: pd.DataFrame, hist: pd.DataFrame,
                    dl_stats: dict, hist_stats: dict, sizes: dict, energy_summary: dict,
                    stats_table: pd.DataFrame, gaps: pd.DataFrame,
                    voltage_anomalies: pd.DataFrame, high_load_episodes: pd.DataFrame,
                    crossval: dict) -> str:
    """Assemble the dashboard page and return the finished HTML string."""
    pal = PALETTES.get(config.DASHBOARD_THEME, PALETTES["dark"])

    bv_panel, bv_slope = _panel_bv(datalog_df, pal)
    rt_panel, latest_w, latest_rt = _panel_rt(energy_df, pal)
    proj_panel, proj_1yr_kb = _panel_proj(dl_stats, pal)
    panels = {
        "lv": _panel_lv(datalog_df, voltage_anomalies, pal),
        "ul": _panel_ul(datalog_df, pal),
        "pw": _panel_pw(energy_df, pal),
        "hm": _panel_hm(_heatmap_pivot(energy_df)),
        "bv": bv_panel,
        "bc": _panel_bc(datalog_df, pal),
        "rt": rt_panel,
        "kw": _panel_kw(energy_summary, pal),
        "daily": _panel_daily(energy_summary, pal),
        "growth": _panel_growth(hist, pal),
        "proj": proj_panel,
        "cad": _panel_cad(datalog_df, pal),
    }

    kpis, sparks, sevs = _build_kpis(datalog_df, energy_df, latest_w, latest_rt, pal)
    health = _build_health(sevs, bv_slope, voltage_anomalies, high_load_episodes, gaps, pal)

    last_sample_ms = None
    if not datalog_df.empty:
        last_sample_ms = _ms_list(datalog_df["ts"].iloc[[-1]])[0]

    payload = {
        "theme": config.DASHBOARD_THEME,
        "palette": pal,
        "gaps": _gap_spans(gaps),
        "panels": panels,
        "sparks": sparks,
        "meta": {
            "last_sample_ms": last_sample_ms,
            "expected_interval_min": config.DATALOG_EXPECTED_INTERVAL_MIN,
        },
    }

    # ----- card subtitles (server-side context strings) -----
    nominal = (config.VOLTAGE_NORMAL_LOW + config.VOLTAGE_NORMAL_HIGH) / 2
    lv_sub = f"{_count(len(voltage_anomalies), 'anomaly', 'anomalies')} · nominal {nominal:g} V"
    ul_sub = _count(len(high_load_episodes), "high-load episode", "high-load episodes")
    pw_sub = "5-min samples"
    if energy_summary:
        span_d = (energy_summary["last"] - energy_summary["first"]).total_seconds() / 86400
        pw_sub = f"5-min · {span_d:.0f} d"
    bv_sub = f"{bv_slope:+.4f} V/day · {'aging' if bv_slope < 0 else 'stable'}" if bv_slope is not None else "trend"
    rt_sub = f"{latest_w:.0f}W → {latest_rt:.0f} min" if latest_rt is not None else "runtime curve"
    kw_sub = f"kWh · ₡ (flat ₡{config.PCSS_FLAT_RATE:g}/kWh)"
    proj_sub = f"≈ {proj_1yr_kb / 1024:.1f} MB / yr" if proj_1yr_kb else "current rate"
    energy_note = (f"{energy_summary['total_kwh']:.1f} kWh · "
                   f"₡{energy_summary['total_cost_pcss']:,.0f} flat · "
                   f"₡{energy_summary['total_cost_tiered']:,.0f} tiered") if energy_summary else "no energy data"
    growth_note = (f"+{fmt_bytes(hist_stats['bytes_per_day'])}/day · "
                   f"{fmt_bytes(sum(sizes.values()))} total") if hist_stats else fmt_bytes(sum(sizes.values())) + " total"

    n_samples = len(datalog_df)
    span_days = dl_stats.get("span_days") if dl_stats else None
    cadence = dl_stats.get("median_interval_sec") if dl_stats else None
    sub_bits = ["PowerChute Serial Shutdown"]
    if n_samples:
        sub_bits.append(f"{n_samples:,} samples")
    if span_days and np.isfinite(span_days):
        sub_bits.append(f"{span_days:.1f} days")
    if cadence and np.isfinite(cadence):
        sub_bits.append(f"{cadence / 60:.0f}-min cadence")
    header_sub = " · ".join(sub_bits)

    latest_rows, run_rows = _summary_rows(datalog_df, energy_summary, crossval, sizes,
                                          hist_stats, voltage_anomalies, high_load_episodes,
                                          gaps, pal)

    generated = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    foot = (f"Generated {generated} · read-only analytics snapshot · "
            f"envelope {config.VOLTAGE_NORMAL_LOW:g}–{config.VOLTAGE_NORMAL_HIGH:g} V · "
            f"high-load {config.HIGH_LOAD_PCT:g}% · theme {config.DASHBOARD_THEME}")

    payload_json = json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    charts_js = _CHARTS_JS_TEMPLATE.replace("__DASH_DATA__", payload_json)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PowerChute UPS Dashboard</title>
<style>{_shell_css(pal)}</style>
</head>
<body>
<div class="wrap">

  <header>
    <div>
      <div class="brand">UPS&nbsp;MONITOR</div>
      <h1>{_esc(config.DASHBOARD_MODEL)}</h1>
      <div class="header-sub">{_esc(header_sub)} · <span id="staleness"></span></div>
    </div>
    <div class="health-wrap" style="--health:{health['color']}">
      <div class="health-pill"><span class="health-dot"></span>
        <span class="health-label">{_esc(health['label'])}</span></div>
      <div class="health-sub">{_esc(health['sub'])}</div>
    </div>
  </header>

  {_kpi_row_html(kpis)}

  {_section_head('Power Quality', 'line · load · draw', presets=True)}
  <div class="grid12">
    {_chart_card('lv', 12, 'Line Voltage', lv_sub, True)}
    {_chart_card('ul', 4, 'UPS Load', ul_sub, True)}
    {_chart_card('pw', 4, 'Power Draw', pw_sub, True)}
    {_chart_card('hm', 4, 'Hourly Power Map', 'mean W', False)}
  </div>

  {_section_head('Battery Health', 'voltage · charge · runtime')}
  <div class="grid12">
    {_chart_card('bv', 12, 'Battery Voltage', bv_sub, True, sub_color=pal['amber'] if bv_slope is not None and bv_slope < 0 else None)}
    {_chart_card('bc', 6, 'Battery Charge', '% capacity', True)}
    {_chart_card('rt', 6, 'Estimated Runtime', rt_sub, False, sub_color=pal['violet'])}
  </div>

  {_section_head('Energy & Cost', energy_note)}
  <div class="grid12">
    {_chart_card('kw', 8, 'Cumulative Energy & Cost', kw_sub, True)}
    {_chart_card('daily', 4, 'Daily Energy', 'kWh / day', False)}
  </div>

  {_section_head('Logs & Storage', growth_note)}
  <div class="grid12">
    {_chart_card('growth', 6, 'Log File Growth', 'KB', True)}
    {_chart_card('proj', 3, 'DataLog Projection', proj_sub, False)}
    {_chart_card('cad', 3, 'Sample Cadence', 'interval min', False)}
  </div>

  {_section_head('Reference', 'statistics · latest readings')}
  <div class="grid12">
    <div class="card table-card s7">
      <div class="card-title" style="margin-bottom:12px">Per-metric Statistics</div>
      {_stats_table_html(stats_table)}
    </div>
    <div class="card table-card s5">
      <div class="card-title" style="margin-bottom:12px">Latest Readings &amp; Run Summary</div>
      <div class="sum-grid">
        <div class="sum-col">
          <div class="mini-head">Latest sample</div>
          {_summary_list_html(latest_rows)}
        </div>
        <div class="sum-col" style="--blue:{pal['green']}">
          {_summary_list_html(run_rows)}
        </div>
      </div>
    </div>
  </div>

  <footer>
    <div>{_esc(foot)}</div>
    <div class="dim">Charts rendered as inline SVG — no chart library, fully offline.</div>
  </footer>

</div>

<div id="lightbox" hidden>
  <div class="lightbox-card">
    <div class="card-head">
      <div class="card-title" id="lightbox-title"></div>
      <button class="lightbox-close" type="button" title="Close">✕</button>
    </div>
    <div class="chart-box" id="lightbox-chart"></div>
  </div>
</div>

{charts_js}
</body>
</html>
"""
    return page
