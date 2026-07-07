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
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from pcss import config
from pcss.common import fmt_age_hours, fmt_bytes, fmt_crc
from pcss.eventlog import EVENT_DEFAULT_VISIBLE, categorize_event
from pcss.stats import (
    _billing_period_bounds,
    battery_replace_projection,
    detect_baseline_deviations,
    detect_self_tests,
    estimate_runtime,
    forecast_period_cost,
    grid_quality_trend,
    weekday_weekend_profiles,
)

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

# Spanish translations, keyed by the English source string ([dashboard]
# language = "es"). A key missing here falls back to English, so a new
# string can ship untranslated without breaking the page. Number and date
# formatting stay en-US in every language: the CSV export and the payload
# are machine-standard, and the tooltips follow them for consistency.
_STRINGS_ES = {
    "UPS MONITOR": "MONITOR UPS",
    "Power Quality": "Calidad de energía",
    "line · load · draw": "línea · carga · consumo",
    "Battery Health": "Salud de la batería",
    "voltage · charge · runtime": "voltaje · carga · autonomía",
    "Energy & Cost": "Energía y costo",
    "Logs & Storage": "Registros y almacenamiento",
    "Reference": "Referencia",
    "statistics · latest readings": "estadísticas · últimas lecturas",
    "All": "Todo",
    "Line Voltage": "Voltaje de línea",
    "UPS Load": "Carga del UPS",
    "Power Draw": "Consumo de energía",
    "Hourly Power Map": "Mapa horario de potencia",
    "mean W": "W medio",
    "Battery Voltage": "Voltaje de batería",
    "Battery Charge": "Carga de batería",
    "% capacity": "% de capacidad",
    "Estimated Runtime": "Autonomía estimada",
    "runtime curve": "curva de autonomía",
    "measured": "medido",
    "measured from": "medido a partir de",
    "discharge": "descarga",
    "discharges": "descargas",
    "not enough discharge data yet": "aún no hay suficientes datos de descarga",
    "Cumulative Energy & Cost": "Energía y costo acumulados",
    "Daily Energy": "Energía diaria",
    "kWh / day": "kWh / día",
    "Period Comparison": "Comparación de períodos",
    "cumulative kWh · same day offset": "kWh acumulado · mismo día del período",
    "Weekday vs Weekend": "Entre semana vs fin de semana",
    "mean W by hour": "W medio por hora",
    "Log File Growth": "Crecimiento de registros",
    "DataLog Projection": "Proyección del DataLog",
    "current rate": "ritmo actual",
    "Sample Cadence": "Cadencia de muestreo",
    "interval min": "intervalo en min",
    "Per-metric Statistics": "Estadísticas por métrica",
    "Latest Readings & Run Summary": "Últimas lecturas y resumen",
    "Est. Runtime": "Autonomía est.",
    "no data": "sin datos",
    "no data in the analyzed window": "sin datos en la ventana analizada",
    "WARN": "AVISO",
    "ALERT": "ALERTA",
    "LIVE": "EN VIVO",
    "All systems nominal": "Sistemas en estado nominal",
    "Advisory": "Aviso",
    "Attention needed": "Atención necesaria",
    "Multiple alerts": "Varias alertas",
    "anomaly": "anomalía",
    "anomalies": "anomalías",
    "on-battery": "en batería",
    "high-load": "carga alta",
    "gap": "hueco",
    "gaps": "huecos",
    "in window": "en la ventana",
    "aging": "envejeciendo",
    "stable": "estable",
    "trend": "tendencia",
    "reading": "lectura",
    "8h mean": "media 8h",
    "weekday": "entre semana",
    "weekend": "fin de semana",
    "projected": "proyectado",
    "runtime": "autonomía",
    "energy": "energía",
    "intervals": "intervalos",
    "data": "datos",
    "event": "eventos",
    "mean power": "potencia media",
    "last sample": "última muestra",
    "Latest sample": "Última muestra",
    "Energy": "Energía",
    "Total energy": "Energía total",
    "Cost (Coopesantos tiered)": "Costo (Coopesantos escalonado)",
    "CO₂ emitted": "CO₂ emitido",
    "Span": "Período",
    "Samples (5-min)": "Muestras (5 min)",
    "Cross-validation": "Validación cruzada",
    "Files": "Archivos",
    "Growth": "Crecimiento",
    "Per hour": "Por hora",
    "Per day": "Por día",
    "Snapshots": "Capturas",
    "Anomalies": "Anomalías",
    "Voltage out-of-range": "Voltaje fuera de rango",
    "On-battery episodes (at cadence)": "Episodios en batería (según cadencia)",
    "Sustained high-load episodes": "Episodios de carga alta sostenida",
    "DataLog": "DataLog",
    "read-only analytics snapshot": "captura analítica de solo lectura",
    "Charts rendered as inline SVG — no chart library, fully offline.":
        "Gráficos SVG generados en línea — sin biblioteca de gráficos, totalmente sin conexión.",
    "samples": "muestras",
    "days": "días",
    "day": "día",
    "no rows": "sin filas",
    "envelope": "rango",
    "at": "a",
    "5-min sample": "muestra de 5 min",
    "metric(s) outside normal range": "métrica(s) fuera del rango normal",
    "metric(s) near limits": "métrica(s) cerca del límite",
    "battery replace": "reemplazo de batería",
    "battery trend": "tendencia de batería",
    "battery age": "antigüedad de la batería",
    "since": "desde",
    "replace": "reemplazo",
    "crosses": "cruza",
    "not enough history to project replace-by":
        "historial insuficiente para proyectar el reemplazo",
    "self-test": "autoprueba",
    "self-tests": "autopruebas",
    "median sag": "caída media",
    "nominal": "nominal",
    "high-load episode": "episodio de carga alta",
    "high-load episodes": "episodios de carga alta",
    "-min cadence": "cadencia en min",
    "Cost": "Costo",
    "DataLog gaps": "Huecos del DataLog",
    "Events": "Eventos",
    "Parsed events": "Eventos analizados",
    "On-battery (EventLog)": "En batería (EventLog)",
    "Last event": "Último evento",
    "last sample {t} ago": "última muestra hace {t}",
    "No data in the analyzed window.": "Sin datos en la ventana analizada.",
    "Latest": "Último",
    "minimum": "mínimo",
    "maximum": "máximo",
    "From": "Desde",
    "to": "hasta",
    "bars": "barras",
    "Largest": "Mayor",
    "days by 24 hours": "días por 24 horas",
    "Mean power from": "Potencia media de",
    "Generated": "Generado",
    "theme": "tema",
    "chart": "gráfico",
    "Data feed stale": "Fuente de datos desactualizada",
    "no new samples in": "sin muestras nuevas en",
    "Billing Periods": "Períodos de facturación",
    "Period": "Período",
    "Tiered": "Escalonado",
    "Notes": "Notas",
    "partial": "parcial",
    "current rates": "tarifas actuales",
    "rates from": "tarifas desde",
    "not enough of the period recorded yet": "aún no hay suficientes datos de este período",
    "at the current pace": "al ritmo actual",
    "by": "para",
    "tier limit already exceeded": "límite de franja ya superado",
    "tier crosses": "cruza el límite de franja",
    "Bill Reconciliation": "Conciliación de facturas",
    "UPS-metered share of the billed consumption":
        "proporción medida por el UPS del consumo facturado",
    "Share": "Proporción",
    "day deviates from the recorded baseline":
        "día se desvía de la referencia registrada",
    "days deviate from the recorded baseline":
        "días se desvían de la referencia registrada",
    "Grid Quality Trend": "Tendencia de calidad de red",
    "events visible at the {n}-min sampling cadence; short events between samples are missed":
        "eventos visibles a la cadencia de muestreo de {n} min; "
        "los eventos cortos entre muestras no se detectan",
    "Month": "Mes",
    "Sags": "Caídas",
    "Swells": "Subidas",
    "Interruptions": "Interrupciones",
    "Recorded days": "Días registrados",
    "Events/day": "Eventos/día",
    "Mean sag depth": "Profundidad media de caída",
    "Mean swell depth": "Profundidad media de subida",
    "Worst event": "Peor evento",
    "sag": "caída",
    "swell": "subida",
    "Showing the last {n} days; older history remains in output/archive/.":
        "Mostrando los últimos {n} días; el historial anterior permanece en output/archive/.",
    "Event Timeline": "Cronología de eventos",
    "events by category": "eventos por categoría",
    "categories": "categorías",
    "previous": "anterior",
    "quarter": "trimestre",
    "Compare against the previous period": "Comparar con el período anterior",
    "Compare against the same period one quarter ago":
        "Comparar con el mismo período de hace un trimestre",
    "Compare against a specific period": "Comparar con un período específico",
    "pick a period…": "elegir un período…",
    "not enough history for this option": "no hay suficiente historial para esta opción",
    "Power": "Corriente",
    "Battery": "Batería",
    "Shutdown": "Apagado",
    "Communication": "Comunicación",
    "Monitoring": "Monitoreo",
    "Other": "Otros",
    "inspecting": "inspeccionando",
}


