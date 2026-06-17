"""Plotly dashboard construction: the 7×2 panel grid, per-metric tables, and
the latest-readings / cost summary. Returns (fig, animations); the caller
injects the animation controls and writes the HTML."""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from pcss import config
from pcss.animation import (
    _heatmap_metadata,
    _register_animation,
    _replay_metadata,
    _runtime_metadata,
)
from pcss.common import fmt_bytes, fmt_crc
from pcss.stats import estimate_runtime


def add_timeseries(fig, df, col, row, col_idx, color, ylabel,
                   animated: list | None = None, show_legend: bool = True):
    """Add a time-series trace. If `animated` is a list, append
    (trace_index, x_array, y_array) so the caller can build animation frames.
    `show_legend=False` keeps the trace off the shared legend (used for
    single-trace panels whose subplot title already names them — it keeps the
    horizontal legend short enough not to wrap into the figure title)."""
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
            showlegend=show_legend,
        ),
        row=row, col=col_idx,
    )
    if animated is not None:
        animated.append((len(fig.data) - 1, x, y))


def _add_battery_health(fig, datalog_df) -> None:
    """Overlay a rolling mean + linear-fit degradation trend on the Battery
    Voltage panel (row 1, col 2). Static reference traces (not animated) — a
    slow downward slope over weeks/months is the early sign of an aging
    battery. Kept on the existing panel to avoid disturbing the 7×2 grid and
    the tuned animation-overlay geometry."""
    if "Battery Voltage" not in datalog_df.columns:
        return
    bv = datalog_df.dropna(subset=["Battery Voltage"])
    if len(bv) < 5:
        return
    ts = bv["ts"]
    volts = bv["Battery Voltage"].to_numpy(dtype=float)
    # Rolling mean (~8h window at the 20-min default cadence) to damp noise.
    roll = bv["Battery Voltage"].rolling(window=24, min_periods=3).mean()
    fig.add_trace(
        go.Scatter(
            x=ts, y=roll, mode="lines", name="BV rolling mean",
            line=dict(color="#555555", width=1, dash="dot"),
            hovertemplate="rolling mean<br>%{x|%Y-%m-%d %H:%M}<br>%{y:.2f} V<extra></extra>",
        ),
        row=1, col=2,
    )
    # Linear fit of voltage vs days-elapsed → slope is the degradation rate.
    days = (ts - ts.iloc[0]).dt.total_seconds().to_numpy() / 86400.0
    slope, intercept = np.polyfit(days, volts, 1)
    trend = slope * days + intercept
    fig.add_trace(
        go.Scatter(
            x=ts, y=trend, mode="lines",
            name=f"BV trend ({slope:+.3f} V/day)",
            line=dict(color="#ff7f0e", width=1.5),
            hovertemplate=f"trend {slope:+.4f} V/day<br>%{{x|%Y-%m-%d %H:%M}}<br>%{{y:.2f}} V<extra></extra>",
        ),
        row=1, col=2,
    )


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
        fig.add_hrect(y0=config.VOLTAGE_NORMAL_LOW, y1=config.VOLTAGE_NORMAL_HIGH,
                      fillcolor="green", opacity=0.05, line_width=0, row=1, col=1)

        add_timeseries(fig, datalog_df, "Battery Voltage", 1, 2, "#2ca02c", "Battery Voltage", bv_animated)
        _add_battery_health(fig, datalog_df)
        add_timeseries(fig, datalog_df, "UPS Load", 2, 1, "#d62728", "UPS Load", ul_animated,
                       show_legend=False)
        # 80% threshold line on UPS Load
        fig.add_hline(y=config.HIGH_LOAD_PCT, line_dash="dash", line_color="orange",
                      annotation_text=f"{config.HIGH_LOAD_PCT}% threshold",
                      annotation_position="top right", row=2, col=1)
        add_timeseries(fig, datalog_df, "Battery Capacity", 2, 2, "#bcbd22", "Battery %", bc_animated,
                       show_legend=False)

    # ----- Row 3 col 1: Power consumption from energylog -----
    if not energy_df.empty:
        s = energy_df.dropna(subset=["power_w"])
        fig.add_trace(
            go.Scatter(
                x=s["ts"], y=s["power_w"],
                mode="lines", name="Power (W)",
                line=dict(color="#e377c2", width=1.5),
                hovertemplate="<b>Power</b><br>%{x|%Y-%m-%d %H:%M}<br>%{y:.0f} W<extra></extra>",
                showlegend=False,
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
        s["cum_cost_pcss"] = s["cum_kwh"] * config.PCSS_FLAT_RATE
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
                showlegend=False,
            ),
            row=4, col=2,
        )

    # ----- Row 5 col 1: Runtime estimate curve + current point -----
    w_axis = np.linspace(0, 1300, 200)
    # np.interp matches estimate_runtime() over [0, 1300] (w<=0 maps to the
    # first curve point either way) — vectorized, so no 200-iteration loop.
    rt_axis = np.interp(w_axis, config.RUNTIME_CURVE_W, config.RUNTIME_CURVE_MIN)
    fig.add_trace(
        go.Scatter(
            x=w_axis, y=rt_axis, mode="lines",
            name="Runtime curve",
            line=dict(color="#8c564b", width=2),
            hovertemplate="%{x:.0f} W → %{y:.1f} min<extra></extra>",
            showlegend=False,
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
                go.Histogram(x=deltas, nbinsx=30, marker_color="#7f7f7f", name="Intervals",
                             showlegend=False),
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
                showlegend=False,
            ),
            row=6, col=2,
        )
        for d, lbl in [(30, "1 mo"), (90, "3 mo"), (180, "6 mo"), (365, "1 yr")]:
            v = (dl_stats["daily_bytes"] * d) / 1024
            # Use row/col so Plotly resolves the correct axis refs. The
            # secondary_y on row 4 col 1 adds an extra y-axis, which shifts
            # this panel's data onto y13 — hardcoding "y12" anchored the
            # annotations to a data-less axis and pushed them off-scale.
            fig.add_annotation(
                x=d, y=v, row=6, col=2,
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
        rows.append([f"Cost (PCSS flat ₡{config.PCSS_FLAT_RATE:g})", fmt_crc(energy_summary["total_cost_pcss"])])
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
    rows.append([f"DataLog gaps (>{config.DATALOG_EXPECTED_INTERVAL_MIN * 2:.0f} min)", f"{len(gaps)}"])

    fig.add_trace(
        go.Table(
            header=dict(values=["Metric", "Value"],
                        fill_color="#444", font=dict(color="white"), align="left"),
            cells=dict(values=list(zip(*rows, strict=False)) if rows else [(), ()],
                       align="left", height=22),
        ),
        row=7, col=2,
    )

    # Axes labels
    fig.update_xaxes(title_text="Time", row=1, col=1)
    fig.update_yaxes(title_text="Volts", row=1, col=1)
    fig.update_xaxes(title_text="Time", row=1, col=2)
    fig.update_yaxes(title_text="Volts", row=1, col=2)
    fig.update_xaxes(title_text="Time", row=2, col=1)
    fig.update_yaxes(title_text="%", row=2, col=1)
    fig.update_xaxes(title_text="Time", row=2, col=2)
    fig.update_yaxes(title_text="%", row=2, col=2)
    fig.update_xaxes(title_text="Time", row=3, col=1)
    fig.update_yaxes(title_text="W", row=3, col=1)
    fig.update_xaxes(title_text="Hour", row=3, col=2)
    fig.update_yaxes(title_text="Date", row=3, col=2)
    fig.update_xaxes(title_text="Time", row=4, col=1)
    fig.update_yaxes(title_text="kWh (cum.)", row=4, col=1, secondary_y=False)
    fig.update_yaxes(title_text="CRC (cum.)", row=4, col=1, secondary_y=True)
    fig.update_xaxes(title_text="Date", row=4, col=2)
    fig.update_yaxes(title_text="kWh", row=4, col=2)
    fig.update_xaxes(title_text="Power (W)", row=5, col=1)
    fig.update_yaxes(title_text="Runtime (min)", row=5, col=1)
    fig.update_xaxes(title_text="Seconds", row=5, col=2)
    fig.update_yaxes(title_text="Count", row=5, col=2)
    fig.update_xaxes(title_text="Snapshot time", row=6, col=1)
    fig.update_yaxes(title_text="KB", row=6, col=1)
    fig.update_xaxes(title_text="Days from today", row=6, col=2)
    fig.update_yaxes(title_text="KB", row=6, col=2)

    fig.update_layout(
        title=dict(
            text=f"<b>PowerChute UPS Dashboard</b> — {datetime.now():%Y-%m-%d %H:%M:%S}",
            x=0.5, xanchor="center", y=0.992, yanchor="top", yref="container",
            font=dict(size=22),
        ),
        height=2400,
        # Tall top margin reserves a band for the title + the horizontal legend.
        # The legend is anchored just below the title (yanchor="bottom", growing
        # upward), so even when it wraps to two rows it stays inside this band
        # instead of climbing into the title. Single-trace panels are kept off
        # the legend (show_legend=False) so it stays short. The play/pause
        # overlays are positioned per-panel in JS from the real axis domains, so
        # they adapt to this margin automatically.
        margin=dict(b=80, t=175, l=60, r=60),
        showlegend=True,
        template="plotly_white",
        hovermode="closest",
        legend=dict(orientation="h", y=1.028, x=0.5,
                    xanchor="center", yanchor="bottom",
                    font=dict(size=10), bgcolor="rgba(255,255,255,0.7)",
                    bordercolor="#d0d0d0", borderwidth=1),
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
