"""Unit tests for loaders, config overrides, common helpers, and the
analyzer's date-filter / projection behavior (pytest, no browser). These cover
code paths test_math.py doesn't, and lock in the fixes made during the audit
(single-row projection guard; --since must not inflate the disk-growth
projection). The dashboard payload contract lives in test_chart_payload.py."""
from __future__ import annotations

import json
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

import pcss.config as cfg
from pcss.common import fmt_bytes, fmt_crc, ts_2010_to_dt
from pcss.config import TariffPeriod, _parse_tariff_history
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
        "HIGH_LOAD_PCT", "DATALOG_EXPECTED_INTERVAL_MIN", "STALE_WARN_HOURS",
        "STALE_CRIT_HOURS", "ALERTS_ENABLED", "WEBHOOK_ENABLED",
        "RUNTIME_CURVE_W", "RUNTIME_CURVE_MIN",
        "BATTERY_CHARGE_WARN_PCT", "BATTERY_CHARGE_CRIT_PCT",
        "RUNTIME_WARN_MIN", "RUNTIME_CRIT_MIN", "DASHBOARD_THEME", "DASHBOARD_MODEL",
        "DASHBOARD_LANGUAGE", "DASHBOARD_REFRESH_MINUTES", "ARCHIVE_ENABLED",
        "BILLING_CYCLE_START_DAY", "ON_BATTERY_VOLTAGE_V", "ON_BATTERY_CAPACITY_DROP_PCT",
        "BATTERY_REPLACE_VOLTAGE_V", "BATTERY_TREND_MIN_DAYS", "TARIFF_HISTORY",
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


def test_webhook_enabled_defaults_false(restore_config):
    # Roadmap item 23: the webhook channel is off unless a config explicitly
    # turns it on — and it also needs a URL in the keyring, which is checked
    # by the tray, not here.
    c = restore_config
    assert c.WEBHOOK_ENABLED is False


def test_load_config_webhook_enabled_override(tmp_path, restore_config):
    c = restore_config
    conf = tmp_path / "config.toml"
    conf.write_text("[alerts]\nenabled = true\nwebhook_enabled = true\n", encoding="utf-8")
    c.load_config(conf)
    assert c.ALERTS_ENABLED is True
    assert c.WEBHOOK_ENABLED is True


def test_load_config_rejects_unknown_language(tmp_path, restore_config):
    c = restore_config
    conf = tmp_path / "config.toml"
    conf.write_text('[dashboard]\nlanguage = "fr"\n', encoding="utf-8")
    c.load_config(conf)
    assert c.DASHBOARD_LANGUAGE == "en"


def test_default_theme_is_auto():
    """Roadmap item 30: the module default theme is "auto" — a single build
    that follows the viewer's prefers-color-scheme, replacing the old "dark"
    default. Explicit "dark"/"light" configs still pin the initial theme."""
    assert cfg.DASHBOARD_THEME == "auto"


def test_load_config_accepts_theme_values(tmp_path, restore_config):
    c = restore_config
    for theme in ("auto", "dark", "light"):
        conf = tmp_path / f"{theme}.toml"
        conf.write_text(f'[dashboard]\ntheme = "{theme}"\n', encoding="utf-8")
        c.load_config(conf)
        assert theme == c.DASHBOARD_THEME


def test_load_config_rejects_unknown_theme(tmp_path, restore_config):
    c = restore_config
    c.DASHBOARD_THEME = "dark"
    conf = tmp_path / "config.toml"
    conf.write_text('[dashboard]\ntheme = "solarized"\n', encoding="utf-8")
    c.load_config(conf)
    assert c.DASHBOARD_THEME == "dark"   # unknown value ignored, prior kept


def test_load_config_agent_dir_arg_wins(tmp_path, restore_config):
    c = restore_config
    agent = tmp_path / "viacli"
    agent.mkdir()
    c.load_config(None, agent_dir=agent)
    assert agent == c.PCSS_AGENT
    assert agent / "DataLog" == c.DATALOG


# ---------------------------------------------------- tariff history (item 17)
def test_load_config_parses_tariff_history(tmp_path, restore_config):
    c = restore_config
    conf = tmp_path / "config.toml"
    conf.write_text(
        "[[tariff.history]]\n"
        'effective_from = "2026-06-01"\n'
        "coopesantos_low = 82.30\ncoopesantos_high = 133.10\n"
        "tier_limit_kwh = 200.0\npcss_flat = 133.10\n"
        "[[tariff.history]]\n"
        'effective_from = "2026-01-01"\n'
        "coopesantos_low = 75.00\ncoopesantos_high = 120.00\n"
        "tier_limit_kwh = 200.0\npcss_flat = 120.00\n",
        encoding="utf-8",
    )
    c.load_config(conf)
    # date-sorted regardless of the order the entries appeared in the file.
    assert [p.effective_from.isoformat() for p in c.TARIFF_HISTORY] == ["2026-01-01", "2026-06-01"]
    assert c.TARIFF_HISTORY[0].coopesantos_low == pytest.approx(75.00)
    assert c.TARIFF_HISTORY[1].pcss_flat == pytest.approx(133.10)


