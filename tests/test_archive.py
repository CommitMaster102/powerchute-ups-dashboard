"""Unit tests for the DataLog archive: each analyzer run
appends the freshly loaded DataLog rows to monthly CSV partitions under
output/archive/, and the pipeline merges the archive with the live log so
charts and statistics can span more than the PCSS retention window."""
from __future__ import annotations

import pandas as pd
import pytest

from pcss.loaders import (
    append_datalog_archive,
    load_datalog_archive,
    merge_datalog_frames,
)


def _frame(rows):
    """Build a DataLog-shaped frame from (ts, line_v, capacity) tuples."""
    df = pd.DataFrame(rows, columns=["ts", "Line Voltage", "Battery Capacity"])
    df["ts"] = pd.to_datetime(df["ts"])
    return df.sort_values("ts").reset_index(drop=True)


def test_append_creates_monthly_partitions(tmp_path):
    df = _frame([
        ("2026-04-30 23:40:00", 120.0, 100.0),
        ("2026-05-01 00:00:00", 121.0, 100.0),
        ("2026-05-01 00:20:00", 119.5, 99.0),
    ])
    added = append_datalog_archive(df, tmp_path)
    assert added == 3
    assert (tmp_path / "datalog-2026-04.csv").exists()
    assert (tmp_path / "datalog-2026-05.csv").exists()
    april = pd.read_csv(tmp_path / "datalog-2026-04.csv")
    may = pd.read_csv(tmp_path / "datalog-2026-05.csv")
    assert len(april) == 1
    assert len(may) == 2


def test_append_is_idempotent(tmp_path):
    df = _frame([
        ("2026-05-01 00:00:00", 121.0, 100.0),
        ("2026-05-01 00:20:00", 119.5, 99.0),
    ])
    assert append_datalog_archive(df, tmp_path) == 2
    assert append_datalog_archive(df, tmp_path) == 0
    stored = pd.read_csv(tmp_path / "datalog-2026-05.csv")
    assert len(stored) == 2


def test_append_keeps_distinct_rows_sharing_a_timestamp(tmp_path):
    # PCSS can log two rows in the same second; only exact duplicates are
    # dropped, rows that differ in any value are both kept.
    df = _frame([
        ("2026-05-01 00:00:00", 121.0, 100.0),
        ("2026-05-01 00:00:00", 118.0, 100.0),
    ])
    assert append_datalog_archive(df, tmp_path) == 2
    stored = pd.read_csv(tmp_path / "datalog-2026-05.csv")
    assert len(stored) == 2


def test_append_tolerates_schema_drift(tmp_path):
    # A probe column appearing in a later run must not break the merge; the
    # archive grows to the union of columns and old rows read back as NaN.
    first = _frame([("2026-05-01 00:00:00", 121.0, 100.0)])
    append_datalog_archive(first, tmp_path)
    second = _frame([("2026-05-01 00:20:00", 119.5, 99.0)])
    second["Probe 1 Temperature"] = 24.5
    assert append_datalog_archive(second, tmp_path) == 1
    stored = load_datalog_archive(tmp_path)
    assert len(stored) == 2
    assert "Probe 1 Temperature" in stored.columns
    assert pd.isna(stored["Probe 1 Temperature"].iloc[0])
    assert stored["Probe 1 Temperature"].iloc[1] == pytest.approx(24.5)


def test_load_archive_returns_sorted_span(tmp_path):
    append_datalog_archive(_frame([("2026-05-01 00:00:00", 121.0, 100.0)]), tmp_path)
    append_datalog_archive(_frame([("2026-04-01 12:00:00", 120.0, 100.0)]), tmp_path)
    stored = load_datalog_archive(tmp_path)
    assert len(stored) == 2
    assert stored["ts"].is_monotonic_increasing
    assert stored["ts"].iloc[0] == pd.Timestamp("2026-04-01 12:00:00")


def test_load_archive_empty_dir(tmp_path):
    assert load_datalog_archive(tmp_path).empty
    assert load_datalog_archive(tmp_path / "missing").empty