def _L(s: str) -> str:
    """Translate one UI string when the dashboard language is Spanish."""
    if config.DASHBOARD_LANGUAGE == "es":
        return _STRINGS_ES.get(s, s)
    return s


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


def _episode_spans(eps: pd.DataFrame) -> list[list[int]]:
    """On-battery episodes as [start_ms, end_ms] pairs (same shape as gaps)."""
    if eps is None or eps.empty:
        return []
    return [[a, b] for a, b in zip(_ms_list(eps["start"]), _ms_list(eps["end"]), strict=True)]


def _annotation_markers(annotations: pd.DataFrame | None) -> list[dict]:
    """Battery lifecycle annotations (roadmap item 26) as a top-level payload
    list: one {x, label} dict per entry, x being the epoch-ms midnight of the
    entry's date under the same naive-local-as-UTC encoding every timestamp
    on this payload uses. charts.js draws each as a dashed vertical marker on
    every time-axis panel. A blank label (the loader allows one) falls back
    to the entry's kind so the marker is never unlabeled."""
    if annotations is None or annotations.empty:
        return []
    xs = _ms_list(pd.to_datetime(annotations["date"]))
    return [{"x": x, "label": (lbl or kind)} for x, kind, lbl in
            zip(xs, annotations["kind"], annotations["label"], strict=True)]


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
        "series": [_line_series(_L("Line Voltage"), pal["blue"], *xy, width=1.8, fill=True)],
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
        "series": [_line_series(_L("UPS Load"), pal["amber"], *xy, width=1.6, fill=True)],
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
    # Real dates (epoch-ms midnights) alongside the display labels, so the
    # crosshair on a time panel can find the hovered day's row.
    day_keys = _ms_list(pd.to_datetime(list(pivot.index)))
    return {"kind": "heatmap", "unit": "W", "z": z, "days": days, "dayKeys": day_keys,
            "vb": [460, 250]}


def _panel_bv(datalog_df, pal) -> tuple[dict | None, float | None]:
    """Battery voltage panel (raw + rolling mean + linear trend). Returns
    (panel, slope_v_per_day) — the slope also feeds the card subtitle and
    the health pill."""
    xy = _xy(datalog_df, "Battery Voltage")
    if xy is None:
        return None, None
    series = [_line_series(_L("reading"), pal["teal"], *xy, width=1.2, opacity=0.5)]
    slope_day = None
    bv = datalog_df.dropna(subset=["Battery Voltage"])
    if len(bv) >= 5:
        # Rolling mean (~8h window at the 20-min default cadence) damps noise.
        roll = bv["Battery Voltage"].rolling(window=24, min_periods=3).mean()
        rmask = roll.notna()
        series.append(_line_series(_L("8h mean"), pal["blue"],
                                   _ms_list(bv.loc[rmask, "ts"]), _vals(roll[rmask]), width=2))
        # Linear fit of voltage against days elapsed: the slope is the
        # degradation rate — a slow downward slope is an aging battery.
        ts = bv["ts"]
        days = (ts - ts.iloc[0]).dt.total_seconds().to_numpy() / 86400.0
        volts = bv["Battery Voltage"].to_numpy(dtype=float)
        slope, intercept = np.polyfit(days, volts, 1)
        slope_day = float(slope)
        x0, x1 = _ms_list(ts.iloc[[0, -1]])
        series.append(_line_series(_L("trend"), pal["amber"], [x0, x1],
                                   _vals([intercept, intercept + slope * days[-1]]),
                                   width=2, dash="6 4"))
    panel = {
        "kind": "line", "unit": "V", "dec": 2, "sync": True, "gaps": True,
        "legend": True, "vb": [820, 250], "series": series,
    }
    return panel, slope_day


def _panel_bc(datalog_df, pal, self_tests: pd.DataFrame | None = None) -> dict | None:
    xy = _xy(datalog_df, "Battery Capacity")
    if xy is None:
        return None
    lo = min(min(xy[1]), 96)
    # Self-test markers (roadmap item 18): one dot per detected test, at the
    # nearest Battery Capacity reading — the same marker shape the Line
    # Voltage card uses for anomalies, reused here rather than a new type.
    markers: list[dict] = []
    if self_tests is not None and not self_tests.empty:
        bc = datalog_df.dropna(subset=["Battery Capacity"])[["ts", "Battery Capacity"]].sort_values("ts")
        test_ts = self_tests[["ts"]].dropna().sort_values("ts")
        if not bc.empty and not test_ts.empty:
            merged = pd.merge_asof(test_ts, bc, on="ts", direction="nearest")
            markers = [{"x": x, "y": y, "type": "dot"} for x, y in
                       zip(_ms_list(merged["ts"]), _vals(merged["Battery Capacity"]), strict=True)]
    return {
        "kind": "line", "unit": "%", "dec": 0, "sync": True, "gaps": True, "vb": [460, 250],
        "series": [_line_series("Battery %", pal["green"], *xy, width=1.8, fill=True)],
        "yDomain": [max(0.0, lo - 4), 103],
        "markers": markers,
    }


def _self_test_sub(n_tests: int, median_sag_v: float | None) -> str:
    """Battery Charge card subtitle (roadmap item 18): the count of detected
    self-tests and, when known, their median voltage sag. With none detected
    the card keeps its original "% capacity" wording rather than announcing
    a "0 self-tests" that nobody asked about."""
    if n_tests == 0:
        return _L("% capacity")
    bits = [_count(n_tests, _L("self-test"), _L("self-tests"))]
    if median_sag_v is not None:
        bits.append(f"{_L('median sag')} {median_sag_v:.2f} V")
    return " · ".join(bits)


