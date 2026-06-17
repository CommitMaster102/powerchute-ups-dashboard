"""Unit tests for loaders, config overrides, common helpers, animation
metadata, and the analyzer's date-filter / projection behavior (pytest, no
browser). These cover code paths test_math.py doesn't, and lock in the fixes
made during the audit (single-row projection guard; --since must not inflate
the disk-growth projection)."""
from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

import pcss.config as cfg
from pcss.animation import _heatmap_metadata, _runtime_metadata
from pcss.common import fmt_bytes, fmt_crc, ts_2010_to_dt
from pcss.loaders import load_datalog, load_energylog
from pcss.stats import datalog_stats


@pytest.fixture
def restore_config():
    """Snapshot the mutable module-level config globals and restore them after
    the test (load_config / main mutate these in place)."""
    names = [
        "PCSS_AGENT", "DATALOG", "EVENTLOG", "ENERGYLOG_DIR", "DASHBOARD_HTML",
        "COOPESANTOS_TIER_LIMIT_KWH", "COOPESANTOS_LOW_RATE", "COOPESANTOS_HIGH_RATE",
        "PCSS_FLAT_RATE", "CO2_KG_PER_KWH", "VOLTAGE_NORMAL_LOW", "VOLTAGE_NORMAL_HIGH",
        "HIGH_LOAD_PCT", "DATALOG_EXPECTED_INTERVAL_MIN", "ALERTS_ENABLED",
        "RUNTIME_CURVE_W", "RUNTIME_CURVE_MIN",
    ]
    saved = {n: getattr(cfg, n) for n in names}
    yield cfg
    for n, v in saved.items():
        setattr(cfg, n, v)


# ---------------------------------------------------------------- common helpers
def test_fmt_bytes_units():
    assert fmt_bytes(512) == "512.0 B"
    assert fmt_bytes(1536) == "1.5 KB"
    assert fmt_bytes(1024 ** 2) == "1.0 MB"
    assert fmt_bytes(1024 ** 3) == "1.0 GB"


def test_fmt_crc():
    assert fmt_crc(1234.5) == "CRC 1,234.50"
    assert fmt_crc(float("nan")) == "—"


def test_ts_2010_to_dt():
    assert ts_2010_to_dt(0) == datetime(2010, 1, 1)
    assert ts_2010_to_dt(86400) == datetime(2010, 1, 2)


# ---------------------------------------------------------------- datalog_stats
def test_datalog_stats_single_row_has_no_projection():
    # One row gives no interval/span; projections must be NaN (not ~tens of
    # GB/day from a 1-second span). Regression guard for the audit fix.
    df = pd.DataFrame({"ts": pd.to_datetime(["2026-05-01 00:00"]), "Line Voltage": [120.0]})
    st = datalog_stats(df, file_size=1_000_000)
    assert st["n_entries"] == 1
    assert np.isnan(st["span_days"])
    assert np.isnan(st["daily_bytes"])
    assert np.isnan(st["yearly_bytes"])
    assert np.isnan(st["daily_entries"])


# ---------------------------------------------------------------- loaders
def test_load_datalog_parses_spanish_and_skips_undated(tmp_path):
    p = tmp_path / "DataLog"
    p.write_text(
        "Date and Time\tLine Voltage\tBattery Capacity\n"
        "05/01/2026 00:00:00\t120,5\t100\n"
        "05/01/2026 00:20:00\t121,0\t99\n"
        "garbage line with no date\n",
        encoding="utf-8",
    )
    df = load_datalog(p)
    assert len(df) == 2                                  # undated row dropped
    assert df["Line Voltage"].iloc[0] == pytest.approx(120.5)
    assert df["Battery Capacity"].iloc[1] == pytest.approx(99.0)
    assert "ts" in df.columns


def test_load_datalog_missing_file_returns_empty(tmp_path):
    assert load_datalog(tmp_path / "nope").empty


def test_load_energylog_parses_header_and_rows(tmp_path):
    d = tmp_path / "energylog"
    d.mkdir()
    (d / "2026-05.log").write_text(
        "# $month=2026-05\n# $interval=300\n# $calculatedMaxLoad=1400.0\n"
        "1714521600;null;20.0;280.0\n"
        "1714521900;null;25.0;350.0\n",
        encoding="utf-8",
    )
    df, metas = load_energylog(d)
    assert len(df) == 2
    assert df["power_w"].iloc[0] == pytest.approx(280.0)
    assert df["interval_sec"].iloc[0] == 300
    assert pd.isna(df["real_w"].iloc[0])                 # 'null' -> NaN
    assert len(metas) == 1
    assert metas[0].interval_sec == 300
    assert metas[0].max_load_w == pytest.approx(1400.0)
    assert metas[0].n_samples == 2


