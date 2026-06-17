"""Loaders for the three PCSS data sources + the persistent size-history log.

Three different formats (the parsing quirks are the heart of this code):
  - DataLog: TSV, Spanish-locale numbers (1.234,56) — parsed vectorized via
    read_csv(decimal=",").
  - energylog/*.log: ';'-delimited, dot-decimal, ts = seconds since 2010 LOCAL.
  - EventLog: binary; only its size is read (elsewhere).
"""
from __future__ import annotations

import contextlib
import csv
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from pcss import config
from pcss.common import parse_pcss_number, ts_2010_to_dt


def load_datalog(path: Path | None = None) -> pd.DataFrame:
    """Load the DataLog TSV into a tidy frame with a parsed `ts` column.

    Spanish-locale decimals (``122,0``) and the ``N/A``/``null`` sentinels are
    handled by read_csv itself (``decimal=","`` + ``na_values``), which is both
    faster and simpler than the old per-cell ``apply(parse_es_number)``.
    """
    path = path or config.DATALOG
    if not path.exists():
        return pd.DataFrame()
    # Read the file once. Count non-blank source lines (minus header) to
    # surface how many rows get dropped as malformed/undated, then parse the
    # same in-memory text instead of opening the file a second time.
    text = path.read_text(encoding="utf-8", errors="ignore")
    n_raw = max(sum(1 for line in text.splitlines() if line.strip()) - 1, 0)
    df = pd.read_csv(
        io.StringIO(text), sep="\t", engine="python", on_bad_lines="skip",
        decimal=",", na_values=["N/A", "null", "NaN", ""],
    )
    df.columns = [c.strip() for c in df.columns]
    df["ts"] = pd.to_datetime(df["Date and Time"], format="%m/%d/%Y %H:%M:%S", errors="coerce")
    df = df.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    skipped = n_raw - len(df)
    if skipped > 0:
        print(f"  [warn] DataLog: skipped {skipped} malformed/undated row(s) of {n_raw}")
    return df


@dataclass
class EnergyLogMeta:
    month: str
    interval_sec: int
    max_load_w: float
    n_samples: int


def load_energylog(energylog_dir: Path | None = None) -> tuple[pd.DataFrame, list[EnergyLogMeta]]:
    """
    Parse all energylog/*.log files into a single DataFrame:
      ts, power_w, load_pct, real_w, source_file, interval_sec
    Plus a list of per-file metadata.
    """
    energylog_dir = energylog_dir or config.ENERGYLOG_DIR
    all_rows: list[dict] = []
    metas: list[EnergyLogMeta] = []
    if not energylog_dir.exists():
        return pd.DataFrame(), metas

    for f in sorted(energylog_dir.glob("*.log")):
        # Typed per-file metadata locals (avoids a heterogeneous dict).
        m_month: str = f.stem
        m_interval: int = 300
        m_maxload: float = 1400.0
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
                    m_month = line.split("=", 1)[1]
                elif "$interval=" in line:
                    with contextlib.suppress(ValueError):
                        m_interval = int(line.split("=", 1)[1])
                elif "$calculatedMaxLoad=" in line:
                    m_maxload = float(parse_pcss_number(line.split("=", 1)[1]))
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
                "interval_sec": m_interval,
            })
            n_samples += 1
        metas.append(EnergyLogMeta(
            month=m_month,
            interval_sec=m_interval,
            max_load_w=m_maxload,
            n_samples=n_samples,
        ))

    if not all_rows:
        return pd.DataFrame(), metas
    df = pd.DataFrame(all_rows).sort_values("ts").reset_index(drop=True)
    return df, metas


# ======================================================================
# Snapshot tracking (persistent file-size growth log)
# ======================================================================
def record_size_snapshot(sizes: dict, path: Path | None = None) -> pd.DataFrame:
    path = path or config.SIZE_HISTORY_CSV
    now = datetime.now().replace(microsecond=0)
    row = {
        "timestamp": now.isoformat(),
        "datalog_bytes": sizes.get("DataLog", 0),
        "eventlog_bytes": sizes.get("EventLog (binary)", 0),
        "energylog_bytes": sizes.get("energylog/", 0),
        "total_bytes": sum(sizes.values()),
    }
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            w.writeheader()
        w.writerow(row)
    return pd.read_csv(path, parse_dates=["timestamp"])


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