def test_load_config_no_history_leaves_prior_tariff_history_unchanged(tmp_path, restore_config):
    """Sibling flat-tariff keys read as `tariff.get(key, EXISTING_VALUE)`, so
    an absent key leaves whatever the module already holds untouched rather
    than resetting to a hardcoded default (module-level config state — see
    load_config's docstring). TARIFF_HISTORY follows the same convention: a
    history-less config must not silently clear an already-loaded history.
    ([] happens to be the module default, so asserting == [] after a fresh
    process start can't discriminate between "left alone" and "reset
    to []" — pre-seeding a non-empty list here is what makes the assertion
    meaningful.)"""
    c = restore_config
    seeded = [TariffPeriod(date(2026, 1, 1), coopesantos_low=1.0, coopesantos_high=2.0,
                           tier_limit_kwh=200.0, pcss_flat=2.0)]
    c.TARIFF_HISTORY = seeded
    conf = tmp_path / "config.toml"
    conf.write_text("[tariff]\npcss_flat = 130.0\n", encoding="utf-8")
    c.load_config(conf)
    assert seeded == c.TARIFF_HISTORY


def test_parse_tariff_history_accepts_date_object():
    """tomllib parses an unquoted TOML date literal (effective_from =
    2026-04-01, no quotes) into a datetime.date, not a str. The old
    isinstance(str) check misreported this as a missing 'effective_from'."""
    entries = [{
        "effective_from": date(2026, 4, 1),
        "coopesantos_low": 82.30, "coopesantos_high": 133.10,
        "tier_limit_kwh": 200.0, "pcss_flat": 133.10,
    }]
    parsed = _parse_tariff_history(entries)
    assert parsed[0].effective_from == date(2026, 4, 1)


def test_parse_tariff_history_accepts_datetime_object():
    """An unquoted TOML date-time literal (e.g. 2026-04-01T13:30:00) parses
    to datetime.datetime, not date — accepted and truncated to its date."""
    entries = [{
        "effective_from": datetime(2026, 4, 1, 13, 30),
        "coopesantos_low": 82.30, "coopesantos_high": 133.10,
        "tier_limit_kwh": 200.0, "pcss_flat": 133.10,
    }]
    parsed = _parse_tariff_history(entries)
    assert parsed[0].effective_from == date(2026, 4, 1)


def test_load_config_accepts_unquoted_toml_date_literal(tmp_path, restore_config):
    """End-to-end round trip through tomllib: a config.toml with an unquoted
    date literal (no quotes around effective_from) must parse cleanly."""
    c = restore_config
    conf = tmp_path / "config.toml"
    conf.write_text(
        "[[tariff.history]]\n"
        "effective_from = 2026-04-01\n"
        "coopesantos_low = 82.30\ncoopesantos_high = 133.10\n"
        "tier_limit_kwh = 200.0\npcss_flat = 133.10\n",
        encoding="utf-8",
    )
    c.load_config(conf)
    assert c.TARIFF_HISTORY[0].effective_from == date(2026, 4, 1)


