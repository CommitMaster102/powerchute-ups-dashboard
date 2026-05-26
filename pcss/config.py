"""Configuration constants for the PCSS analyzer.

Phase 1 keeps these as module-level constants (identical values to the old
analyze_ups.py CONFIG block). Phase 2 layers a `Config` dataclass +
`load_config()` (TOML) + CLI overrides on top of these defaults.
"""
from __future__ import annotations

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