def test_merge_live_with_archive(tmp_path):
    # The archive holds rows PCSS has since rotated away; the merged frame
    # spans both and the overlap appears once.
    archived = _frame([
        ("2026-04-01 00:00:00", 120.0, 100.0),
        ("2026-05-01 00:00:00", 121.0, 100.0),   # overlaps with live
    ])
    append_datalog_archive(archived, tmp_path)
    live = _frame([
        ("2026-05-01 00:00:00", 121.0, 100.0),
        ("2026-05-01 00:20:00", 119.5, 99.0),
    ])
    merged = merge_datalog_frames(live, load_datalog_archive(tmp_path))
    assert len(merged) == 3
    assert merged["ts"].iloc[0] == pd.Timestamp("2026-04-01 00:00:00")
    assert merged["ts"].is_monotonic_increasing


def test_merge_handles_empty_inputs():
    live = _frame([("2026-05-01 00:00:00", 121.0, 100.0)])
    assert len(merge_datalog_frames(live, pd.DataFrame())) == 1
    assert len(merge_datalog_frames(pd.DataFrame(), live)) == 1
    assert merge_datalog_frames(pd.DataFrame(), pd.DataFrame()).empty


def test_append_empty_frame_is_noop(tmp_path):
    assert append_datalog_archive(pd.DataFrame(), tmp_path) == 0
    assert not any(tmp_path.glob("datalog-*.csv"))


# ---------------------------------------------------------------- analyzer integration
def _write_agent(agent, start="2026-05-01", rows=72):
    agent.mkdir(parents=True, exist_ok=True)
    start_ts = pd.Timestamp(start)
    dl = ["Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity"]
    for i in range(rows):
        t = start_ts + pd.Timedelta(minutes=20 * i)
        dl.append(f"{t:%m/%d/%Y %H:%M:%S}\t120,0\t27,4\t15,0\t100")
    (agent / "DataLog").write_text("\n".join(dl) + "\n", encoding="utf-8")
    return agent


@pytest.fixture
def redirected_output(tmp_path, monkeypatch):
    """Point the analyzer's persistent outputs (size history, archive) at a
    temp dir so integration runs stay hermetic."""
    import pcss.config as cfg
    monkeypatch.setattr(cfg, "SIZE_HISTORY_CSV", tmp_path / "size_history.csv")
    monkeypatch.setattr(cfg, "ARCHIVE_DIR", tmp_path / "archive")
    saved = {n: getattr(cfg, n) for n in
             ("PCSS_AGENT", "DATALOG", "EVENTLOG", "ENERGYLOG_DIR",
              "DASHBOARD_HTML", "ARCHIVE_ENABLED")}
    yield tmp_path
    for n, v in saved.items():
        setattr(cfg, n, v)


def test_analyzer_appends_and_merges_archive(tmp_path, redirected_output):
    """A run archives the live rows; when the live log later rotates, the
    next run still analyzes the archived span (merged into the pipeline)."""
    import json

    import analyze_ups
    agent = _write_agent(tmp_path / "agent", start="2026-05-01", rows=72)
    out = tmp_path / "dash.html"
    j = tmp_path / "s.json"
    common = ["--agent-dir", str(agent), "-o", str(out), "--no-browser", "--quiet"]
    analyze_ups.main([*common, "--json", str(j)])
    first = json.loads(j.read_text())
    assert first["archive"]["rows"] == 72
    assert first["archive"]["added"] == 72

    # Simulate PCSS rotation: the live log now only holds one newer day.
    _write_agent(agent, start="2026-05-03", rows=72)
    analyze_ups.main([*common, "--json", str(j)])
    second = json.loads(j.read_text())
    assert second["archive"]["rows"] == 144
    assert second["archive"]["added"] == 72
    # The dashboard page is built from the merged frame: both days appear.
    html = out.read_text(encoding="utf-8")
    assert "144 samples" in html


def test_no_snapshot_skips_archive_append(tmp_path, redirected_output):
    import analyze_ups
    agent = _write_agent(tmp_path / "agent")
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--quiet", "--no-snapshot"])
    assert not (redirected_output / "archive").exists()


def test_archive_disabled_by_config(tmp_path, redirected_output):
    import analyze_ups
    agent = _write_agent(tmp_path / "agent")
    conf = tmp_path / "config.toml"
    conf.write_text("[archive]\nenabled = false\n", encoding="utf-8")
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--config", str(conf), "--no-browser", "--quiet"])
    assert not (redirected_output / "archive").exists()
