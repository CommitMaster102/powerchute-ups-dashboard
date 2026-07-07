"""Unit tests for the weekly digest (roadmap item 32).

Event-driven alerts (items 14 and 23) say when something happened; the
digest says that nothing did, on a schedule. It is opt-in on top of
`[alerts] enabled` (its only transport is `alerts.log`; the tray's
`AlertWatcher` toast and item 23's webhook then deliver it for free — this
feature must not grow its own transport), gated by a marker file
(`output/last_digest.txt`) recording the ISO (year, week) of the last run
that actually appended a digest line, and worded from numbers the pipeline
has already computed elsewhere (energy summary, forecast, anomalies,
episodes, battery projection) — nothing here computes a new statistic.

Layers, matching how the implementation is built: config default/override,
the gate (marker parse/compare against an injected "today", plus the atomic
marker write), the line assembly (a pure function over a dict of
already-computed values), and the wiring (`_maybe_write_weekly_digest`,
which requires both config flags and only then gates on the marker,
appends, and advances it).
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd
import pytest

import pcss.config as cfg


# ---------------------------------------------------------------------------
# Layer: config
# ---------------------------------------------------------------------------
def test_weekly_digest_disabled_by_default():
    assert cfg.WEEKLY_DIGEST_ENABLED is False


def test_load_config_overrides_weekly_digest(tmp_path):
    saved = cfg.WEEKLY_DIGEST_ENABLED
    try:
        conf = tmp_path / "config.toml"
        conf.write_text("[alerts]\nenabled = true\nweekly_digest = true\n", encoding="utf-8")
        cfg.load_config(conf)
        assert cfg.ALERTS_ENABLED is True
        assert cfg.WEEKLY_DIGEST_ENABLED is True
    finally:
        cfg.WEEKLY_DIGEST_ENABLED = saved
        cfg.ALERTS_ENABLED = False


def test_load_config_weekly_digest_defaults_false_when_omitted(tmp_path):
    saved = cfg.WEEKLY_DIGEST_ENABLED
    try:
        conf = tmp_path / "config.toml"
        conf.write_text("[alerts]\nenabled = true\n", encoding="utf-8")
        cfg.load_config(conf)
        assert cfg.WEEKLY_DIGEST_ENABLED is False
    finally:
        cfg.WEEKLY_DIGEST_ENABLED = saved
        cfg.ALERTS_ENABLED = False


# ---------------------------------------------------------------------------
# Layer: the gate (marker parse/compare against an injected "today", and the
# atomic marker write)
# ---------------------------------------------------------------------------
from analyze_ups import (  # noqa: E402
    _format_digest_marker,
    _iso_year_week,
    _parse_digest_marker,
    _should_fire_weekly_digest,
    _write_weekly_digest_marker,
)


def test_iso_year_week_matches_isocalendar():
    # 2026-07-06 is a Monday; isocalendar() gives (2026, 28, 1).
    assert _iso_year_week(pd.Timestamp("2026-07-06")) == (2026, 28)
    assert _iso_year_week(pd.Timestamp("2026-06-29")) == (2026, 27)


def test_iso_year_week_handles_the_iso_year_boundary():
    # Dec 29 2025 is a Monday that already belongs to ISO week 1 of 2026 —
    # the ISO year, not the calendar year, is what must be compared.
    assert _iso_year_week(pd.Timestamp("2025-12-29")) == (2026, 1)


def test_format_digest_marker_shape():
    assert _format_digest_marker(pd.Timestamp("2026-07-06")) == "2026-W28"


def test_parse_digest_marker_round_trips_format():
    assert _parse_digest_marker("2026-W28") == (2026, 28)
    assert _parse_digest_marker("2026-W28\n") == (2026, 28)


def test_parse_digest_marker_missing_file_is_none():
    assert _parse_digest_marker(None) is None


def test_parse_digest_marker_blank_is_none():
    assert _parse_digest_marker("") is None
    assert _parse_digest_marker("   \n") is None


def test_parse_digest_marker_malformed_is_none():
    assert _parse_digest_marker("not a marker") is None
    assert _parse_digest_marker("2026-06-01") is None  # the old daily format


def test_fires_on_first_run_ever_missing_marker():
    assert _should_fire_weekly_digest(None, pd.Timestamp("2026-07-06")) is True


def test_fires_on_first_run_of_a_new_iso_week():
    # Marker recorded last week (27); today is in week 28.
    assert _should_fire_weekly_digest("2026-W27", pd.Timestamp("2026-07-06")) is True


def test_noop_on_same_week_rerun():
    assert _should_fire_weekly_digest("2026-W28", pd.Timestamp("2026-07-06")) is False
    # Later the same ISO week (Sunday) is still a no-op.
    assert _should_fire_weekly_digest("2026-W28", pd.Timestamp("2026-07-12")) is False


def test_fires_after_a_week_rollover_across_a_year_boundary():
    # Marker recorded the last ISO week of 2025; today is ISO week 1 of 2026.
    assert _should_fire_weekly_digest("2025-W52", pd.Timestamp("2025-12-29")) is True


def test_does_not_fire_when_marker_is_ahead_of_today():
    # Clock skew / a marker from the future: today's week is not newer.
    assert _should_fire_weekly_digest("2026-W29", pd.Timestamp("2026-07-06")) is False


def test_fires_when_marker_is_malformed():
    assert _should_fire_weekly_digest("garbage", pd.Timestamp("2026-07-06")) is True


def test_fires_across_a_53_iso_week_year_boundary():
    # 2026 is a long ISO year with 53 weeks (2026-12-28 through 2027-01-03
    # are still ISO week 53 of 2026); the marker records that last week and
    # today is the first Monday that falls in 2027's own ISO week 1. The
    # digest must still fire even though the week NUMBER drops from 53 back
    # down to 1 — the ISO YEAR advanced, and _iso_year_week's tuple
    # comparison already orders (2027, 1) > (2026, 53) correctly (polish
    # item A6c).
    assert _should_fire_weekly_digest("2026-W53", pd.Timestamp("2027-01-04")) is True


def test_fires_from_week_52_to_53_within_the_same_53_week_year():
    assert _should_fire_weekly_digest("2026-W52", pd.Timestamp("2026-12-28")) is True


def test_write_weekly_digest_marker_success_leaves_no_tmp_file(tmp_path):
    marker = tmp_path / "last_digest.txt"
    ts = pd.Timestamp("2026-07-06")
    _write_weekly_digest_marker(marker, ts)
    assert marker.read_text(encoding="utf-8") == "2026-W28"
    assert not (tmp_path / "last_digest.txt.tmp").exists()


def test_write_weekly_digest_marker_creates_parent_dir(tmp_path):
    marker = tmp_path / "nested" / "last_digest.txt"
    _write_weekly_digest_marker(marker, pd.Timestamp("2026-07-06"))
    assert marker.read_text(encoding="utf-8") == "2026-W28"


def test_write_weekly_digest_marker_is_atomic(tmp_path, monkeypatch):
    """A failed rename must never leave a half-written marker: the write
    goes to a sibling temp file first, and only an atomic os.replace ever
    touches the real marker path."""
    import analyze_ups

    marker = tmp_path / "last_digest.txt"
    marker.write_text("2026-W20", encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("simulated failure")

    monkeypatch.setattr(analyze_ups.os, "replace", boom)
    with pytest.raises(OSError):
        _write_weekly_digest_marker(marker, pd.Timestamp("2026-07-06"))
    assert marker.read_text(encoding="utf-8") == "2026-W20"


def test_write_weekly_digest_marker_failure_cleans_up_the_tmp_sibling(tmp_path, monkeypatch):
    """A failed os.replace must not leave last_digest.txt.tmp behind on disk
    forever (polish item A6b)."""
    import analyze_ups

    marker = tmp_path / "last_digest.txt"

    def boom(*_a, **_k):
        raise OSError("simulated failure")

    monkeypatch.setattr(analyze_ups.os, "replace", boom)
    with pytest.raises(OSError):
        _write_weekly_digest_marker(marker, pd.Timestamp("2026-07-06"))
    assert not (tmp_path / "last_digest.txt.tmp").exists()


# ---------------------------------------------------------------------------
# Layer: line assembly (pure functions over a dict of already-computed
# summaries — nothing here computes a new statistic)
# ---------------------------------------------------------------------------
from analyze_ups import _build_weekly_digest_line, _weekly_digest_data  # noqa: E402

NOW = pd.Timestamp("2026-07-06 09:15:00")

_EMPTY_DIGEST_DATA = {
    "period_kwh": None, "period_cost_tiered": None, "period_partial": False,
    "forecast_kwh": None, "forecast_cost_tiered": None, "forecast_period_end": None,
    "voltage_anomalies_7d": 0, "episodes_7d": 0,
    "battery_status": None, "battery_replace_date": None,
    "biggest_day_date": None, "biggest_day_kwh": None,
    "stale_level": "fresh", "stale_age_hours": 0.0,
}


def _data(**overrides):
    d = dict(_EMPTY_DIGEST_DATA)
    d.update(overrides)
    return d


# ---- _build_weekly_digest_line ----
def test_line_starts_with_timestamp_and_tag():
    line = _build_weekly_digest_line(_data(), NOW)
    assert line.startswith("2026-07-06 09:15:00  weekly_digest  ")
    assert line.endswith("\n")


def test_line_always_carries_the_7day_counts_even_when_zero():
    line = _build_weekly_digest_line(_data(), NOW)
    assert "voltage_anomalies_7d=0" in line
    assert "on_battery_episodes_7d=0" in line


def test_line_reports_nonzero_7day_counts():
    line = _build_weekly_digest_line(_data(voltage_anomalies_7d=3, episodes_7d=1), NOW)
    assert "voltage_anomalies_7d=3" in line
    assert "on_battery_episodes_7d=1" in line


def test_line_omits_period_clause_when_unavailable():
    line = _build_weekly_digest_line(_data(), NOW)
    assert "period=" not in line


def test_line_includes_period_clause_with_partial_wording():
    line = _build_weekly_digest_line(
        _data(period_kwh=42.314, period_cost_tiered=3395.0, period_partial=True), NOW)
    assert "period=42.31 kWh" in line
    assert "CRC 3,395.00 tiered" in line
    assert "(partial)" in line


def test_line_period_clause_has_no_partial_wording_when_period_is_complete():
    line = _build_weekly_digest_line(
        _data(period_kwh=42.314, period_cost_tiered=3395.0, period_partial=False), NOW)
    assert "period=42.31 kWh" in line
    assert "(partial)" not in line


def test_line_omits_forecast_clause_when_unavailable():
    line = _build_weekly_digest_line(_data(), NOW)
    assert "forecast=" not in line


def test_line_includes_forecast_clause_worded_as_a_projection():
    line = _build_weekly_digest_line(_data(
        forecast_kwh=123.4, forecast_cost_tiered=9800.5,
        forecast_period_end=date(2026, 7, 31),
    ), NOW)
    assert "forecast=~123.40 kWh" in line
    assert "CRC 9,800.50 tiered" in line
    assert "by 2026-07-31" in line
    assert "projected" in line


def test_line_omits_battery_clause_when_status_is_insufficient_history():
    line = _build_weekly_digest_line(_data(battery_status="insufficient_history"), NOW)
    assert "battery=" not in line


def test_line_includes_battery_stable():
    line = _build_weekly_digest_line(_data(battery_status="stable"), NOW)
    assert "battery=stable" in line


def test_line_includes_battery_replace_date_when_projected():
    line = _build_weekly_digest_line(
        _data(battery_status="projected", battery_replace_date=date(2027, 1, 15)), NOW)
    assert "battery=replace ~2027-01-15" in line


def test_line_omits_staleness_caveat_when_fresh():
    """A clean, up-to-date week carries no staleness caveat at all (polish
    item A6a)."""
    line = _build_weekly_digest_line(_data(), NOW)
    assert "stale" not in line


def test_line_includes_staleness_caveat_when_warn():
    line = _build_weekly_digest_line(
        _data(stale_level="warn", stale_age_hours=14.2), NOW)
    assert "stale_data=warn(14.2h)" in line


def test_line_includes_staleness_caveat_when_crit():
    line = _build_weekly_digest_line(
        _data(stale_level="crit", stale_age_hours=60.75), NOW)
    assert "stale_data=crit(60.8h)" in line


def test_line_omits_biggest_day_clause_when_unavailable():
    line = _build_weekly_digest_line(_data(), NOW)
    assert "biggest_day_7d=" not in line


def test_line_includes_biggest_day_clause():
    line = _build_weekly_digest_line(
        _data(biggest_day_date=date(2026, 7, 5), biggest_day_kwh=12.345), NOW)
    assert "biggest_day_7d=2026-07-05" in line
    assert "12.35 kWh" in line


def test_line_with_every_clause_present():
    data = _data(
        period_kwh=42.31, period_cost_tiered=3395.0, period_partial=True,
        forecast_kwh=123.4, forecast_cost_tiered=9800.5,
        forecast_period_end=date(2026, 7, 31),
        voltage_anomalies_7d=2, episodes_7d=1,
        battery_status="projected", battery_replace_date=date(2027, 1, 15),
        biggest_day_date=date(2026, 7, 5), biggest_day_kwh=12.3,
    )
    line = _build_weekly_digest_line(data, NOW)
    for expected in ("period=", "forecast=", "voltage_anomalies_7d=2",
                      "on_battery_episodes_7d=1", "battery=replace ~2027-01-15",
                      "biggest_day_7d=2026-07-05"):
        assert expected in line


# ---- _weekly_digest_data ----
def _energy_summary(monthly_rows=None, daily_rows=None):
    es: dict = {}
    if monthly_rows is not None:
        es["monthly"] = pd.DataFrame(monthly_rows)
    if daily_rows is not None:
        es["daily"] = pd.DataFrame(daily_rows)
    return es


def test_weekly_digest_data_all_empty_inputs_yields_all_none_or_zero():
    data = _weekly_digest_data({}, {}, pd.DataFrame(), pd.DataFrame(), {}, NOW)
    assert data == _EMPTY_DIGEST_DATA


def test_weekly_digest_data_extracts_current_period():
    es = _energy_summary(monthly_rows=[
        {"month": "2026-06", "kwh": 200.0, "cost_tiered": 15000.0, "partial": False},
        {"month": "2026-07", "kwh": 42.31, "cost_tiered": 3395.0, "partial": True},
    ])
    data = _weekly_digest_data(es, {}, pd.DataFrame(), pd.DataFrame(), {}, NOW)
    assert data["period_kwh"] == pytest.approx(42.31)
    assert data["period_cost_tiered"] == pytest.approx(3395.0)
    assert data["period_partial"] is True


def test_weekly_digest_data_forecast_only_when_projected():
    forecast_not_ready = {"status": "insufficient_evidence"}
    data = _weekly_digest_data({}, forecast_not_ready, pd.DataFrame(), pd.DataFrame(), {}, NOW)
    assert data["forecast_kwh"] is None

    forecast_ready = {
        "status": "projected", "projected_kwh": 123.4,
        "projected_cost_tiered": 9800.5, "period_end": date(2026, 7, 31),
    }
    data = _weekly_digest_data({}, forecast_ready, pd.DataFrame(), pd.DataFrame(), {}, NOW)
    assert data["forecast_kwh"] == pytest.approx(123.4)
    assert data["forecast_cost_tiered"] == pytest.approx(9800.5)
    assert data["forecast_period_end"] == date(2026, 7, 31)


def test_weekly_digest_data_counts_anomalies_in_last_7_days_only():
    anomalies = pd.DataFrame({"ts": pd.to_datetime([
        "2026-06-20 00:00",   # more than 7 days before NOW -> excluded
        "2026-07-01 00:00",   # within the last 7 days -> included
        "2026-07-05 00:00",   # within the last 7 days -> included
    ])})
    data = _weekly_digest_data({}, {}, anomalies, pd.DataFrame(), {}, NOW)
    assert data["voltage_anomalies_7d"] == 2


def test_weekly_digest_data_counts_episodes_whose_end_falls_in_the_last_7_days():
    episodes = pd.DataFrame({
        "start": pd.to_datetime(["2026-06-15 00:00", "2026-07-04 00:00"]),
        "end": pd.to_datetime(["2026-06-15 01:00", "2026-07-04 02:00"]),
    })
    data = _weekly_digest_data({}, {}, pd.DataFrame(), episodes, {}, NOW)
    assert data["episodes_7d"] == 1


def test_weekly_digest_data_carries_battery_status_and_replace_date():
    battery = {"status": "projected", "replace_date": pd.Timestamp("2027-01-15")}
    data = _weekly_digest_data({}, {}, pd.DataFrame(), pd.DataFrame(), battery, NOW)
    assert data["battery_status"] == "projected"
    assert data["battery_replace_date"] == pd.Timestamp("2027-01-15")


def test_weekly_digest_data_defaults_staleness_to_fresh_when_not_provided():
    data = _weekly_digest_data({}, {}, pd.DataFrame(), pd.DataFrame(), {}, NOW)
    assert data["stale_level"] == "fresh"
    assert data["stale_age_hours"] == pytest.approx(0.0)


def test_weekly_digest_data_carries_staleness_when_provided():
    staleness = {"level": "warn", "age_hours": 14.2}
    data = _weekly_digest_data({}, {}, pd.DataFrame(), pd.DataFrame(), {}, NOW,
                               staleness=staleness)
    assert data["stale_level"] == "warn"
    assert data["stale_age_hours"] == pytest.approx(14.2)


def test_weekly_digest_data_picks_biggest_day_in_last_7_days():
    es = _energy_summary(daily_rows=[
        {"date": date(2026, 6, 20), "kwh": 99.0},   # older than 7 days -> excluded
        {"date": date(2026, 7, 1), "kwh": 5.0},
        {"date": date(2026, 7, 4), "kwh": 12.3},
    ])
    data = _weekly_digest_data(es, {}, pd.DataFrame(), pd.DataFrame(), {}, NOW)
    assert data["biggest_day_date"] == date(2026, 7, 4)
    assert data["biggest_day_kwh"] == pytest.approx(12.3)


# ---------------------------------------------------------------------------
# Layer: wiring (_maybe_write_weekly_digest requires both config flags,
# gates on the marker, appends, and advances the marker only on a real fire;
# then one true end-to-end check that analyze_ups.main() actually calls it)
# ---------------------------------------------------------------------------
def _digest_config(tmp_path, monkeypatch, *, alerts_enabled, weekly_digest_enabled):
    monkeypatch.setattr(cfg, "ALERTS_ENABLED", alerts_enabled)
    monkeypatch.setattr(cfg, "WEEKLY_DIGEST_ENABLED", weekly_digest_enabled)
    monkeypatch.setattr(cfg, "ALERTS_LOG", tmp_path / "alerts.log")
    monkeypatch.setattr(cfg, "LAST_DIGEST_MARKER", tmp_path / "last_digest.txt")


def test_maybe_write_weekly_digest_noop_when_alerts_disabled(tmp_path, monkeypatch):
    import analyze_ups
    _digest_config(tmp_path, monkeypatch, alerts_enabled=False, weekly_digest_enabled=True)
    path = analyze_ups._maybe_write_weekly_digest({}, {}, pd.DataFrame(), pd.DataFrame(), {}, NOW)
    assert path is None
    assert not (tmp_path / "alerts.log").exists()
    assert not (tmp_path / "last_digest.txt").exists()


def test_maybe_write_weekly_digest_noop_when_weekly_digest_disabled(tmp_path, monkeypatch):
    import analyze_ups
    _digest_config(tmp_path, monkeypatch, alerts_enabled=True, weekly_digest_enabled=False)
    path = analyze_ups._maybe_write_weekly_digest({}, {}, pd.DataFrame(), pd.DataFrame(), {}, NOW)
    assert path is None
    assert not (tmp_path / "alerts.log").exists()


def test_maybe_write_weekly_digest_fires_when_both_flags_set(tmp_path, monkeypatch):
    import analyze_ups
    _digest_config(tmp_path, monkeypatch, alerts_enabled=True, weekly_digest_enabled=True)
    path = analyze_ups._maybe_write_weekly_digest({}, {}, pd.DataFrame(), pd.DataFrame(), {}, NOW)
    assert path == cfg.ALERTS_LOG
    text = path.read_text(encoding="utf-8")
    assert "weekly_digest" in text
    assert (tmp_path / "last_digest.txt").read_text(encoding="utf-8") == _format_digest_marker(NOW)


def test_maybe_write_weekly_digest_is_a_noop_on_a_same_week_rerun(tmp_path, monkeypatch):
    import analyze_ups
    _digest_config(tmp_path, monkeypatch, alerts_enabled=True, weekly_digest_enabled=True)
    first = analyze_ups._maybe_write_weekly_digest({}, {}, pd.DataFrame(), pd.DataFrame(), {}, NOW)
    assert first is not None
    later_same_week = NOW + pd.Timedelta(days=1)
    second = analyze_ups._maybe_write_weekly_digest(
        {}, {}, pd.DataFrame(), pd.DataFrame(), {}, later_same_week)
    assert second is None
    text = (tmp_path / "alerts.log").read_text(encoding="utf-8")
    assert text.count("weekly_digest") == 1


def test_maybe_write_weekly_digest_fires_again_after_a_week_rollover(tmp_path, monkeypatch):
    import analyze_ups
    _digest_config(tmp_path, monkeypatch, alerts_enabled=True, weekly_digest_enabled=True)
    analyze_ups._maybe_write_weekly_digest({}, {}, pd.DataFrame(), pd.DataFrame(), {}, NOW)
    next_week = NOW + pd.Timedelta(days=7)
    second = analyze_ups._maybe_write_weekly_digest(
        {}, {}, pd.DataFrame(), pd.DataFrame(), {}, next_week)
    assert second is not None
    text = (tmp_path / "alerts.log").read_text(encoding="utf-8")
    assert text.count("weekly_digest") == 2


def _write_single_row_agent(agent_dir, when):
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "DataLog").write_text(
        "Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity\n"
        f"{when:%m/%d/%Y %H:%M:%S}\t120,0\t27,4\t15,0\t100\n",
        encoding="utf-8",
    )
    return agent_dir


def _hermetic_config(tmp_path):
    conf = tmp_path / "config.toml"
    conf.write_text("[archive]\nenabled = false\n", encoding="utf-8")
    return conf


def test_main_wires_the_weekly_digest_end_to_end(tmp_path, monkeypatch):
    """analyze_ups.main() itself calls the gate after the analysis
    completes: with both flags on, the first run of the week appends one
    weekly_digest line and writes the marker; a same-week rerun is a no-op.

    The clock is injected via STATEOFUPS_NOW (analyze_ups._wall_clock_now's
    test-only override) rather than the real datetime.now(), so both main()
    calls below are guaranteed to land in the same ISO week regardless of
    when the test actually runs — the previous version used the real clock
    for both calls, leaving a vanishing flake window right at an ISO-week
    boundary (polish item A6d)."""
    import analyze_ups
    _digest_config(tmp_path, monkeypatch, alerts_enabled=True, weekly_digest_enabled=True)
    fixed_now = datetime(2026, 7, 6, 9, 15, 0)
    monkeypatch.setenv("STATEOFUPS_NOW", fixed_now.isoformat())
    agent = _write_single_row_agent(tmp_path / "agent", fixed_now - pd.Timedelta(minutes=5))
    out = tmp_path / "d.html"
    argv = ["--agent-dir", str(agent), "-o", str(out), "--no-browser", "--quiet",
            "--no-snapshot", "--config", str(_hermetic_config(tmp_path))]

    analyze_ups.main(argv)
    alerts_text = cfg.ALERTS_LOG.read_text(encoding="utf-8")
    assert "weekly_digest" in alerts_text
    assert cfg.LAST_DIGEST_MARKER.exists()

    analyze_ups.main(argv)   # same week: must not append a second line
    alerts_text_after = cfg.ALERTS_LOG.read_text(encoding="utf-8")
    assert alerts_text_after.count("weekly_digest") == 1


def test_main_does_not_write_a_digest_when_weekly_digest_is_off(tmp_path, monkeypatch):
    import analyze_ups
    _digest_config(tmp_path, monkeypatch, alerts_enabled=True, weekly_digest_enabled=False)
    agent = _write_single_row_agent(tmp_path / "agent", datetime.now() - pd.Timedelta(minutes=5))
    out = tmp_path / "d.html"
    argv = ["--agent-dir", str(agent), "-o", str(out), "--no-browser", "--quiet",
            "--no-snapshot", "--config", str(_hermetic_config(tmp_path))]
    analyze_ups.main(argv)
    assert not cfg.LAST_DIGEST_MARKER.exists()
    # Mirrors the "on" test's alerts_text assertion: with the digest off,
    # alerts.log must carry no weekly_digest line either (it may not exist
    # at all for this minimal hermetic dataset, since no event-driven alert
    # condition is met).
    if cfg.ALERTS_LOG.exists():
        assert "weekly_digest" not in cfg.ALERTS_LOG.read_text(encoding="utf-8")


def test_main_survives_a_marker_write_failure_and_logs_a_warning(tmp_path, monkeypatch, capsys):
    """A failure in the marker write (disk full, an AV lock, ...) must not
    crash the run: main() completes normally (dashboard written, exit 0)
    with a clear warning logged — even though the digest line already
    landed in alerts.log, a known, acceptable duplicate on the next run
    (the marker never advanced), not a crash."""
    import analyze_ups
    _digest_config(tmp_path, monkeypatch, alerts_enabled=True, weekly_digest_enabled=True)
    agent = _write_single_row_agent(tmp_path / "agent", datetime.now() - pd.Timedelta(minutes=5))
    out = tmp_path / "d.html"
    argv = ["--agent-dir", str(agent), "-o", str(out), "--no-browser", "--quiet",
            "--no-snapshot", "--config", str(_hermetic_config(tmp_path))]

    def boom(*_a, **_k):
        raise OSError("simulated disk full")

    monkeypatch.setattr(analyze_ups, "_write_weekly_digest_marker", boom)

    rc = analyze_ups.main(argv)

    assert rc == 0
    assert out.exists()
    assert "weekly_digest" in cfg.ALERTS_LOG.read_text(encoding="utf-8")
    assert not cfg.LAST_DIGEST_MARKER.exists()
    captured = capsys.readouterr()
    assert "[warn]" in captured.err
    assert "weekly digest" in captured.err.lower()