def test_load_config_rejects_invalid_effective_from(tmp_path, restore_config):
    c = restore_config
    conf = tmp_path / "config.toml"
    conf.write_text(
        "[[tariff.history]]\n"
        'effective_from = "not-a-date"\n'
        "coopesantos_low = 82.30\ncoopesantos_high = 133.10\n"
        "tier_limit_kwh = 200.0\npcss_flat = 133.10\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"\[\[tariff\.history\]\] entry 1"):
        c.load_config(conf)


def test_load_config_rejects_missing_rate_field(tmp_path, restore_config):
    c = restore_config
    conf = tmp_path / "config.toml"
    conf.write_text(
        "[[tariff.history]]\n"
        'effective_from = "2026-06-01"\n'
        "coopesantos_low = 82.30\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="missing required field"):
        c.load_config(conf)


def test_load_config_rejects_non_numeric_rate_field(tmp_path, restore_config):
    """A non-numeric rate field (e.g. a quoted string) must raise the same
    loud, named config-error style as the other validation failures, not a
    raw ValueError straight out of float()."""
    c = restore_config
    conf = tmp_path / "config.toml"
    conf.write_text(
        "[[tariff.history]]\n"
        'effective_from = "2026-06-01"\n'
        'coopesantos_low = "oops"\ncoopesantos_high = 133.10\n'
        "tier_limit_kwh = 200.0\npcss_flat = 133.10\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"\[\[tariff\.history\]\] entry 1.*coopesantos_low"):
        c.load_config(conf)


def test_load_config_rejects_history_not_a_list(tmp_path, restore_config):
    """A `history` value that isn't a list of tables (e.g. a bare string)
    must raise a named config error, not an unlabeled AttributeError from
    iterating something that isn't iterable the way expected."""
    c = restore_config
    conf = tmp_path / "config.toml"
    conf.write_text('[tariff]\nhistory = "oops"\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[tariff\.history\] must be a list"):
        c.load_config(conf)


def test_load_config_rejects_history_entry_not_a_table(tmp_path, restore_config):
    """A `history` list whose entries aren't tables (e.g. plain integers)
    must raise a named config error per entry, not an unlabeled
    AttributeError from calling .get() on something that isn't a dict."""
    c = restore_config
    conf = tmp_path / "config.toml"
    conf.write_text("[tariff]\nhistory = [1, 2, 3]\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"\[\[tariff\.history\]\] entry 1.*table"):
        c.load_config(conf)


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


def test_tariff_history_tags_console_and_json_periods(tmp_path, restore_config, capsys):
    """End to end (item 17): with [[tariff.history]] configured, the console
    monthly breakdown and the --json energy.periods array both say which
    rates priced the period, so a rate boundary mid-history does not read
    as a consumption change."""
    import analyze_ups
    agent = _write_multiday_agent(tmp_path / "agent")
    conf = tmp_path / "config.toml"
    conf.write_text(
        "[archive]\nenabled = false\n"
        "[[tariff.history]]\n"
        'effective_from = "2026-01-01"\n'
        "coopesantos_low = 78.17\ncoopesantos_high = 126.51\n"
        "tier_limit_kwh = 200.0\npcss_flat = 126.51\n",
        encoding="utf-8",
    )
    j = tmp_path / "out.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "a.html"),
                      "--no-browser", "--no-snapshot", "--config", str(conf),
                      "--json", str(j)])
    console = capsys.readouterr().out
    assert "rates from 2026-01-01" in console
    data = json.loads(j.read_text())
    periods = data["energy"]["periods"]
    assert len(periods) == 1
    assert periods[0]["month"] == "2026-05"
    assert periods[0]["rate_tag"] == "rates from 2026-01-01"


def test_no_tariff_history_json_has_no_periods_key(tmp_path, restore_config):
    """Without [[tariff.history]] entries, --json carries no periods key at
    all (not an empty list) — the energy dict is unchanged from before this
    feature existed."""
    import analyze_ups
    agent = _write_multiday_agent(tmp_path / "agent")
    j = tmp_path / "out.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "a.html"),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path)), "--json", str(j)])
    data = json.loads(j.read_text())
    assert "periods" not in data["energy"]


def _raise_locked(*_args, **_kwargs):
    raise OSError("alerts.log is locked")


def test_main_survives_a_locked_alerts_log(tmp_path, restore_config, monkeypatch, capsys):
    """A failing alerts.log append (a locked file, a full disk) must not crash
    a run whose analysis already completed: main() still writes the dashboard
    and returns 0, logging a warning — the same guard the weekly digest call
    site already has, now extended to the event-driven alert append."""
    import analyze_ups
    agent = _write_multiday_agent(tmp_path / "agent")
    conf = tmp_path / "config.toml"
    conf.write_text("[archive]\nenabled = false\n[alerts]\nenabled = true\n", encoding="utf-8")
    monkeypatch.setattr(analyze_ups, "_maybe_write_alerts", _raise_locked)
    out = tmp_path / "d.html"
    rc = analyze_ups.main(["--agent-dir", str(agent), "-o", str(out),
                           "--no-browser", "--quiet", "--no-snapshot", "--config", str(conf)])
    assert rc == 0
    assert out.exists()
    err = capsys.readouterr().err
    assert "alert append failed" in err
    assert "Traceback" not in err


def test_malformed_tariff_history_exits_nonzero_without_traceback(
        tmp_path, restore_config, capsys):
    """A malformed [[tariff.history]] entry makes load_config raise at the
    analyzer's entry (roadmap item 17). main() must print the loud
    "config error: ..." message and exit nonzero, not surface a traceback at a
    user who only mistyped a date in config.toml."""
    import analyze_ups
    conf = tmp_path / "config.toml"
    conf.write_text(
        "[[tariff.history]]\n"
        'effective_from = "not-a-date"\n'
        "coopesantos_low = 70\ncoopesantos_high = 110\n"
        "tier_limit_kwh = 200\npcss_flat = 110\n",
        encoding="utf-8",
    )
    rc = analyze_ups.main(["--config", str(conf), "--no-browser", "--quiet",
                           "--agent-dir", str(tmp_path / "empty-agent")])
    assert rc != 0
    err = capsys.readouterr().err
    assert "config error" in err
    assert "Traceback" not in err