# ---------------------------------------------------------------- config overrides
def test_load_config_overrides(tmp_path, restore_config):
    c = restore_config
    agent = tmp_path / "agent"
    agent.mkdir()
    out = tmp_path / "dash.html"
    conf = tmp_path / "config.toml"
    conf.write_text(
        f"[paths]\npcss_agent = '{agent.as_posix()}'\n"
        "[tariff]\npcss_flat = 200.0\ncoopesantos_low = 10.0\n"
        "[thresholds]\nhigh_load_pct = 75.0\n"
        "[alerts]\nenabled = true\n",
        encoding="utf-8",
    )
    used = c.load_config(conf, output=out)
    assert used == conf
    assert agent == c.PCSS_AGENT
    assert agent / "DataLog" == c.DATALOG
    assert agent / "energylog" == c.ENERGYLOG_DIR
    assert pytest.approx(200.0) == c.PCSS_FLAT_RATE
    assert pytest.approx(10.0) == c.COOPESANTOS_LOW_RATE
    assert pytest.approx(75.0) == c.HIGH_LOAD_PCT
    assert c.ALERTS_ENABLED is True
    assert out == c.DASHBOARD_HTML


def test_load_config_agent_dir_arg_wins(tmp_path, restore_config):
    c = restore_config
    agent = tmp_path / "viacli"
    agent.mkdir()
    c.load_config(None, agent_dir=agent)
    assert agent == c.PCSS_AGENT
    assert agent / "DataLog" == c.DATALOG


# ---------------------------------------------------------------- animation metadata
def _energy_df(power_w, start="2026-05-01 00:00:00", interval_sec=300):
    ts = pd.date_range(start, periods=len(power_w), freq=f"{interval_sec}s")
    return pd.DataFrame({"ts": ts, "power_w": power_w})


def test_runtime_metadata_marker_uses_latest_reading():
    meta = _runtime_metadata(0, _energy_df([100.0, 200.0, 300.0]), n_frames=3)
    assert meta["type"] == "marker"
    assert meta["trace_idx"] == 0
    assert len(meta["marker_data"]) == 3
    assert meta["marker_data"][-1]["w"] == pytest.approx(300.0)
    assert meta["marker_data"][0]["w"] == pytest.approx(100.0)


def test_runtime_metadata_none_when_empty():
    assert _runtime_metadata(0, pd.DataFrame()) is None
    assert _runtime_metadata(None, _energy_df([100.0])) is None


def test_heatmap_metadata_nan_becomes_none():
    pivot = pd.DataFrame(
        [[1.0, 2.0], [3.0, np.nan]],
        index=[date(2026, 5, 1), date(2026, 5, 2)],
        columns=[0, 1],
    )
    meta = _heatmap_metadata(0, pivot)
    assert meta["type"] == "heatmap_reveal"
    assert meta["n_frames"] == 2
    assert meta["z_full"][0][0] == pytest.approx(1.0)
    assert meta["z_full"][1][1] is None                  # NaN -> None for JSON/JS


# ---------------------------------------------------------------- analyzer integration
def _write_multiday_agent(agent):
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "energylog").mkdir(exist_ok=True)
    start = datetime(2026, 5, 1)
    dl = ["Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity"]
    for i in range(5 * 72):                              # 5 days @ 20 min
        t = start + pd.Timedelta(minutes=20 * i)
        dl.append(f"{t:%m/%d/%Y %H:%M:%S}\t120,0\t27,4\t15,0\t100")
    (agent / "DataLog").write_text("\n".join(dl) + "\n", encoding="utf-8")
    el = ["# $month=2026-05", "# $interval=300", "# $calculatedMaxLoad=1400.0"]
    for i in range(5 * 288):                             # 5 days @ 5 min
        secs = (start + pd.Timedelta(minutes=5 * i) - datetime(2010, 1, 1)).total_seconds()
        el.append(f"{secs:.0f};null;15.0;210.0")
    (agent / "energylog" / "2026-05.log").write_text("\n".join(el) + "\n", encoding="utf-8")
    return agent


def test_since_filter_does_not_inflate_projection(tmp_path, restore_config):
    """The disk-growth projection is whole-file based, so it must be identical
    whether or not --since trims the analyzed window (audit regression guard)."""
    import analyze_ups
    agent = _write_multiday_agent(tmp_path / "agent")
    j_full = tmp_path / "full.json"
    j_since = tmp_path / "since.json"
    common = ["--agent-dir", str(agent), "--no-browser", "--quiet", "--no-snapshot"]
    analyze_ups.main([*common, "-o", str(tmp_path / "a.html"), "--json", str(j_full)])
    analyze_ups.main([*common, "-o", str(tmp_path / "b.html"), "--json", str(j_since),
                      "--since", "2026-05-05"])
    full = json.loads(j_full.read_text())
    since = json.loads(j_since.read_text())
    assert full["datalog"]["daily_bytes"] == pytest.approx(since["datalog"]["daily_bytes"])
    assert full["datalog"]["daily_bytes"] > 0
