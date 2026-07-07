"""Unit tests for the log-staleness watchdog (roadmap item 31).

Nothing else notices when PCSS stops writing: a dead serial link, a stopped
service, or a wedged agent just means every analyzer run re-analyzes aging
data. `assess_staleness` compares the newest DataLog sample against an
injected wall clock (never `datetime.now()` deep in the pipeline) and
degrades the dashboard health pill, prints a console line, and feeds the
`[alerts]` trigger. The default thresholds are generous (12h warn / 48h crit)
so an ordinary evening with the PC off never reads as trouble, and the "stale
now" wording stays distinct from the historical gaps `detect_gaps` reports.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

import pcss.config as cfg
from pcss.common import fmt_age_hours
from pcss.dashboard import _build_health
from pcss.stats import assess_staleness

EMPTY = pd.DataFrame()


# ---------------------------------------------------------------- fmt_age_hours
def test_fmt_age_hours_under_a_day_uses_hours():
    assert fmt_age_hours(5.0) == "5.0 h"
    assert fmt_age_hours(47.9) == "47.9 h"


def test_fmt_age_hours_two_days_or_more_uses_days():
    assert fmt_age_hours(48.0) == "2.0 d"
    assert fmt_age_hours(100.0) == "4.2 d"


# ---------------------------------------------------------------- assess_staleness
def test_fresh_within_warn_threshold():
    now = pd.Timestamp("2026-06-01 12:00:00")
    newest = now - pd.Timedelta(hours=1)
    result = assess_staleness(newest, now, warn_hours=12, crit_hours=48)
    assert result["level"] == "fresh"
    assert result["age_hours"] == pytest.approx(1.0)


def test_warn_at_the_boundary():
    now = pd.Timestamp("2026-06-01 12:00:00")
    newest = now - pd.Timedelta(hours=12)
    result = assess_staleness(newest, now, warn_hours=12, crit_hours=48)
    assert result["level"] == "warn"


def test_warn_between_thresholds():
    now = pd.Timestamp("2026-06-01 12:00:00")
    newest = now - pd.Timedelta(hours=30)
    result = assess_staleness(newest, now, warn_hours=12, crit_hours=48)
    assert result["level"] == "warn"
    assert result["age_hours"] == pytest.approx(30.0)


def test_crit_at_the_boundary():
    now = pd.Timestamp("2026-06-01 12:00:00")
    newest = now - pd.Timedelta(hours=48)
    result = assess_staleness(newest, now, warn_hours=12, crit_hours=48)
    assert result["level"] == "crit"


def test_crit_beyond_threshold():
    now = pd.Timestamp("2026-06-01 12:00:00")
    newest = now - pd.Timedelta(hours=100)
    result = assess_staleness(newest, now, warn_hours=12, crit_hours=48)
    assert result["level"] == "crit"
    assert result["age_hours"] == pytest.approx(100.0)


def test_uses_config_defaults_when_thresholds_omitted(monkeypatch):
    monkeypatch.setattr(cfg, "STALE_WARN_HOURS", 2.0)
    monkeypatch.setattr(cfg, "STALE_CRIT_HOURS", 4.0)
    now = pd.Timestamp("2026-06-01 12:00:00")
    newest = now - pd.Timedelta(hours=3)
    result = assess_staleness(newest, now)
    assert result["level"] == "warn"      # 3h sits between the patched 2h/4h


def test_negative_age_from_clock_skew_is_clamped():
    now = pd.Timestamp("2026-06-01 12:00:00")
    newest = now + pd.Timedelta(minutes=5)   # "newest" in the future (clock skew)
    result = assess_staleness(newest, now, warn_hours=12, crit_hours=48)
    assert result["age_hours"] == 0.0
    assert result["level"] == "fresh"


def test_naive_plain_datetimes_work_too():
    now = datetime(2026, 6, 1, 12, 0, 0)
    newest = datetime(2026, 5, 30, 12, 0, 0)   # 48h earlier, naive local like ts_2010_to_dt
    result = assess_staleness(newest, now, warn_hours=12, crit_hours=48)
    assert result["level"] == "crit"
    assert result["age_hours"] == pytest.approx(48.0)


# ---------------------------------------------------------------- config defaults
def test_config_defaults_are_generous():
    assert pytest.approx(12.0) == cfg.STALE_WARN_HOURS
    assert pytest.approx(48.0) == cfg.STALE_CRIT_HOURS


def test_load_config_overrides_stale_thresholds(tmp_path):
    saved = (cfg.STALE_WARN_HOURS, cfg.STALE_CRIT_HOURS, cfg.DASHBOARD_HTML)
    try:
        conf = tmp_path / "config.toml"
        conf.write_text(
            "[thresholds]\nstale_warn_hours = 6\nstale_crit_hours = 24\n", encoding="utf-8")
        cfg.load_config(conf)
        assert pytest.approx(6.0) == cfg.STALE_WARN_HOURS
        assert pytest.approx(24.0) == cfg.STALE_CRIT_HOURS
    finally:
        cfg.STALE_WARN_HOURS, cfg.STALE_CRIT_HOURS, cfg.DASHBOARD_HTML = saved


# ---------------------------------------------------------------- health pill
def test_health_pill_unaffected_when_staleness_none():
    health = _build_health([], None, EMPTY, EMPTY, EMPTY)
    assert health["color"] == "green"
    assert health["label"] == "All systems nominal"


def test_health_pill_unaffected_when_fresh():
    staleness = {"level": "fresh", "age_hours": 1.0}
    health = _build_health([], None, EMPTY, EMPTY, EMPTY, staleness=staleness)
    assert health["color"] == "green"
    assert "no new samples" not in health["sub"]


def test_health_pill_degrades_amber_on_stale_warn():
    staleness = {"level": "warn", "age_hours": 15.0}
    health = _build_health([], None, EMPTY, EMPTY, EMPTY, staleness=staleness)
    assert health["color"] == "amber"
    assert "no new samples in" in health["sub"]
    assert "15.0 h" in health["sub"]


def test_health_pill_degrades_red_on_stale_crit():
    staleness = {"level": "crit", "age_hours": 100.0}
    health = _build_health([], None, EMPTY, EMPTY, EMPTY, staleness=staleness)
    assert health["color"] == "red"
    assert "no new samples in" in health["sub"]
    assert "4.2 d" in health["sub"]


def test_health_pill_stale_crit_outranks_existing_warn_kpi():
    staleness = {"level": "crit", "age_hours": 72.0}
    health = _build_health(["warn"], None, EMPTY, EMPTY, EMPTY, staleness=staleness)
    assert health["color"] == "red"
    assert "no new samples in" in health["sub"]
    assert "near limits" in health["sub"]
    assert "outside normal range" not in health["sub"]


def test_health_pill_stale_wording_distinct_from_gap_wording():
    """'Stale now' must read differently from the historical DataLog gaps
    that detect_gaps already reports — both facts show up, but neither
    sentence borrows the other's wording."""
    gaps = pd.DataFrame({
        "from": pd.to_datetime(["2026-01-01 00:00"]),
        "to": pd.to_datetime(["2026-01-01 01:00"]),
        "duration_min": [60.0],
    })
    staleness = {"level": "crit", "age_hours": 100.0}
    health = _build_health([], None, EMPTY, EMPTY, gaps, staleness=staleness)
    assert "no new samples in" in health["sub"]
    assert "1 gap" in health["sub"]


