"""Parsing, formatting, and timestamp helpers shared by the analyzer and the
tray. This is the common module both entry points import (the analyzer's
loaders and the tray's status parser both need locale-aware number parsing).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# energylog uses seconds since 2010-01-01 LOCAL time (verified empirically:
# the first energylog timestamp aligns with the first DataLog timestamp
# within seconds).
EPOCH_2010 = datetime(2010, 1, 1)


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
