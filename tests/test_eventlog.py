"""Unit tests for EventLog parsing.

The PCSS EventLog is one Java ObjectOutputStream of
com.apcc.m11.arch.event.Event objects. The loader decodes it generically
with javaobj-py3 (no APC class definitions), resolves event names from the
resource bundles inside the installed PCSS jars with a built-in fallback
table, and degrades to a status string — never a crash — when the file,
the parser, or the format is not available.

The binary fixture is a real EventLog captured 2026-07-06 from PCSS on this
project's own UPS. It contains no personal data (verified: only class names,
ObjectId integers, and timestamps — no hostnames or free text). Assertions
use epoch-millisecond deltas and counts, never absolute wall-clock strings,
so they hold in any timezone (CI runs under UTC).
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pandas as pd
import pytest

import pcss.eventlog as ev
from pcss.eventlog import (
    append_event_archive,
    load_event_archive,
    load_event_names,
    load_eventlog,
    merge_event_frames,
    on_battery_spans,
)

FIXTURE = Path(__file__).parent / "fixtures" / "EventLog"


# ---------------------------------------------------------------- parsing
def test_fixture_parses_completely():
    df, status = load_eventlog(FIXTURE)
    assert status == "ok"
    assert len(df) == 241
    assert list(df.columns) == ["ts", "ts_ms", "oid", "active", "name"]
    assert df["ts_ms"].is_monotonic_increasing
    # The first event of the capture is a Monitoring Started.
    assert df["oid"].iloc[0] == "3.5.1.5.6.10"
    assert df["name"].iloc[0] == "Monitoring Started"
    assert bool(df["active"].iloc[0]) is True


def test_fixture_on_battery_events_counted():
    df, _ = load_eventlog(FIXTURE)
    assert (df["oid"] == "3.5.1.5.4.1").sum() == 5      # On Battery
    assert (df["oid"] == "3.5.1.5.4.2").sum() == 5      # No Longer On Battery
    assert (df["oid"] == "3.5.1.5.3.18").sum() == 1     # Time On Battery Threshold
    on = df[df["oid"] == "3.5.1.5.4.1"]
    assert set(on["name"]) == {"On Battery"}


def test_on_battery_spans_pair_up():
    df, _ = load_eventlog(FIXTURE)
    spans = on_battery_spans(df)
    assert len(spans) == 5
    assert not spans["open"].any()                       # every outage ended
    # The 2026-06-25 outage ran 19 minutes 24.676 seconds (fixed epoch-ms
    # delta, independent of the test machine's timezone).
    assert spans["duration_min"].max() == pytest.approx(19.411, abs=0.01)
    assert (spans["end"] > spans["start"]).all()


def test_on_battery_spans_open_episode():
    df = pd.DataFrame({
        "ts": pd.to_datetime(["2026-06-01 10:00", "2026-06-01 10:05"]),
        "ts_ms": [1000, 2000],
        "oid": ["3.5.1.5.6.10", "3.5.1.5.4.1"],
        "active": [True, True],
        "name": ["Monitoring Started", "On Battery"],
    })
    spans = on_battery_spans(df)
    assert len(spans) == 1
    assert bool(spans["open"].iloc[0]) is True
    assert pd.isna(spans["end"].iloc[0])


def test_missing_file_degrades():
    df, status = load_eventlog(Path("does/not/exist/EventLog"))
    assert df.empty
    assert status == "missing"


def test_corrupt_file_degrades(tmp_path):
    p = tmp_path / "EventLog"
    p.write_bytes(b"\xac\xed\x00\x05" + b"\x99" * 200)   # right magic, garbage body
    df, status = load_eventlog(p)
    assert df.empty
    assert status.startswith("parse-error")
    p.write_bytes(b"this is not java serialization at all")
    df2, status2 = load_eventlog(p)
    assert df2.empty
    assert status2.startswith("parse-error")


def test_without_parser_degrades(monkeypatch):
    monkeypatch.setattr(ev, "_javaobj", None)
    df, status = load_eventlog(FIXTURE)
    assert df.empty
    assert status == "no-parser"


# ---------------------------------------------------------------- name resolution
def test_names_from_jar_bundles(tmp_path):
    comp = tmp_path / "comp"
    comp.mkdir()
    with zipfile.ZipFile(comp / "Fake.jar", "w") as z:
        z.writestr("com/x/Resources/FakeEvents.properties",
                   ".9.9.9.1.true=Test Event\n"
                   ".9.9.9.1.false=Test Event Cleared\n"
                   ".9.9.9.2.name=Named Both Ways\n")
    names = load_event_names(tmp_path)
    assert names[("9.9.9.1", True)] == "Test Event"
    assert names[("9.9.9.1", False)] == "Test Event Cleared"
    assert names[("9.9.9.2", True)] == "Named Both Ways"
    assert names[("9.9.9.2", False)] == "Named Both Ways"


def test_names_no_jars_is_empty(tmp_path):
    assert load_event_names(tmp_path) == {}


def test_unknown_oid_gets_numeric_label():
    # Ids missing from both the bundles and the fallback table still render.
    assert ev.resolve_name("1.2.3.4", True, {}) == "event 1.2.3.4"
    assert ev.resolve_name("1.2.3.4", False, {}) == "event 1.2.3.4 (cleared)"
    assert ev.resolve_name("3.5.1.5.4.1", True, {}) == "On Battery"


# ---------------------------------------------------------------- event archive
def test_event_archive_idempotent(tmp_path):
    df, _ = load_eventlog(FIXTURE)
    assert append_event_archive(df, tmp_path) == 241
    assert append_event_archive(df, tmp_path) == 0
    stored = load_event_archive(tmp_path)
    assert len(stored) == 241
    assert stored["ts_ms"].is_monotonic_increasing


def test_event_archive_merge_spans_rotation(tmp_path):
    df, _ = load_eventlog(FIXTURE)
    append_event_archive(df.head(100), tmp_path)
    merged = merge_event_frames(df.tail(180), load_event_archive(tmp_path))
    assert len(merged) == 241                            # overlap appears once
    assert merged["ts_ms"].is_monotonic_increasing


# ---------------------------------------------------------------- analyzer integration
def test_analyzer_reports_events(tmp_path, monkeypatch):
    import json

    import analyze_ups
    import pcss.config as cfg
    monkeypatch.setattr(cfg, "SIZE_HISTORY_CSV", tmp_path / "size_history.csv")
    monkeypatch.setattr(cfg, "ARCHIVE_DIR", tmp_path / "archive")
    saved = {n: getattr(cfg, n) for n in
             ("PCSS_AGENT", "DATALOG", "EVENTLOG", "ENERGYLOG_DIR", "DASHBOARD_HTML")}
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "DataLog").write_text(
        "Date and Time\tLine Voltage\tBattery Capacity\n"
        "06/25/2026 14:20:00\t120,0\t100\n"
        "06/25/2026 14:40:00\t120,0\t100\n",
        encoding="utf-8",
    )
    (agent / "EventLog").write_bytes(FIXTURE.read_bytes())
    j = tmp_path / "s.json"
    try:
        analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                          "--no-browser", "--quiet", "--json", str(j)])
    finally:
        for n, v in saved.items():
            setattr(cfg, n, v)
    summary = json.loads(j.read_text())
    assert summary["events"]["status"] == "ok"
    assert summary["events"]["n_events"] == 241
    assert summary["events"]["on_battery_events"] == 5
    # The parsed events were archived alongside the DataLog rows.
    assert (tmp_path / "archive" / "events.csv").exists()
    # The dashboard's episode strips come from the authoritative event spans.
    html = (tmp_path / "d.html").read_text(encoding="utf-8")
    assert '"episodes":[[' in html