# ---------------------------------------------------------------- alerts trigger
def _hermetic_config(tmp_path):
    conf = tmp_path / "config.toml"
    conf.write_text("[archive]\nenabled = false\n", encoding="utf-8")
    return conf


def test_maybe_write_alerts_fires_on_staleness_alone(tmp_path, monkeypatch):
    import analyze_ups
    monkeypatch.setattr(cfg, "ALERTS_ENABLED", True)
    monkeypatch.setattr(cfg, "ALERTS_LOG", tmp_path / "alerts.log")
    staleness = {"level": "crit", "age_hours": 50.0}
    path = analyze_ups._maybe_write_alerts(EMPTY, EMPTY, EMPTY, staleness)
    assert path == cfg.ALERTS_LOG
    text = path.read_text(encoding="utf-8")
    assert "stale=crit" in text
    assert "50.0h" in text


def test_maybe_write_alerts_silent_when_all_fresh_and_empty(tmp_path, monkeypatch):
    import analyze_ups
    monkeypatch.setattr(cfg, "ALERTS_ENABLED", True)
    monkeypatch.setattr(cfg, "ALERTS_LOG", tmp_path / "alerts.log")
    path = analyze_ups._maybe_write_alerts(EMPTY, EMPTY, EMPTY, None)
    assert path is None
    assert not (tmp_path / "alerts.log").exists()


# ---------------------------------------------------------------- end-to-end (analyze_ups.main)
def _write_single_row_agent(agent_dir, when):
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "DataLog").write_text(
        "Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity\n"
        f"{when:%m/%d/%Y %H:%M:%S}\t120,0\t27,4\t15,0\t100\n",
        encoding="utf-8",
    )
    return agent_dir


def test_console_reports_stale_data_feed(tmp_path, capsys):
    import analyze_ups
    agent = _write_single_row_agent(tmp_path / "agent", datetime(2000, 1, 1))
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path))])
    out = capsys.readouterr().out
    assert "no new samples in" in out
    assert "crit" in out.lower()


def test_console_silent_when_data_is_fresh(tmp_path, capsys):
    import analyze_ups
    agent = _write_single_row_agent(tmp_path / "agent", datetime.now() - timedelta(minutes=5))
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path))])
    out = capsys.readouterr().out
    assert "no new samples in" not in out


def test_dashboard_health_pill_shows_stale_reason(tmp_path):
    import analyze_ups
    agent = _write_single_row_agent(tmp_path / "agent", datetime(2000, 1, 1))
    out = tmp_path / "d.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path))])
    html = out.read_text(encoding="utf-8")
    assert "no new samples in" in html