def _panel_rt(energy_df, pal, calibration: dict | None = None) -> tuple[dict | None, float | None, float | None]:
    """Runtime curve + the latest operating point. Returns (panel, latest_w,
    latest_runtime_min); the latter two also feed the KPI row.

    ``calibration`` is the result of ``pcss.stats.calibrate_runtime_curve``
    (roadmap item 16): once enough discharges have been observed
    (``status == "calibrated"``), a second, measured series rides alongside
    the configured curve — visually distinct and legend-toggleable like any
    other series. Below the evidence floor (or with no calibration at all),
    only the configured curve is drawn; the honest note about discharge
    count lives in the card subtitle built by ``build_dashboard``, not here.
    """
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
    series = [_line_series(_L("runtime"), pal["violet"], _vals(w_axis, 1), _vals(rt_axis, 1),
                           width=2, fill=True)]
    if calibration and calibration.get("status") == "calibrated" and calibration.get("watts"):
        series.append(_line_series(_L("measured"), pal["teal"],
                                   _vals(calibration["watts"], 1),
                                   _vals(calibration["minutes"], 1), width=2, dash="5 4"))
    panel = {
        "kind": "line", "unit": "min", "dec": 1, "xkind": "linear", "xunit": "W",
        "legend": True, "vb": [460, 250], "xDomain": [0, w_max],
        "series": series,
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


def _panel_daily(energy_summary, pal, flagged: pd.DataFrame | None = None) -> dict | None:
    """Daily Energy bars, with the baseline-deviation flags (roadmap item
    19) riding the same panel rather than a new one: a flagged day's bar
    takes the amber accent color (the ``d.color`` per-bar override
    ``pcss/charts.js`` already renders, the same convention
    ``_panel_cad`` uses to highlight one bin), and the panel also carries a
    ``markers`` list (bar index, the bar's own kWh as ``y``, ``type: "dot"``,
    and a deviation-percent label) that ``renderBar`` in ``pcss/charts.js``
    draws as a small glyph above the flagged bar, mirroring the ``lv``/``bc``
    marker-list shape so the same marker-drawing code path serves every
    panel.

    ``flagged`` is the ``flagged`` frame from
    ``pcss.stats.detect_baseline_deviations`` (columns ``date``,
    ``day_type``, ``deviation_pct``), or ``None``/empty when nothing is
    flagged.
    """
    if not energy_summary or energy_summary.get("daily") is None or energy_summary["daily"].empty:
        return None
    d = energy_summary["daily"]
    deviation_by_date: dict = {}
    if flagged is not None and not flagged.empty:
        deviation_by_date = dict(zip(flagged["date"], flagged["deviation_pct"], strict=True))
    data = []
    markers = []
    for i, (dt, k) in enumerate(zip(d["date"], d["kwh"], strict=True)):
        kwh = round(float(k), 4)
        item = {"label": f"{dt.month}/{dt.day}", "y": kwh}
        pct = deviation_by_date.get(dt)
        if pct is not None:
            item["color"] = pal["amber"]
            markers.append({"x": i, "y": kwh, "type": "dot", "color": pal["amber"],
                             "label": f"+{pct:.0f}%"})
        data.append(item)
    return {
        "kind": "bar", "unit": "kWh", "dec": 2, "vb": [460, 250], "barName": _L("energy"),
        "color": pal["blue"],
        "data": data,
        "markers": markers,
    }


def _panel_cmp(energy_summary, pal) -> dict | None:
    """Cumulative kWh against day-offset-in-period, the current billing
    period overlaid on one selected baseline — answers "is this period
    normal?". Needs at least two periods of energylog history.

    Roadmap item 22 (selectable comparison periods): the payload carries
    every period, not only the last two, so the client can overlay the
    current period against the previous one (the default), the same period
    one quarter back, or any period picked from a list — charts.js rebuilds
    the rendered pair from ``periods`` on selection instead of asking the
    server to rebuild the page. A year of monthly periods at hourly
    resolution is only a few thousand points total, which is exactly the
    scale the roadmap entry itself judged "fine at hourly resolution" — so,
    deliberately, no server-side decimation is applied here.
    """
    if not energy_summary or "samples" not in energy_summary:
        return None
    s = energy_summary["samples"]
    labels = list(dict.fromkeys(s["month"]))    # in time order, deduplicated
    if len(labels) < 2:
        return None
    start_day = int(getattr(config, "BILLING_CYCLE_START_DAY", 1))
    monthly = energy_summary.get("monthly")
    partial_by_label: dict[str, bool] = {}
    if monthly is not None and "partial" in monthly.columns:
        partial_by_label = dict(
            zip(monthly["month"].astype(str), monthly["partial"], strict=True))

    periods: list[dict[str, object]] = []
    xy_by_index: list[tuple[list[float], list[float]]] = []
    for label in labels:
        part = s[s["month"] == label]
        p_start, _ = _billing_period_bounds(str(label), start_day)
        hourly = part.set_index("ts")["kwh"].resample("1h").sum().cumsum()
        x = [round((idx - p_start).total_seconds() / 86400, 3) for idx in hourly.index]
        y = _vals(hourly, 4)
        periods.append({
            "label": str(label),
            "partial": bool(partial_by_label.get(str(label), False)),
            "x": x,
            "y": y,
        })
        xy_by_index.append((x, y))

    # The initial render (before any client-side selection) keeps today's
    # behavior exactly: current period overlaid on the immediately previous
    # one, styled baseline-faint-dashed versus current-teal-solid.
    prev_x, prev_y = xy_by_index[-2]
    cur_x, cur_y = xy_by_index[-1]
    series = [
        _line_series(str(labels[-2]), pal["faint"], prev_x, prev_y, width=1.6, dash="5 4"),
        _line_series(str(labels[-1]), pal["teal"], cur_x, cur_y, width=2.2),
    ]
    return {
        "kind": "line", "unit": "kWh", "dec": 2, "xkind": "linear", "xunit": "d",
        "legend": True, "vb": [560, 250], "series": series,
        "periods": periods,
    }


def _cmp_pills_html(n_periods: int) -> str:
    """Pills for the two named comparison modes ("previous period", "same
    period one quarter back"). The pick-a-period select lives separately in
    the card tools tray (``_cmp_select_html``) — see roadmap item 22.

    "Quarter" needs a period three cycles back; with fewer than four periods
    in the payload it renders disabled instead of silently comparing against
    the wrong thing."""
    if n_periods < 4:
        quarter_title = _L("not enough history for this option")
        quarter_attrs = f' disabled title="{_esc(quarter_title)}"'
    else:
        quarter_title = _L("Compare against the same period one quarter ago")
        quarter_attrs = f' title="{_esc(quarter_title)}"'
    return (
        '<span class="cmp-pills">'
        f'<button class="cmp-pill" data-mode="previous" type="button" '
        f'title="{_esc(_L("Compare against the previous period"))}">{_esc(_L("previous"))}</button>'
        f'<button class="cmp-pill" data-mode="quarter" type="button"{quarter_attrs}>'
        f'{_esc(_L("quarter"))}</button>'
        '</span>'
    )


def _cmp_select_html(periods: list[dict]) -> str:
    """The pick-a-period control: every period except the current one (the
    last, always shown) is a valid baseline to list."""
    if len(periods) < 2:
        return ""
    label = _esc(_L("Compare against a specific period"))
    options = [f'<option value="" disabled selected>{_esc(_L("pick a period…"))}</option>']
    for p in periods[:-1]:
        text = p["label"] + (f" ({_L('partial')})" if p.get("partial") else "")
        options.append(f'<option value="{_esc(p["label"])}">{_esc(text)}</option>')
    return (f'<select class="cmp-period-select" data-panel="cmp" '
            f'title="{label}" aria-label="{label}">{"".join(options)}</select>')


def _forecast_sub(forecast: dict | None) -> str:
    """Word the end-of-period cost forecast (pcss.stats.forecast_period_cost)
    for the Period Comparison card subtitle. Always speaks as a projection
    ("projected", "at the current pace") — the honesty rule from roadmap
    item 27 — except for the "tier limit already exceeded" fact, which
    describes kWh already recorded this period rather than a future date."""
    if not forecast:
        forecast = {}
    if forecast.get("status") != "projected":
        ev = forecast.get("evidence_days", 0)
        need = forecast.get("min_days", config.FORECAST_MIN_DAYS)
        return f"{_L('not enough of the period recorded yet')} ({ev}/{need:.0f} {_L('days')})"
    bits = [f"{_L('projected')} ≈{forecast['projected_kwh']:.1f} kWh {_L('by')} "
            f"{forecast['period_end']:%Y-%m-%d}"]
    if forecast.get("already_crossed"):
        bits.append(_L("tier limit already exceeded"))
    elif forecast.get("tier_cross_date") is not None:
        bits.append(f"{_L('tier crosses')} ~{forecast['tier_cross_date']:%Y-%m-%d}")
    else:
        bits.append(_L("at the current pace"))
    return " · ".join(bits)


def _panel_wk(energy_df, pal) -> dict | None:
    """Mean power by hour of day, weekday against weekend.

    The profile math itself lives in ``pcss.stats.weekday_weekend_profiles``,
    shared with the baseline-deviation detector (roadmap item 19) so the two
    views of "what a normal day looks like" cannot drift apart.
    """
    profiles = weekday_weekend_profiles(energy_df)
    series = []
    for name, color in [("weekday", pal["blue"]), ("weekend", pal["green"])]:
        prof = profiles.get(name)
        if prof is None:
            continue
        series.append(_line_series(_L(name), color, [int(h) for h in prof.index],
                                   _vals(prof, 1), width=2))
    if not series:
        return None
    return {
        "kind": "line", "unit": "W", "dec": 0, "xkind": "linear", "xunit": "h",
        "legend": True, "vb": [460, 250], "xDomain": [0, 23], "series": series,
    }


def _panel_growth(hist, pal) -> dict | None:
    if hist is None or hist.empty:
        return None
    x = _ms_list(hist["timestamp"])
    series = []
    for col, label, color, width in [
        ("total_bytes", "total", pal["green"], 2),
        ("datalog_bytes", _L("data"), pal["blue"], 1.8),
        ("energylog_bytes", _L("energy"), pal["amber"], 1.5),
        ("eventlog_bytes", _L("event"), pal["teal"], 1.5),
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
        "series": [_line_series(_L("projected"), pal["blue"], days,
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
        "kind": "bar", "unit": _L("samples"), "dec": 0, "vb": [460, 250],
        "barName": _L("intervals"),
        "data": [{"label": lbl, "y": int(counts.get(lbl, 0)),
                  "color": pal["teal"] if i == 1 else pal["faint"]}
                 for i, lbl in enumerate(labels)],
    }


# Event timeline categories (roadmap item 20): the display order and the
# English category label localized through _L. The palette color per category
# is resolved from the passed palette in _panel_ev. Kept beside the panel
# builder so the payload the renderer consumes carries localized row labels,
# exactly as every other panel bakes its localized series names.
_EVENT_CATEGORY_ORDER = ("power", "battery", "shutdown", "communication", "monitoring", "other")
_EVENT_CATEGORY_LABEL = {
    "power": "Power", "battery": "Battery", "shutdown": "Shutdown",
    "communication": "Communication", "monitoring": "Monitoring", "other": "Other",
}


def _panel_ev(events_df, pal) -> dict | None:
    """Event timeline: one categorical row per event category, one dot per
    occurrence, time on the x axis (roadmap item 20).

    Each event crosses the boundary as an epoch-ms integer (the naive local
    wall-clock encoded as if UTC, the shared timezone contract) carrying its
    category, ObjectId, and resolved name, so the client renders the dot, the
    hover tooltip, and the machine-standard CSV export from this one payload.
    Rows ship the power, battery, and shutdown categories visible and the
    communication and monitoring housekeeping hidden — about 95 percent of
    recorded events are that churn (the roadmap noise point) — and the legend
    toggles them. A category with no events in the frame gets no row. Returns
    None when there are no events, so the client-side empty-state note
    renders.
    """
    if events_df is None or events_df.empty:
        return None
    cat_color = {
        "power": pal["red"], "battery": pal["amber"], "shutdown": pal["violet"],
        "communication": pal["blue"], "monitoring": pal["teal"], "other": pal["faint"],
    }
    xs = _ms_list(events_df["ts"])
    cats = [categorize_event(str(oid)) for oid in events_df["oid"]]
    events = [{"x": x, "cat": c, "oid": str(oid), "name": str(name)}
              for x, c, oid, name in
              zip(xs, cats, events_df["oid"], events_df["name"], strict=True)]
    present = set(cats)
    rows = [{"cat": c, "label": _L(_EVENT_CATEGORY_LABEL[c]), "color": cat_color[c],
             "visible": c in EVENT_DEFAULT_VISIBLE}
            for c in _EVENT_CATEGORY_ORDER if c in present]
    return {"kind": "events", "vb": [820, 250], "rows": rows, "events": events}


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
            "status": _L(_SEV_LABEL[sev]), "color": pal[_SEV_COLOR[sev]],
        })
        sparks.append(spark)
        if sev != "info":
            sevs.append(sev)

    latest = datalog_df.iloc[-1] if not datalog_df.empty else None

    v = None if latest is None or "Line Voltage" not in datalog_df.columns else latest["Line Voltage"]
    if v is not None and pd.notna(v):
        sev = "ok" if config.VOLTAGE_NORMAL_LOW <= v <= config.VOLTAGE_NORMAL_HIGH else "crit"
        add(_L("Line Voltage"), f"{v:.1f}", "V",
            f"{_L('envelope')} {config.VOLTAGE_NORMAL_LOW:g}–{config.VOLTAGE_NORMAL_HIGH:g} V",
            sev, _spark(datalog_df, "Line Voltage", pal["blue"]))
    else:
        add(_L("Line Voltage"), "—", "", _L("no data"), "info", None)

    u = None if latest is None or "UPS Load" not in datalog_df.columns else latest["UPS Load"]
    if u is not None and pd.notna(u):
        sev = "ok" if u < 0.875 * config.HIGH_LOAD_PCT else ("warn" if u < config.HIGH_LOAD_PCT else "crit")
        add(_L("UPS Load"), f"{u:.0f}", "%", f"{_L('high-load')} > {config.HIGH_LOAD_PCT:g}%",
            sev, _spark(datalog_df, "UPS Load", pal["amber"]))
    else:
        add(_L("UPS Load"), "—", "", _L("no data"), "info", None)

    c = None if latest is None or "Battery Capacity" not in datalog_df.columns else latest["Battery Capacity"]
    if c is not None and pd.notna(c):
        sev = ("ok" if c >= config.BATTERY_CHARGE_WARN_PCT
               else ("warn" if c >= config.BATTERY_CHARGE_CRIT_PCT else "crit"))
        bv = latest.get("Battery Voltage") if latest is not None else None
        sub = f"{bv:.1f} V bus" if bv is not None and pd.notna(bv) else _L("% capacity")
        add(_L("Battery Charge"), f"{c:.0f}", "%", sub, sev,
            _spark(datalog_df, "Battery Capacity", pal["green"]))
    else:
        add(_L("Battery Charge"), "—", "", _L("no data"), "info", None)

    if latest_rt is not None:
        sev = ("ok" if latest_rt >= config.RUNTIME_WARN_MIN
               else ("warn" if latest_rt >= config.RUNTIME_CRIT_MIN else "crit"))
        rt_spark = _spark(energy_df, "power_w", pal["teal"],
                          transform=lambda w: np.interp(w, config.RUNTIME_CURVE_W,
                                                        config.RUNTIME_CURVE_MIN))
        add(_L("Est. Runtime"), f"{latest_rt:.0f}", "min",
            f"{_L('at')} {latest_w:.0f} W", sev, rt_spark)
    else:
        add(_L("Est. Runtime"), "—", "", _L("no data"), "info", None)

    if latest_w is not None:
        add(_L("Power Draw"), f"{latest_w:.0f}", "W", _L("5-min sample"), "info",
            _spark(energy_df, "power_w", pal["blue"]))
    else:
        add(_L("Power Draw"), "—", "", _L("no data"), "info", None)

    return cards, sparks, sevs


def _build_health(sevs, bv_slope, voltage_anomalies, high_load_episodes, gaps, pal,
                  on_battery=None, battery=None, staleness=None) -> dict:
    n_crit = sevs.count("crit")
    n_warn = sevs.count("warn")
    n_ob = 0 if on_battery is None else len(on_battery)
    ob_bit = f"{n_ob} {_L('on-battery')} · " if n_ob else ""
    counts = (f"{_count(len(voltage_anomalies), _L('anomaly'), _L('anomalies'))} · "
              f"{ob_bit}"
              f"{len(high_load_episodes)} {_L('high-load')} · "
              f"{_count(len(gaps), _L('gap'), _L('gaps'))} {_L('in window')}")

    # "Stale now" (the feed itself has gone quiet) is a different fact from
    # the historical DataLog gaps folded into `counts` above, so it gets its
    # own clause naming the concrete age instead of reusing the gap wording.
    stale_level = (staleness or {}).get("level", "fresh")
    stale_bit = (f"{_L('no new samples in')} {fmt_age_hours(staleness['age_hours'])} · "
                if stale_level in ("warn", "crit") else "")

    if n_crit or stale_level == "crit":
        if stale_level == "crit" and not n_crit:
            label = _L("Data feed stale")
        else:
            label = _L("Multiple alerts") if n_crit > 1 else _L("Attention needed")
        color = pal["red"]
        if n_crit > 0:
            metric_bit = f"{n_crit + n_warn} {_L('metric(s) outside normal range')} · "
        elif n_warn > 0:
            metric_bit = f"{n_warn} {_L('metric(s) near limits')} · "
        else:
            metric_bit = ""
        sub = f"{stale_bit}{metric_bit}{counts}"
    elif n_warn or stale_level == "warn":
        label = _L("Data feed stale") if stale_level == "warn" and not n_warn else _L("Advisory")
        color = pal["amber"]
        metric_bit = f"{n_warn} {_L('metric(s) near limits')} · " if n_warn else ""
        sub = f"{stale_bit}{metric_bit}{counts}"
    else:
        label, color = _L("All systems nominal"), pal["green"]
        if battery and battery.get("status") == "projected":
            sub = f"{_L('battery replace')} ≈ {battery['replace_date']:%Y-%m} · {counts}"
        elif bv_slope is not None:
            sub = f"{_L('battery trend')} {bv_slope:+.4f} V/{_L('day')} · {counts}"
        else:
            sub = counts
    return {"label": label, "color": color, "sub": sub}


# ======================================================================
# Shell HTML
# ======================================================================
def _sr_text(spec) -> str:
    """Screen-reader summary for one panel (its aria-label text).

    The SVG chart is opaque to assistive technology, so the numbers a
    sighted reader takes from the chart — latest, minimum, maximum, span —
    are stated here, generated in Python where they already exist.
    """
    empty = _L("No data in the analyzed window.")
    if not spec:
        return empty
    unit = spec.get("unit", "")
    if spec.get("kind") == "line":
        s0 = (spec.get("series") or [{}])[0]
        y = s0.get("y") or []
        if not y:
            return empty
        txt = (f"{len(y)} {_L('samples')}. {_L('Latest')} {y[-1]:g} {unit}, "
               f"{_L('minimum')} {min(y):g} {unit}, {_L('maximum')} {max(y):g} {unit}.")
        if spec.get("xkind") != "linear" and s0.get("x"):
            t0 = datetime.fromtimestamp(s0["x"][0] / 1000, tz=UTC)
            t1 = datetime.fromtimestamp(s0["x"][-1] / 1000, tz=UTC)
            txt = f"{_L('From')} {t0:%Y-%m-%d %H:%M} {_L('to')} {t1:%Y-%m-%d %H:%M}. {txt}"
        return txt
    if spec.get("kind") == "bar":
        data = spec.get("data") or []
        if not data:
            return empty
        top = max(data, key=lambda d: d["y"])
        return f"{len(data)} {_L('bars')}. {_L('Largest')} {top['y']:g} {unit} ({top['label']})."
    if spec.get("kind") == "heatmap":
        z = [v for row in spec.get("z", []) for v in row if v is not None]
        if not z:
            return empty
        return (f"{len(spec.get('days', []))} {_L('days by 24 hours')}. "
                f"{_L('Mean power from')} {min(z):g} {_L('to')} {max(z):g} {unit}.")
    if spec.get("kind") == "events":
        evs = spec.get("events") or []
        if not evs:
            return empty
        xs = [e["x"] for e in evs]
        t0 = datetime.fromtimestamp(min(xs) / 1000, tz=UTC)
        t1 = datetime.fromtimestamp(max(xs) / 1000, tz=UTC)
        return (f"{len(evs)} {_L('Events')} · {len(spec.get('rows') or [])} "
                f"{_L('categories')}. {_L('From')} {t0:%Y-%m-%d %H:%M} "
                f"{_L('to')} {t1:%Y-%m-%d %H:%M}.")
    return empty


def _tools_html(key: str, zoomable: bool, anomaly_nav: int = 0, extra: str = "") -> str:
    anom = (f'<button class="tool-btn tool-anom" data-panel="{key}" '
            f'title="Jump to next anomaly">⚑ {anomaly_nav}</button>') if anomaly_nav else ""
    reset = (f'<button class="tool-btn tool-reset" data-panel="{key}" hidden '
             f'title="Reset zoom">reset</button>') if zoomable else ""
    return (f'<div class="card-tools">{anom}{reset}'
            f'<button class="tool-btn tool-png" data-panel="{key}" title="Export PNG">png</button>'
            f'<button class="tool-btn tool-csv" data-panel="{key}" title="Export CSV">csv</button>'
            f'<button class="tool-btn tool-expand" data-panel="{key}" title="Expand">⤢</button>'
            f'{extra}'
            f'</div>')


def _chart_card(key: str, span: int, title: str, sub: str, zoomable: bool,
                sub_color: str | None = None, sr_text: str = "",
                anomaly_nav: int = 0, inspectable: bool = False,
                header_extra: str = "", extra_tool: str = "") -> str:
    sub_style = f' style="color:{sub_color}"' if sub_color else ""
    aria = _esc(f"{title} {_L('chart')}. {sr_text}".strip())
    # The inspect-mode badge (roadmap item 21) only exists on line-kind
    # panels — the ones charts.js lets a screen-reader user walk sample by
    # sample with the arrow keys — and starts hidden; charts.js toggles it
    # (and the chart box's `is-inspecting` outline) while that mode is on.
    badge = (f'<span class="inspect-badge" data-panel="{key}" hidden>{_esc(_L("inspecting"))}</span>'
             if inspectable else "")
    return f"""
<div class="card chart-card s{span}">
  <div class="card-head">
    <div class="card-title">{_esc(title)}</div>
    <div class="card-side"><span class="card-sub"{sub_style}>{_esc(sub)}</span>{header_extra}{badge}{_tools_html(key, zoomable, anomaly_nav, extra_tool)}</div>
  </div>
  <div class="chart-box" id="panel-{key}" data-title="{_esc(title)}" role="img" tabindex="0" aria-label="{aria}"></div>
</div>"""


def _section_head(title: str, note: str, presets: bool = False) -> str:
    pills = ""
    if presets:
        pills = ('<span class="presets">'
                 f'<button class="preset-pill" data-days="all">{_esc(_L("All"))}</button>'
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


def _rate_tag_label(tag: str) -> str:
    """Localize a rate-tag string from config.tariff_rates_for ("current
    rates" or "rates from YYYY-MM-DD"). The date suffix itself stays
    unlocalized, like every other date and number on the dashboard."""
    prefix = "rates from "
    if tag.startswith(prefix):
        return f"{_L('rates from')} {tag[len(prefix):]}"
    return _L(tag)


def _periods_table_html(monthly: pd.DataFrame | None) -> str:
    """Per-billing-period cost breakdown — the same figures the console
    prints, tagged with which rates priced each period. Only rendered once
    [[tariff.history]] entries are configured (build_dashboard gates the
    whole card on that, so the page is unchanged without it)."""
    if monthly is None or monthly.empty or "rate_tag" not in monthly.columns:
        return f'<div class="chart-empty">{_esc(_L("no data in the analyzed window"))}</div>'
    head_cols = [_L("Period"), "kWh", "PCSS ₡", f"{_L('Tiered')} ₡", "CO₂ kg", _L("Notes")]
    head = "".join(f'<th class="{"tl" if i == 0 else "tr"}">{_esc(c)}</th>' for i, c in enumerate(head_cols))
    rows = []
    for month, kwh, cost_pcss, cost_tiered, co2_kg, partial, rate_tag in monthly[
        ["month", "kwh", "cost_pcss", "cost_tiered", "co2_kg", "partial", "rate_tag"]
    ].itertuples(index=False, name=None):
        notes = [_L("partial")] if partial else []
        notes.append(_rate_tag_label(rate_tag))
        cells = "".join([
            f'<td class="tl hi">{_esc(month)}</td>',
            f'<td class="tr">{kwh:.4f}</td>',
            f'<td class="tr">{cost_pcss:,.2f}</td>',
            f'<td class="tr">{cost_tiered:,.2f}</td>',
            f'<td class="tr">{co2_kg:.4f}</td>',
            f'<td class="tr">{_esc(" · ".join(notes))}</td>',
        ])
        rows.append(f"<tr>{cells}</tr>")
    return (f'<table class="stats-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _bills_table_html(reconciled: pd.DataFrame | None) -> str:
    """Bill reconciliation table (roadmap item 29): for each period where a
    user-entered bills.csv row lines up with the analyzer's own
    per-billing-period UPS kWh, the UPS-metered share of the billed
    consumption next to the bill's own implied rate and the tariff's
    effective rates for that period (config.tariff_rates_for, item 17,
    reused rather than duplicated). Only rendered once at least one bill
    reconciles (build_dashboard gates the whole block on that), so the page
    is unchanged for anyone who never uses bills.csv."""
    if reconciled is None or reconciled.empty:
        return f'<div class="chart-empty">{_esc(_L("no data in the analyzed window"))}</div>'
    head_cols = [_L("Period"), "UPS kWh", "Billed kWh", f"{_L('Share')} %",
                 f"{_L('Tiered')} ₡", "Billed ₡", "₡/kWh", _L("Notes")]
    head = "".join(f'<th class="{"tl" if i == 0 else "tr"}">{_esc(c)}</th>' for i, c in enumerate(head_cols))
    rows = []
    cols = ["period", "ups_kwh", "billed_kwh", "share_pct", "ups_cost_tiered",
            "billed_amount_crc", "implied_rate_crc_per_kwh", "tariff_low", "tariff_high",
            "rate_tag", "partial"]
    for (period, ups_kwh, billed_kwh, share_pct, ups_cost_tiered, billed_amount_crc,
         implied_rate, tariff_low, tariff_high, rate_tag, partial) in reconciled[
             cols].itertuples(index=False, name=None):
        notes = [_L("partial")] if partial else []
        notes.append(f"{_rate_tag_label(rate_tag)} (₡{tariff_low:g}/{tariff_high:g})")
        cells = "".join([
            f'<td class="tl hi">{_esc(period)}</td>',
            f'<td class="tr">{ups_kwh:.4f}</td>',
            f'<td class="tr">{billed_kwh:.4f}</td>',
            f'<td class="tr">{share_pct:.1f}</td>',
            f'<td class="tr">{ups_cost_tiered:,.2f}</td>',
            f'<td class="tr">{billed_amount_crc:,.2f}</td>',
            f'<td class="tr">{implied_rate:,.2f}</td>',
            f'<td class="tr">{_esc(" · ".join(notes))}</td>',
        ])
        rows.append(f"<tr>{cells}</tr>")
    return (f'<table class="stats-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _grid_quality_table_html(gq: pd.DataFrame | None) -> str:
    """Grid-quality trend table (roadmap item 28): one row per calendar
    month with the sag/swell/interruption counts, the per-recorded-day event
    rate (so a gap-heavy month reads honestly), the mean depth per direction,
    and the worst event. A direction with no events this month shows an em
    dash rather than a NaN. Only rendered once at least one month has data
    (build_dashboard gates the whole block on that), and the card subtitle
    carries the cadence-honesty wording — these are events visible at the
    sampling cadence, not all grid events."""
    if gq is None or gq.empty:
        return f'<div class="chart-empty">{_esc(_L("no data in the analyzed window"))}</div>'
    head_cols = [_L("Month"), _L("Sags"), _L("Swells"), _L("Interruptions"),
                 _L("Recorded days"), _L("Events/day"),
                 f"{_L('Mean sag depth')} V", f"{_L('Mean swell depth')} V",
                 _L("Worst event")]
    head = "".join(f'<th class="{"tl" if i == 0 else "tr"}">{_esc(c)}</th>'
                   for i, c in enumerate(head_cols))
    rows = []
    cols = ["month", "sag_count", "swell_count", "interruption_count",
            "recorded_days", "events_per_recorded_day",
            "sag_mean_depth_v", "swell_mean_depth_v",
            "worst_event_ts", "worst_event_v", "worst_event_direction"]
    for (month, sags, swells, interruptions, recorded_days, rate,
         sag_depth, swell_depth, worst_ts, worst_v, worst_dir) in gq[
             cols].itertuples(index=False, name=None):
        rate_txt = f"{rate:.2f}" if pd.notna(rate) else "—"
        sag_txt = f"{sag_depth:.1f}" if pd.notna(sag_depth) else "—"
        swell_txt = f"{swell_depth:.1f}" if pd.notna(swell_depth) else "—"
        worst_txt = (f"{worst_v:.1f} V · {_L(worst_dir)} · {worst_ts:%Y-%m-%d %H:%M}"
                     if pd.notna(worst_ts) else "—")
        cells = "".join([
            f'<td class="tl hi">{_esc(month)}</td>',
            f'<td class="tr">{sags}</td>',
            f'<td class="tr">{swells}</td>',
            f'<td class="tr">{interruptions}</td>',
            f'<td class="tr">{recorded_days:.1f}</td>',
            f'<td class="tr">{rate_txt}</td>',
            f'<td class="tr">{sag_txt}</td>',
            f'<td class="tr">{swell_txt}</td>',
            f'<td class="tr">{_esc(worst_txt)}</td>',
        ])
        rows.append(f"<tr>{cells}</tr>")
    return (f'<table class="stats-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>')


def _stats_table_html(stats_table: pd.DataFrame) -> str:
    if stats_table.empty:
        return f'<div class="chart-empty">{_esc(_L("no data in the analyzed window"))}</div>'
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
                  voltage_anomalies, high_load_episodes, gaps, pal, on_battery=None,
                  events_summary=None):
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
                    latest_rows.append((_L(c), f"{v}", None))
    else:
        latest_rows.append(("DataLog", _L("no rows"), None))

    run_rows: list[tuple[str, str, str | None]] = []
    if energy_summary:
        run_rows.append(("#", _L("Energy"), None))
        run_rows.append((_L("Total energy"), f"{energy_summary['total_kwh']:.4f} kWh", None))
        run_rows.append((f"{_L('Cost')} (PCSS ₡{config.PCSS_FLAT_RATE:g})",
                         fmt_crc(energy_summary["total_cost_pcss"]), None))
        run_rows.append((_L("Cost (Coopesantos tiered)"),
                         fmt_crc(energy_summary["total_cost_tiered"]), pal["violet"]))
        run_rows.append((_L("CO₂ emitted"), f"{energy_summary['total_co2_kg']:.4f} kg", None))
        run_rows.append((_L("Span"), f"{energy_summary['first']} → {energy_summary['last']}", None))
        run_rows.append((_L("Samples (5-min)"), f"{energy_summary['n_samples']}", None))
    if crossval:
        run_rows.append(("#", _L("Cross-validation"), None))
        run_rows.append(("DataLog vs energylog (load %)",
                         f"MAE {crossval['mean_abs_error_pct']:.2f}%, n={crossval['n_pairs']}", None))
    run_rows.append(("#", _L("Files"), None))
    run_rows.append(("DataLog", fmt_bytes(sizes.get("DataLog", 0)), None))
    run_rows.append(("EventLog", fmt_bytes(sizes.get("EventLog (binary)", 0)), None))
    run_rows.append(("energylog/", fmt_bytes(sizes.get("energylog/", 0)), None))
    run_rows.append(("TOTAL", fmt_bytes(sum(sizes.values())), None))
    if hist_stats:
        run_rows.append(("#", _L("Growth"), None))
        run_rows.append((_L("Per hour"), fmt_bytes(hist_stats["bytes_per_hour"]), None))
        run_rows.append((_L("Per day"), fmt_bytes(hist_stats["bytes_per_day"]), None))
        run_rows.append((_L("Snapshots"), str(hist_stats["snapshots"]), None))
    run_rows.append(("#", _L("Anomalies"), None))
    run_rows.append((_L("Voltage out-of-range"),
                     f"{len(voltage_anomalies)} {_L('samples')}",
                     pal["red"] if len(voltage_anomalies) else None))
    n_ob = 0 if on_battery is None else len(on_battery)
    run_rows.append((_L("On-battery episodes (at cadence)"), f"{n_ob}",
                     pal["red"] if n_ob else None))
    run_rows.append((_L("Sustained high-load episodes"), f"{len(high_load_episodes)}",
                     pal["amber"] if len(high_load_episodes) else None))
    run_rows.append((f"{_L('DataLog gaps')} (>{config.DATALOG_EXPECTED_INTERVAL_MIN * 2:.0f} min)",
                     f"{len(gaps)}", pal["amber"] if len(gaps) else None))
    if events_summary:
        run_rows.append(("#", _L("Events"), None))
        run_rows.append((_L("Parsed events"), str(events_summary["n"]), None))
        run_rows.append((_L("On-battery (EventLog)"), str(events_summary["on_battery"]),
                         pal["red"] if events_summary["on_battery"] else None))
        run_rows.append((_L("Last event"),
                         f"{events_summary['last_name']} · {events_summary['last_ts']:%Y-%m-%d %H:%M}",
                         None))
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
/* Touch screens have no hover; keep the card tools visible. */
@media (pointer: coarse) {{ .card-tools {{ opacity: 1; }} }}
.tool-btn {{ font-size: 10px; padding: 2px 7px; border-radius: 6px; cursor: pointer;
  background: transparent; border: 1px solid var(--border); color: var(--faint); }}
.tool-btn:hover {{ color: var(--text); border-color: var(--border-hover); }}
.tool-reset {{ color: var(--blue); border-color: color-mix(in srgb, var(--blue) 45%, transparent); }}
/* Period Comparison baseline selector (roadmap item 22): the "previous" and
   "quarter" pills reuse the preset-pill look and stay visible in the card
   header (the common case, one click); the pick-a-period select is a rarer
   escape hatch and lives in the hover-revealed card-tools tray instead. */
.cmp-pills {{ display: inline-flex; gap: 4px; }}
.cmp-pill {{ font-size: 10.5px; padding: 2px 9px; border-radius: 999px; cursor: pointer;
  background: transparent; border: 1px solid var(--border); color: var(--mut);
  font-family: inherit; }}
.cmp-pill:hover:not(:disabled) {{ border-color: var(--border-hover); color: var(--text); }}
.cmp-pill.is-active {{ color: var(--blue); border-color: color-mix(in srgb, var(--blue) 45%, transparent);
  background: color-mix(in srgb, var(--blue) 10%, transparent); }}
.cmp-pill:disabled {{ opacity: .35; cursor: not-allowed; }}
.cmp-period-select {{ font-size: 10px; padding: 2px 4px; border-radius: 6px; max-width: 120px;
  background: var(--panel); border: 1px solid var(--border); color: var(--faint); font-family: inherit; }}
.chart-box {{ position: relative; touch-action: pan-y;
  user-select: none; -webkit-user-select: none; }}
.chart-box svg {{ cursor: crosshair; }}
.chart-box:focus-visible {{ outline: 2px solid var(--blue); outline-offset: 2px;
  border-radius: 8px; }}
/* Keyboard sample step-through (roadmap item 21): a visibly distinct cue —
   brighter and a different color than the plain keyboard-focus outline
   above — so a sighted keyboard user can tell inspect mode apart from mere
   focus, plus a small textual badge next to the card tools for the same
   reason. Both are shown/hidden by charts.js, never by CSS state alone. */
.chart-box.is-inspecting {{ outline: 3px solid var(--amber); outline-offset: 2px;
  border-radius: 8px; }}
.inspect-badge {{ font-size: 10px; padding: 2px 7px; border-radius: 6px;
  color: var(--amber); border: 1px solid color-mix(in srgb, var(--amber) 45%, transparent);
  background: color-mix(in srgb, var(--amber) 14%, transparent); }}
/* Visually hidden but still announced by screen readers — the one
   aria-live region the inspect-mode step announcements update. */
.sr-only {{ position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px;
  overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }}
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

/* Whole-page export goes through the browser's print pipeline: hide the
   hover-only controls, avoid breaking cards across pages, and keep the card
   colors (the charts are drawn for their card background). */
@media print {{
  body {{ background: #fff !important; padding: 0; }}
  .wrap {{ max-width: none; }}
  .card {{ break-inside: avoid; }}
  .sec-head {{ break-after: avoid-page; }}
  .card-tools, .presets, .cmp-pills, #print-btn, #lightbox, .chart-tooltip, .inspect-badge {{ display: none !important; }}
  .chart-box.is-inspecting {{ outline: none !important; }}
  * {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }}
}}
#print-btn {{ margin-top: 8px; }}
"""


# ======================================================================
# build_dashboard — the public entry point
# ======================================================================
def build_dashboard(datalog_df: pd.DataFrame, energy_df: pd.DataFrame, hist: pd.DataFrame,
                    dl_stats: dict, hist_stats: dict, sizes: dict, energy_summary: dict,
                    stats_table: pd.DataFrame, gaps: pd.DataFrame,
                    voltage_anomalies: pd.DataFrame, high_load_episodes: pd.DataFrame,
                    crossval: dict, on_battery: pd.DataFrame | None = None,
                    battery: dict | None = None,
                    events_summary: dict | None = None,
                    staleness: dict | None = None,
                    forecast: dict | None = None,
                    bill_reconciliation: pd.DataFrame | None = None,
                    annotations: pd.DataFrame | None = None,
                    calibration: dict | None = None,
                    self_tests: pd.DataFrame | None = None,
                    baseline: dict | None = None,
                    grid_quality: pd.DataFrame | None = None,
                    dashboard_window_days: float | None = None,
                    events: pd.DataFrame | None = None) -> str:
    """Assemble the dashboard page and return the finished HTML string."""
    pal = PALETTES.get(config.DASHBOARD_THEME, PALETTES["dark"])
    if on_battery is None:
        on_battery = pd.DataFrame()
    if self_tests is None:
        self_tests = detect_self_tests(datalog_df)
    if battery is None:
        battery = battery_replace_projection(datalog_df, annotations=annotations, self_tests=self_tests)
    if forecast is None:
        forecast = forecast_period_cost(energy_summary)
    if baseline is None:
        baseline = detect_baseline_deviations(energy_df)
    if grid_quality is None:
        # The `on_battery` frame passed in here is already the caller's
        # resolved choice between the authoritative EventLog spans and the
        # DataLog inference (analyze_ups.py makes it once for the episode
        # strips), so reusing it keeps the interruption counts on the same
        # precedence without re-implementing it.
        grid_quality = grid_quality_trend(datalog_df, gaps=gaps, episodes=on_battery)

    bv_panel, bv_slope = _panel_bv(datalog_df, pal)
    rt_panel, latest_w, latest_rt = _panel_rt(energy_df, pal, calibration)
    proj_panel, proj_1yr_kb = _panel_proj(dl_stats, pal)
    panels = {
        "lv": _panel_lv(datalog_df, voltage_anomalies, pal),
        "ul": _panel_ul(datalog_df, pal),
        "pw": _panel_pw(energy_df, pal),
        "hm": _panel_hm(_heatmap_pivot(energy_df)),
        "bv": bv_panel,
        "bc": _panel_bc(datalog_df, pal, self_tests),
        "rt": rt_panel,
        "kw": _panel_kw(energy_summary, pal),
        "daily": _panel_daily(energy_summary, pal, baseline.get("flagged")),
        "cmp": _panel_cmp(energy_summary, pal),
        "wk": _panel_wk(energy_df, pal),
        "growth": _panel_growth(hist, pal),
        "proj": proj_panel,
        "cad": _panel_cad(datalog_df, pal),
        "ev": _panel_ev(events, pal),
    }

    kpis, sparks, sevs = _build_kpis(datalog_df, energy_df, latest_w, latest_rt, pal)
    health = _build_health(sevs, bv_slope, voltage_anomalies, high_load_episodes, gaps, pal,
                           on_battery, battery, staleness)

    last_sample_ms = None
    if not datalog_df.empty:
        last_sample_ms = _ms_list(datalog_df["ts"].iloc[[-1]])[0]

    # With the archive merged in, the full range eventually spans months and
    # the default view becomes unreadably dense; open on the 30-day preset
    # once the analyzed history exceeds ~45 days (the pills still offer All).
    default_preset_days = None
    if not datalog_df.empty:
        span_d = (datalog_df["ts"].iloc[-1] - datalog_df["ts"].iloc[0]).total_seconds() / 86400
        if span_d > 45:
            default_preset_days = 30

    payload = {
        "theme": config.DASHBOARD_THEME,
        "palette": pal,
        "gaps": _gap_spans(gaps),
        "episodes": _episode_spans(on_battery),
        "annotations": _annotation_markers(annotations),
        "panels": panels,
        "sparks": sparks,
        # Chart-side UI strings ride the payload so the Python and JS string
        # tables cannot drift apart.
        "strings": {
            "empty": _L("no data in the analyzed window"),
            "meanPower": _L("mean power"),
            "noData": _L("no data"),
            "staleness": _L("last sample {t} ago"),
        },
        "meta": {
            "last_sample_ms": last_sample_ms,
            "expected_interval_min": config.DATALOG_EXPECTED_INTERVAL_MIN,
            "default_preset_days": default_preset_days,
        },
    }

    # ----- card subtitles (server-side context strings) -----
    nominal = (config.VOLTAGE_NORMAL_LOW + config.VOLTAGE_NORMAL_HIGH) / 2
    ob_bit = f"{len(on_battery)} {_L('on-battery')} · " if len(on_battery) else ""
    lv_sub = (f"{_count(len(voltage_anomalies), _L('anomaly'), _L('anomalies'))} · "
              f"{ob_bit}{_L('nominal')} {nominal:g} V")
    ul_sub = _count(len(high_load_episodes), _L("high-load episode"), _L("high-load episodes"))
    pw_sub = "5-min"
    if energy_summary:
        span_d = (energy_summary["last"] - energy_summary["first"]).total_seconds() / 86400
        pw_sub = f"5-min · {span_d:.0f} d"
    if bv_slope is None:
        bv_sub = _L("trend")
    elif battery["status"] == "projected":
        bv_sub = (f"{bv_slope:+.4f} V/{_L('day')} · {_L('replace')} ≈ "
                  f"{battery['replace_date']:%Y-%m} ({_L('crosses')} {battery['threshold_v']:g} V)")
    elif battery["status"] == "insufficient_history" and bv_slope < 0:
        bv_sub = (f"{bv_slope:+.4f} V/{_L('day')} · {_L('aging')} · "
                  f"{_L('not enough history to project replace-by')}")
    else:
        bv_sub = f"{bv_slope:+.4f} V/{_L('day')} · {_L('aging') if bv_slope < 0 else _L('stable')}"
    # Battery lifecycle annotations (roadmap item 26): once a battery_replaced
    # boundary is in play, name the battery's age alongside whatever the fit
    # itself reports above — segmentation and age are separate facts.
    if battery.get("battery_age_days") is not None:
        bv_sub += (f" · {_L('battery age')} {battery['battery_age_days']:.0f} {_L('days')} "
                   f"({_L('since')} {battery['battery_installed_on']:%Y-%m-%d})")
    n_self_tests = 0 if self_tests is None else int(len(self_tests))
    median_sag_v = None
    if self_tests is not None and not self_tests.empty and "sag_v" in self_tests.columns:
        sagged = self_tests["sag_v"].dropna()
        if not sagged.empty:
            median_sag_v = float(sagged.median())
    bc_sub = _self_test_sub(n_self_tests, median_sag_v)

    rt_sub = (f"{latest_w:.0f}W → {latest_rt:.0f} min" if latest_rt is not None
              else _L("runtime curve"))
    # Runtime-curve calibration (roadmap item 16): name the number of
    # discharges behind the measured overlay once calibrated, or the honest
    # floor note when there is not yet enough discharge data — the same
    # pattern as the battery replace-by projection's history floor.
    cal = calibration or {}
    cal_min_episodes = cal.get("min_episodes", config.CALIBRATION_MIN_EPISODES)
    if cal.get("status") == "calibrated":
        cal_bit = (f"{_L('measured from')} "
                   f"{_count(cal['n_episodes'], _L('discharge'), _L('discharges'))}")
    else:
        cal_bit = (f"{_L('not enough discharge data yet')} "
                   f"({cal.get('n_episodes', 0)}/{cal_min_episodes:.0f})")
    rt_sub = f"{rt_sub} · {cal_bit}"
    kw_sub = f"kWh · ₡ (₡{config.PCSS_FLAT_RATE:g}/kWh)"
    # Baseline-deviation flags (roadmap item 19): silent when nothing is
    # flagged (whether because the history is clean or below the floor —
    # either way there is nothing to say), named only when at least one
    # complete day deviates from the recorded baseline.
    daily_sub = _L("kWh / day")
    n_flagged = len(baseline.get("flagged", []))
    if n_flagged:
        daily_sub = f"{daily_sub} · {_count(n_flagged, _L('day deviates from the recorded baseline'), _L('days deviate from the recorded baseline'))}"
    proj_sub = f"≈ {proj_1yr_kb / 1024:.1f} MB / yr" if proj_1yr_kb else _L("current rate")
    # Event timeline subtitle (roadmap item 20): the total event count and the
    # "by category" framing, or the empty note when the EventLog did not parse
    # or holds no events.
    if events is not None and not events.empty:
        ev_sub = f"{len(events)} {_L('Events')} · {_L('events by category')}"
    else:
        ev_sub = _L("no data")
    energy_note = (f"{energy_summary['total_kwh']:.1f} kWh · "
                   f"₡{energy_summary['total_cost_pcss']:,.0f} · "
                   f"₡{energy_summary['total_cost_tiered']:,.0f}") if energy_summary else _L("no data")
    growth_note = (f"+{fmt_bytes(hist_stats['bytes_per_day'])}/{_L('day')} · "
                   f"{fmt_bytes(sum(sizes.values()))} total") if hist_stats else fmt_bytes(sum(sizes.values())) + " total"

    n_samples = len(datalog_df)
    span_days = dl_stats.get("span_days") if dl_stats else None
    cadence = dl_stats.get("median_interval_sec") if dl_stats else None
    sub_bits = ["PowerChute Serial Shutdown"]
    if n_samples:
        sub_bits.append(f"{n_samples:,} {_L('samples')}")
    if span_days and np.isfinite(span_days):
        sub_bits.append(f"{span_days:.1f} {_L('days')}")
    if cadence and np.isfinite(cadence):
        sub_bits.append(f"{cadence / 60:.0f} min")
    header_sub = " · ".join(sub_bits)

    latest_rows, run_rows = _summary_rows(datalog_df, energy_summary, crossval, sizes,
                                          hist_stats, voltage_anomalies, high_load_episodes,
                                          gaps, pal, on_battery, events_summary)

    generated = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
    foot = (f"{_L('Generated')} {generated} · {_L('read-only analytics snapshot')} · "
            f"{_L('envelope')} {config.VOLTAGE_NORMAL_LOW:g}–{config.VOLTAGE_NORMAL_HIGH:g} V · "
            f"{_L('high-load')} {config.HIGH_LOAD_PCT:g}% · {_L('theme')} {config.DASHBOARD_THEME}")

    # Payload budget for multi-year archives (roadmap item 25): honesty note
    # for the cheap [dashboard] max_days window. Only rendered when the
    # caller (analyze_ups.py) reports that the window actually trimmed
    # something — dashboard_window_days is None both when max_days is 0 (the
    # default) and when max_days turned out larger than the recorded span,
    # so this stays silent in both of those no-op cases.
    window_note_html = ""
    if dashboard_window_days:
        window_note = _L(
            "Showing the last {n} days; older history remains in output/archive/."
        ).format(n=f"{dashboard_window_days:g}")
        window_note_html = f'\n    <div class="dim">{_esc(window_note)}</div>'

    payload_json = json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    charts_js = _CHARTS_JS_TEMPLATE.replace("__DASH_DATA__", payload_json)

    refresh_tag = ""
    if config.DASHBOARD_REFRESH_MINUTES > 0:
        refresh_tag = ('\n<meta http-equiv="refresh" '
                       f'content="{int(config.DASHBOARD_REFRESH_MINUTES * 60)}">')

    # The Billing Periods table only exists once [[tariff.history]] entries
    # are configured — with no history this stays "" so the page is
    # byte-identical to before this feature existed (see the template below,
    # where the blank line either side still renders with no history).
    periods_block = ""
    if energy_summary and energy_summary.get("tariff_history_active"):
        periods_block = (
            '  <div class="grid12">\n'
            '    <div class="card table-card s12">\n'
            f'      <div class="card-title" style="margin-bottom:12px">{_esc(_L("Billing Periods"))}</div>\n'
            f'      {_periods_table_html(energy_summary.get("monthly"))}\n'
            '    </div>\n'
            '  </div>'
        )

    # The Bill Reconciliation table only exists once at least one bills.csv
    # entry reconciles (roadmap item 29) — with none, this stays "" so the
    # page is unchanged for anyone who never uses bills.csv.
    bills_block = ""
    if bill_reconciliation is not None and not bill_reconciliation.empty:
        bills_block = (
            '  <div class="grid12">\n'
            '    <div class="card table-card s12">\n'
            f'      <div class="card-title">{_esc(_L("Bill Reconciliation"))}</div>\n'
            f'      <div class="card-sub" style="margin-bottom:12px">'
            f'{_esc(_L("UPS-metered share of the billed consumption"))}</div>\n'
            f'      {_bills_table_html(bill_reconciliation)}\n'
            '    </div>\n'
            '  </div>'
        )

    # The Grid Quality Trend table (roadmap item 28) only exists once at
    # least one month has samples — with none, this stays "" so the page is
    # unchanged. The subtitle states the cadence caveat with the configured
    # interval: these are events visible at the sampling cadence, not all
    # grid events.
    grid_quality_block = ""
    if grid_quality is not None and not grid_quality.empty:
        cadence_note = _L(
            "events visible at the {n}-min sampling cadence; "
            "short events between samples are missed"
        ).format(n=f"{config.DATALOG_EXPECTED_INTERVAL_MIN:g}")
        grid_quality_block = (
            '  <div class="grid12">\n'
            '    <div class="card table-card s12">\n'
            f'      <div class="card-title">{_esc(_L("Grid Quality Trend"))}</div>\n'
            f'      <div class="card-sub" style="margin-bottom:12px">'
            f'{_esc(cadence_note)}</div>\n'
            f'      {_grid_quality_table_html(grid_quality)}\n'
            '    </div>\n'
            '  </div>'
        )

    def _card(key, span_, title, sub, zoomable, sub_color=None, anomaly_nav=0):
        spec = panels.get(key)
        inspectable = bool(spec and spec.get("kind") == "line")
        header_extra = extra_tool = ""
        # The Period Comparison card's baseline selector (roadmap item 22)
        # only exists once the payload actually carries every period to
        # choose from.
        if key == "cmp" and spec and spec.get("periods"):
            header_extra = _cmp_pills_html(len(spec["periods"]))
            extra_tool = _cmp_select_html(spec["periods"])
        return _chart_card(key, span_, title, sub, zoomable, sub_color,
                           sr_text=_sr_text(spec), anomaly_nav=anomaly_nav,
                           inspectable=inspectable, header_extra=header_extra,
                           extra_tool=extra_tool)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">{refresh_tag}
<title>PowerChute UPS Dashboard</title>
<style>{_shell_css(pal)}</style>
</head>
<body>
<div id="inspect-live" class="sr-only" role="status" aria-live="polite" aria-atomic="true"></div>
<div class="wrap">

  <header>
    <div>
      <div class="brand">{_esc(_L('UPS MONITOR')).replace(' ', '&nbsp;')}</div>
      <h1>{_esc(config.DASHBOARD_MODEL)}</h1>
      <div class="header-sub">{_esc(header_sub)} · <span id="staleness"></span></div>
    </div>
    <div class="health-wrap" style="--health:{health['color']}">
      <div class="health-pill"><span class="health-dot"></span>
        <span class="health-label">{_esc(health['label'])}</span></div>
      <div class="health-sub">{_esc(health['sub'])}</div>
      <button id="print-btn" class="tool-btn" type="button"
        title="Save the whole page as PDF via the browser print dialog">⎙ pdf</button>
    </div>
  </header>

  {_kpi_row_html(kpis)}

  {_section_head(_L('Power Quality'), _L('line · load · draw'), presets=True)}
  <div class="grid12">
    {_card('lv', 12, _L('Line Voltage'), lv_sub, True, anomaly_nav=len(voltage_anomalies))}
    {_card('ul', 4, _L('UPS Load'), ul_sub, True)}
    {_card('pw', 4, _L('Power Draw'), pw_sub, True)}
    {_card('hm', 4, _L('Hourly Power Map'), _L('mean W'), False)}
  </div>

  {_section_head(_L('Battery Health'), _L('voltage · charge · runtime'))}
  <div class="grid12">
    {_card('bv', 12, _L('Battery Voltage'), bv_sub, True, sub_color=pal['amber'] if bv_slope is not None and bv_slope < 0 else None)}
    {_card('bc', 6, _L('Battery Charge'), bc_sub, True)}
    {_card('rt', 6, _L('Estimated Runtime'), rt_sub, False, sub_color=pal['violet'])}
  </div>

  {_section_head(_L('Energy & Cost'), energy_note)}
  <div class="grid12">
    {_card('kw', 8, _L('Cumulative Energy & Cost'), kw_sub, True)}
    {_card('daily', 4, _L('Daily Energy'), daily_sub, False)}
    {_card('cmp', 7, _L('Period Comparison'), f"{_L('cumulative kWh · same day offset')} · {_forecast_sub(forecast)}", False)}
    {_card('wk', 5, _L('Weekday vs Weekend'), _L('mean W by hour'), False)}
  </div>
{periods_block}
{bills_block}
{grid_quality_block}
  {_section_head(_L('Logs & Storage'), growth_note)}
  <div class="grid12">
    {_card('growth', 6, _L('Log File Growth'), 'KB', True)}
    {_card('proj', 3, _L('DataLog Projection'), proj_sub, False)}
    {_card('cad', 3, _L('Sample Cadence'), _L('interval min'), False)}
  </div>

  {_section_head(_L('Reference'), _L('statistics · latest readings'))}
  <div class="grid12">
    {_card('ev', 12, _L('Event Timeline'), ev_sub, True)}
  </div>
  <div class="grid12">
    <div class="card table-card s7">
      <div class="card-title" style="margin-bottom:12px">{_esc(_L('Per-metric Statistics'))}</div>
      {_stats_table_html(stats_table)}
    </div>
    <div class="card table-card s5">
      <div class="card-title" style="margin-bottom:12px">{_esc(_L('Latest Readings & Run Summary'))}</div>
      <div class="sum-grid">
        <div class="sum-col">
          <div class="mini-head">{_esc(_L('Latest sample'))}</div>
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
    <div class="dim">{_esc(_L('Charts rendered as inline SVG — no chart library, fully offline.'))}</div>{window_note_html}
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
