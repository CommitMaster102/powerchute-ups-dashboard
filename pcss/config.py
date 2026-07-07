"""Configuration constants for the PCSS analyzer.

Phase 1 keeps these as module-level constants (identical values to the old
analyze_ups.py CONFIG block). Phase 2 layers a `Config` dataclass +
`load_config()` (TOML) + CLI overrides on top of these defaults.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import numpy as np

# PCSS writes its logs here. Override if PCSS is reinstalled elsewhere.
PCSS_AGENT = Path(r"C:\Program Files\APC\PowerChute Serial Shutdown\agent")
DATALOG = PCSS_AGENT / "DataLog"
EVENTLOG = PCSS_AGENT / "EventLog"
ENERGYLOG_DIR = PCSS_AGENT / "energylog"

# output/ lives at the repo root (one level above this package).
OUTPUT = Path(__file__).resolve().parent.parent / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)
SIZE_HISTORY_CSV = OUTPUT / "size_history.csv"
DASHBOARD_HTML = OUTPUT / "dashboard.html"

# Coopesantos T-RE Residencial 2026 structural rates (CRC per kWh).
# First 200 kWh/month at LOW_RATE, anything beyond at HIGH_RATE.
COOPESANTOS_TIER_LIMIT_KWH = 200.0
COOPESANTOS_LOW_RATE = 78.17
COOPESANTOS_HIGH_RATE = 126.51
# What PCSS itself uses (single-rate mode — set to the high tier):
PCSS_FLAT_RATE = 126.51
# Costa Rica grid CO2 intensity (Low Carbon Power 2024 dataset):
CO2_KG_PER_KWH = 0.098

# Empirical UPS runtime curve for the BX2000M-LM with current build.
# Used to estimate "if outage happens now, how long would the UPS last?"
RUNTIME_CURVE_W = np.array([0,   100, 150, 250, 500, 600, 800, 1200])
RUNTIME_CURVE_MIN = np.array([60, 40,  30,  15,  5,   3.5, 2,   0])

# Voltage envelope considered normal for 120V grids (NEC ±5%).
VOLTAGE_NORMAL_LOW = 114.0
VOLTAGE_NORMAL_HIGH = 126.0

# Sustained-high-load threshold for anomaly detection (matches PCSS umbral).
HIGH_LOAD_PCT = 80.0

# DataLog default sample interval (PCSS factory default).
DATALOG_EXPECTED_INTERVAL_MIN = 20.0

# KPI status-pill cut points for the dashboard header row. Battery charge is
# WARN below the warn threshold and ALERT below the crit threshold; estimated
# runtime works the same way in minutes.
BATTERY_CHARGE_WARN_PCT = 90.0
BATTERY_CHARGE_CRIT_PCT = 50.0
RUNTIME_WARN_MIN = 15.0
RUNTIME_CRIT_MIN = 7.0

# Dashboard color theme: "dark" (the designed default) or "light".
DASHBOARD_THEME = "dark"
# UPS model name shown in the dashboard header.
DASHBOARD_MODEL = "APC BX2000M-LM"

# Opt-in alerting: when enabled (config [alerts] enabled=true), the analyzer
# appends a line to ALERTS_LOG whenever the analyzed window has voltage
# anomalies or sustained high-load episodes. Email/notify is a documented
# extension point (no SMTP dependency by default).
ALERTS_ENABLED = False
ALERTS_LOG = OUTPUT / "alerts.log"


def load_config(path: Path | None = None, *, agent_dir: Path | None = None,
                output: Path | None = None) -> Path | None:
    """Overlay a config.toml (and CLI overrides) onto the module defaults.

    Config is intentionally module-level state: consumers read ``config.X`` at
    call time, so mutating these globals here — before the pipeline runs — is
    how a config file / CLI flags take effect, without threading a Config
    object through every function. Returns the config path that was used (or
    None). When `path` is None, falls back to ./config.toml if it exists.
    """
    global PCSS_AGENT, DATALOG, EVENTLOG, ENERGYLOG_DIR, DASHBOARD_HTML
    global COOPESANTOS_TIER_LIMIT_KWH, COOPESANTOS_LOW_RATE, COOPESANTOS_HIGH_RATE, PCSS_FLAT_RATE
    global CO2_KG_PER_KWH, RUNTIME_CURVE_W, RUNTIME_CURVE_MIN
    global VOLTAGE_NORMAL_LOW, VOLTAGE_NORMAL_HIGH, HIGH_LOAD_PCT, DATALOG_EXPECTED_INTERVAL_MIN
    global BATTERY_CHARGE_WARN_PCT, BATTERY_CHARGE_CRIT_PCT, RUNTIME_WARN_MIN, RUNTIME_CRIT_MIN
    global DASHBOARD_THEME, DASHBOARD_MODEL, ALERTS_ENABLED

    if path is None:
        default = Path("config.toml")
        path = default if default.exists() else None

    data: dict = {}
    if path is not None and Path(path).exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)

    paths = data.get("paths", {})
    tariff = data.get("tariff", {})
    grid = data.get("grid", {})
    th = data.get("thresholds", {})
    rc = data.get("runtime_curve", {})

    agent = agent_dir or paths.get("pcss_agent")
    if agent:
        PCSS_AGENT = Path(agent)
    DATALOG = PCSS_AGENT / "DataLog"
    EVENTLOG = PCSS_AGENT / "EventLog"
    ENERGYLOG_DIR = PCSS_AGENT / "energylog"

    COOPESANTOS_LOW_RATE = float(tariff.get("coopesantos_low", COOPESANTOS_LOW_RATE))
    COOPESANTOS_HIGH_RATE = float(tariff.get("coopesantos_high", COOPESANTOS_HIGH_RATE))
    COOPESANTOS_TIER_LIMIT_KWH = float(tariff.get("tier_limit_kwh", COOPESANTOS_TIER_LIMIT_KWH))
    PCSS_FLAT_RATE = float(tariff.get("pcss_flat", PCSS_FLAT_RATE))
    CO2_KG_PER_KWH = float(grid.get("co2_kg_per_kwh", CO2_KG_PER_KWH))
    VOLTAGE_NORMAL_LOW = float(th.get("voltage_normal_low", VOLTAGE_NORMAL_LOW))
    VOLTAGE_NORMAL_HIGH = float(th.get("voltage_normal_high", VOLTAGE_NORMAL_HIGH))
    HIGH_LOAD_PCT = float(th.get("high_load_pct", HIGH_LOAD_PCT))
    DATALOG_EXPECTED_INTERVAL_MIN = float(th.get("datalog_expected_interval_min", DATALOG_EXPECTED_INTERVAL_MIN))
    BATTERY_CHARGE_WARN_PCT = float(th.get("battery_charge_warn_pct", BATTERY_CHARGE_WARN_PCT))
    BATTERY_CHARGE_CRIT_PCT = float(th.get("battery_charge_crit_pct", BATTERY_CHARGE_CRIT_PCT))
    RUNTIME_WARN_MIN = float(th.get("runtime_warn_min", RUNTIME_WARN_MIN))
    RUNTIME_CRIT_MIN = float(th.get("runtime_crit_min", RUNTIME_CRIT_MIN))

    dash = data.get("dashboard", {})
    theme = str(dash.get("theme", DASHBOARD_THEME)).lower()
    if theme in ("dark", "light"):
        DASHBOARD_THEME = theme
    DASHBOARD_MODEL = str(dash.get("model", DASHBOARD_MODEL))

    if rc.get("watts") and rc.get("minutes"):
        RUNTIME_CURVE_W = np.array(rc["watts"], dtype=float)
        RUNTIME_CURVE_MIN = np.array(rc["minutes"], dtype=float)

    ALERTS_ENABLED = bool(data.get("alerts", {}).get("enabled", ALERTS_ENABLED))

    DASHBOARD_HTML = Path(output) if output else (OUTPUT / "dashboard.html")
    return path
