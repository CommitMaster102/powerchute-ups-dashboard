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
from datetime import date, datetime
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
# Bill reconciliation (roadmap item 29) — a user-owned bills.csv
# ======================================================================
_BILLS_COLUMNS = ["period_start", "kwh", "amount_crc"]


def load_bills(path: Path | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Load the user-owned bills.csv: period_start (YYYY-MM-DD), kwh (billed
    kWh for the whole house), amount_crc (billed amount), header row
    required.

    This file holds hand-entered personal data, so it is read defensively.
    A missing file simply disables bill reconciliation — reconciliation is
    opt-in per bill, not a required habit — and returns an empty frame with
    no warnings at all. A file missing one of the required columns is
    reported and ignored in full. A row with an unparseable period_start or
    a non-numeric kwh/amount_crc is reported (naming the line and, where
    possible, the offending value) and skipped; the analyzer never crashes
    on this file. A kwh of zero or less, or a negative amount_crc, is
    likewise reported and skipped rather than kept: dividing by a
    non-positive kwh downstream would otherwise produce NaN share/rate
    figures ("nan%" in the console, a non-standard NaN token in --json).
    The file is only ever read here, never written.
    """
    path = path or config.BILLS_FILE
    if not path.exists():
        return pd.DataFrame(columns=_BILLS_COLUMNS), []

    warnings: list[str] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(_BILLS_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            warnings.append(
                f"bills file {path}: missing required column(s) "
                f"{', '.join(sorted(missing))}; the file is ignored")
            return pd.DataFrame(columns=_BILLS_COLUMNS), warnings

        rows: list[dict] = []
        for line_no, row in enumerate(reader, start=2):  # header occupies line 1
            raw_date = (row.get("period_start") or "").strip()
            try:
                period_start = date.fromisoformat(raw_date)
            except ValueError:
                warnings.append(
                    f"bills file line {line_no}: invalid period_start "
                    f"{raw_date!r} (expected YYYY-MM-DD); row skipped")
                continue
            try:
                kwh = float(row.get("kwh", ""))
                amount_crc = float(row.get("amount_crc", ""))
            except (TypeError, ValueError):
                warnings.append(
                    f"bills file line {line_no} ({raw_date}): non-numeric "
                    "kwh or amount_crc; row skipped")
                continue
            if kwh <= 0 or amount_crc < 0:
                warnings.append(
                    f"bills file line {line_no} ({raw_date}): kwh must be "
                    "positive and amount_crc must not be negative; row skipped")
                continue
            rows.append({"period_start": period_start, "kwh": kwh, "amount_crc": amount_crc})
    return pd.DataFrame(rows, columns=_BILLS_COLUMNS), warnings


# ======================================================================
# Battery lifecycle annotations (roadmap item 26) — a user-owned
# annotations.csv
# ======================================================================
_ANNOTATIONS_COLUMNS = ["date", "kind", "label"]


def load_annotations(path: Path | None = None) -> tuple[pd.DataFrame, list[str]]:
    """Load the user-owned annotations.csv: date (YYYY-MM-DD), kind (freeform
    text; the analyzer only ever recognizes "battery_replaced" as a
    fit-segmentation boundary), label (a short note shown on the dashboard's
    time panels), header row required.

    This file holds hand-entered personal notes, so it is read defensively,
    the same pattern as `load_bills` (roadmap item 29). A missing file simply
    disables the feature — annotating lifecycle events is opt-in, not a
    required habit — and returns an empty frame with no warnings at all. A
    file missing one of the required columns is reported and ignored in
    full. A row with an unparseable date, or a blank kind, is reported
    (naming the line and, where possible, the offending value) and skipped;
    the analyzer never crashes on this file. A blank label is kept as an
    empty string rather than skipped — the dashboard falls back to the
    entry's kind when there is no label to show. The file is only ever read
    here, never written.
    """
    path = path or config.ANNOTATIONS_FILE
    if not path.exists():
        return pd.DataFrame(columns=_ANNOTATIONS_COLUMNS), []

    warnings: list[str] = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(_ANNOTATIONS_COLUMNS) - set(reader.fieldnames or [])
        if missing:
            warnings.append(
                f"annotations file {path}: missing required column(s) "
                f"{', '.join(sorted(missing))}; the file is ignored")
            return pd.DataFrame(columns=_ANNOTATIONS_COLUMNS), warnings

        rows: list[dict] = []
        for line_no, row in enumerate(reader, start=2):  # header occupies line 1
            raw_date = (row.get("date") or "").strip()
            try:
                entry_date = date.fromisoformat(raw_date)
            except ValueError:
                warnings.append(
                    f"annotations file line {line_no}: invalid date "
                    f"{raw_date!r} (expected YYYY-MM-DD); row skipped")
                continue
            kind = (row.get("kind") or "").strip()
            if not kind:
                warnings.append(
                    f"annotations file line {line_no} ({raw_date}): missing "
                    "kind; row skipped")
                continue
            label = (row.get("label") or "").strip()
            rows.append({"date": entry_date, "kind": kind, "label": label})
    return pd.DataFrame(rows, columns=_ANNOTATIONS_COLUMNS), warnings


# ======================================================================
# DataLog archive (persistent measurements beyond PCSS's retention window)
# ======================================================================
def _dedup_datalog(df: pd.DataFrame) -> pd.DataFrame:
    """Drop exact-duplicate rows and sort by timestamp.

    The timestamp alone is not a safe key: PCSS can log two rows in the same
    second, and a clock adjustment can repeat a timestamp. Whole-row equality
    keeps those (they differ in some value) while making re-appends of the
    same data a no-op. pandas treats NaN as equal to NaN here, so rows with
    missing probe columns still deduplicate.
    """
    return df.drop_duplicates().sort_values("ts", kind="stable").reset_index(drop=True)


def append_datalog_archive(df: pd.DataFrame, archive_dir: Path | None = None) -> int:
    """Append DataLog rows to monthly CSV partitions (datalog-YYYY-MM.csv).

    Idempotent: consecutive runs see mostly the same rows, and only rows not
    already present are added. Columns may appear or disappear across PCSS
    reconfigurations (temperature and humidity probes); the merge is a
    concatenation followed by deduplication, so the archive simply grows to
    the union of columns. Returns the number of newly stored rows.
    """
    archive_dir = archive_dir or config.ARCHIVE_DIR
    if df is None or df.empty or "ts" not in df.columns:
        return 0
    archive_dir.mkdir(parents=True, exist_ok=True)
    added = 0
    for period, month_df in df.groupby(df["ts"].dt.to_period("M")):
        f = archive_dir / f"datalog-{period}.csv"
        if f.exists():
            try:
                existing = pd.read_csv(f, parse_dates=["ts"])
            except Exception:
                existing = pd.DataFrame()
            merged = _dedup_datalog(pd.concat([existing, month_df], ignore_index=True))
            added += len(merged) - len(existing)
        else:
            merged = _dedup_datalog(month_df)
            added += len(merged)
        merged.to_csv(f, index=False)
    return added


def load_datalog_archive(archive_dir: Path | None = None) -> pd.DataFrame:
    """Load every monthly archive partition into one sorted frame."""
    archive_dir = archive_dir or config.ARCHIVE_DIR
    if not archive_dir.exists():
        return pd.DataFrame()
    frames = []
    for f in sorted(archive_dir.glob("datalog-*.csv")):
        try:
            frames.append(pd.read_csv(f, parse_dates=["ts"]))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    return _dedup_datalog(pd.concat(frames, ignore_index=True))


def merge_datalog_frames(live: pd.DataFrame, archive: pd.DataFrame) -> pd.DataFrame:
    """Merge the live DataLog with the archive; overlap rows appear once."""
    frames = [f for f in (archive, live) if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        return frames[0].reset_index(drop=True)
    return _dedup_datalog(pd.concat(frames, ignore_index=True))


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
