"""Unit tests for loaders, config overrides, common helpers, and the
analyzer's date-filter / projection behavior (pytest, no browser). These cover
code paths test_math.py doesn't, and lock in the fixes made during the audit
(single-row projection guard; --since must not inflate the disk-growth
projection). The dashboard payload contract lives in test_chart_payload.py."""
from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import pcss.config as cfg
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
        "BATTERY_CHARGE_WARN_PCT", "BATTERY_CHARGE_CRIT_PCT",
        "RUNTIME_WARN_MIN", "RUNTIME_CRIT_MIN", "DASHBOARD_THEME", "DASHBOARD_MODEL",
        "DASHBOARD_LANGUAGE", "DASHBOARD_REFRESH_MINUTES", "ARCHIVE_ENABLED",
        "BILLING_CYCLE_START_DAY", "ON_BATTERY_VOLTAGE_V", "ON_BATTERY_CAPACITY_DROP_PCT",
        "BATTERY_REPLACE_VOLTAGE_V", "BATTERY_TREND_MIN_DAYS",
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


def test_load_config_new_roadmap_keys(tmp_path, restore_config):
    c = restore_config
    conf = tmp_path / "config.toml"
    conf.write_text(
        '[dashboard]\nlanguage = "es"\nrefresh_minutes = 10\n'
        "[tariff]\nbilling_cycle_start_day = 15\n"
        "[thresholds]\non_battery_voltage_v = 40.0\nbattery_replace_voltage_v = 24.8\n"
        "battery_trend_min_days = 45\n"
        "[archive]\nenabled = false\n",
        encoding="utf-8",
    )
    c.load_config(conf)
    assert c.DASHBOARD_LANGUAGE == "es"
    assert pytest.approx(10.0) == c.DASHBOARD_REFRESH_MINUTES
    assert c.BILLING_CYCLE_START_DAY == 15
    assert pytest.approx(40.0) == c.ON_BATTERY_VOLTAGE_V
    assert pytest.approx(24.8) == c.BATTERY_REPLACE_VOLTAGE_V
    assert pytest.approx(45.0) == c.BATTERY_TREND_MIN_DAYS
    assert c.ARCHIVE_ENABLED is False


def test_load_config_rejects_unknown_language(tmp_path, restore_config):
    c = restore_config
    conf = tmp_path / "config.toml"
    conf.write_text('[dashboard]\nlanguage = "fr"\n', encoding="utf-8")
    c.load_config(conf)
    assert c.DASHBOARD_LANGUAGE == "en"


def test_load_config_agent_dir_arg_wins(tmp_path, restore_config):
    c = restore_config
    agent = tmp_path / "viacli"
    agent.mkdir()
    c.load_config(None, agent_dir=agent)
    assert agent == c.PCSS_AGENT
    assert agent / "DataLog" == c.DATALOG


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


def _hermetic_config(tmp_path):
    """A config that keeps analyzer integration runs hermetic: without it, a
    developer's real output/archive would merge into the analyzed frame."""
    conf = tmp_path / "config.toml"
    conf.write_text("[archive]\nenabled = false\n", encoding="utf-8")
    return conf


def test_since_filter_does_not_inflate_projection(tmp_path, restore_config):
    """The disk-growth projection is whole-file based, so it must be identical
    whether or not --since trims the analyzed window (audit regression guard)."""
    import analyze_ups
    agent = _write_multiday_agent(tmp_path / "agent")
    j_full = tmp_path / "full.json"
    j_since = tmp_path / "since.json"
    common = ["--agent-dir", str(agent), "--no-browser", "--quiet", "--no-snapshot",
              "--config", str(_hermetic_config(tmp_path))]
    analyze_ups.main([*common, "-o", str(tmp_path / "a.html"), "--json", str(j_full)])
    analyze_ups.main([*common, "-o", str(tmp_path / "b.html"), "--json", str(j_since),
                      "--since", "2026-05-05"])
    full = json.loads(j_full.read_text())
    since = json.loads(j_since.read_text())
    assert full["datalog"]["daily_bytes"] == pytest.approx(since["datalog"]["daily_bytes"])
    assert full["datalog"]["daily_bytes"] > 0


def test_main_writes_shell(tmp_path, restore_config):
    """main() end to end: the written page carries the design shell, the
    substituted payload, and no external resource references."""
    import analyze_ups
    agent = _write_multiday_agent(tmp_path / "agent")
    out = tmp_path / "dash.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path))])
    html = out.read_text(encoding="utf-8")
    for token in ["panel-lv", "panel-kw", "panel-cad", "__chartsDebug",
                  "Per-metric Statistics", "preset-pill", "lightbox"]:
        assert token in html, f"missing shell token: {token}"
    assert "__DASH_DATA__" not in html
    assert "<script src" not in html and "<link " not in html
