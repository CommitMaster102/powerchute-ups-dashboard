"""Unit tests for the end-of-period cost forecast (roadmap item 27).

The billing-period grouping (item 8) and the tariff-history rate lookup
(item 17) make an end-of-period forecast mostly a lookup: project the current
period's recorded kWh to the period's end date, price it both flat and
tiered, and name the date the tier limit will be crossed. `forecast_period_cost`
in `pcss/stats.py` does the math; `pcss/dashboard.py`'s `_forecast_sub` words
it for the Period Comparison card subtitle; `analyze_ups.py` prints a console
line and adds a `forecast` key to `--json`. Every surface says "projected" /
"at the current pace", never a plain measurement, and a period with fewer
than `[tariff] forecast_min_days` (default 5) distinct days of energylog
evidence gets the honest "not enough of the period recorded yet" with no
numbers — the same floor pattern as `battery_replace_projection`.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from pcss import config
from pcss.config import TariffPeriod
from pcss.stats import compute_energy_summary, forecast_period_cost


def _edf(rows, interval_sec=3600):
    """(ts, power_w) tuples -> energylog-shaped frame at a fixed interval."""
    df = pd.DataFrame(rows, columns=["ts", "power_w"])
    df["ts"] = pd.to_datetime(df["ts"])
    df["interval_sec"] = interval_sec
    return df.sort_values("ts").reset_index(drop=True)


# ---------------------------------------------------------------- config default
def test_config_default_is_five_days():
    assert pytest.approx(5.0) == config.FORECAST_MIN_DAYS


def test_load_config_overrides_forecast_min_days(tmp_path):
    saved = config.FORECAST_MIN_DAYS
    try:
        conf = tmp_path / "config.toml"
        conf.write_text("[tariff]\nforecast_min_days = 10\n", encoding="utf-8")
        config.load_config(conf)
        assert pytest.approx(10.0) == config.FORECAST_MIN_DAYS
    finally:
        config.FORECAST_MIN_DAYS = saved


# ---------------------------------------------------------------- forecast_period_cost
def test_below_floor_returns_no_numbers():
    """Three distinct days of evidence, below the default 5-day floor: the
    honest result carries no projected numbers at all."""
    es = compute_energy_summary(_edf([
        ("2026-01-05 12:00", 1000.0),
        ("2026-01-10 12:00", 1000.0),
        ("2026-01-15 12:00", 1000.0),
    ]))
    result = forecast_period_cost(es)
    assert result["status"] == "insufficient_evidence"
    assert result["evidence_days"] == 3
    assert result["min_days"] == pytest.approx(config.FORECAST_MIN_DAYS)
    assert result["projected_kwh"] is None
    assert result["projected_cost_pcss"] is None
    assert result["projected_cost_tiered"] is None
    assert result["tier_cross_date"] is None
    assert result["already_crossed"] is False


def test_empty_energy_summary_is_insufficient():
    assert forecast_period_cost({})["status"] == "insufficient_evidence"
    assert forecast_period_cost({})["evidence_days"] == 0


def test_projection_with_known_numbers():
    """Six days at exactly 2 kWh/day (1 sample/day, 2000 W for one hour):
    the per-day mean is 2 kWh, projected across January's 31 days is 62 kWh,
    priced at the current flat and tiered rates (well under the 200 kWh
    tier, so the tiered cost is the low rate throughout)."""
    rows = [(f"2026-01-{d:02d} 00:00", 2000.0) for d in range(1, 7)]
    es = compute_energy_summary(_edf(rows))
    result = forecast_period_cost(es)
    assert result["status"] == "projected"
    assert result["evidence_days"] == 6
    assert result["period_start"] == date(2026, 1, 1)
    assert result["period_end"] == date(2026, 2, 1)
    assert result["projected_kwh"] == pytest.approx(2.0 * 31)
    assert result["projected_cost_pcss"] == pytest.approx(62.0 * config.PCSS_FLAT_RATE)
    assert result["projected_cost_tiered"] == pytest.approx(62.0 * config.COOPESANTOS_LOW_RATE)
    assert result["already_crossed"] is False
    assert result["tier_cross_date"] is None
    assert result["rate_tag"] == "current rates"


def test_tier_cross_date_arithmetic():
    """Six days at 10 kWh/day project to 310 kWh over January — comfortably
    past the 200 kWh tier limit. The crossing date is the linear back-solve
    from the period start: tier_limit / per_day_kwh days in."""
    rows = [(f"2026-01-{d:02d} 00:00", 10000.0) for d in range(1, 7)]
    es = compute_energy_summary(_edf(rows))
    result = forecast_period_cost(es)
    assert result["status"] == "projected"
    assert result["already_crossed"] is False
    tier_limit = config.COOPESANTOS_TIER_LIMIT_KWH
    expected_date = date(2026, 1, 1) + timedelta(days=tier_limit / 10.0)
    assert result["tier_cross_date"] == expected_date
    assert result["projected_kwh"] == pytest.approx(10.0 * 31)


def test_already_crossed_tier_limit():
    """When the *recorded* kWh so far already exceeds the tier limit, the
    result says so directly instead of projecting a crossing date."""
    tier_limit = config.COOPESANTOS_TIER_LIMIT_KWH
    per_day_kwh = tier_limit / 4.0   # 6 days of evidence => 1.5x the tier limit already recorded
    power_w = per_day_kwh * 1000.0
    rows = [(f"2026-01-{d:02d} 00:00", power_w) for d in range(1, 7)]
    es = compute_energy_summary(_edf(rows))
    result = forecast_period_cost(es)
    assert result["status"] == "projected"
    assert result["already_crossed"] is True
    assert result["tier_cross_date"] is None
    assert result["projected_kwh"] == pytest.approx(per_day_kwh * 31)


def test_uses_tariff_history_rates_for_current_period(monkeypatch):
    """With [[tariff.history]] configured (item 17), the forecast prices the
    current period with the rates in force on the period's own start date —
    reusing config.tariff_rates_for rather than a second rate lookup."""
    monkeypatch.setattr(config, "TARIFF_HISTORY", [
        TariffPeriod(date(2026, 1, 1), coopesantos_low=70.0, coopesantos_high=110.0,
                     tier_limit_kwh=200.0, pcss_flat=110.0),
    ])
    rows = [(f"2026-01-{d:02d} 00:00", 2000.0) for d in range(1, 7)]
    es = compute_energy_summary(_edf(rows))
    result = forecast_period_cost(es)
    assert result["rate_tag"] == "rates from 2026-01-01"
    assert result["projected_cost_pcss"] == pytest.approx(result["projected_kwh"] * 110.0)
    assert result["projected_cost_tiered"] == pytest.approx(result["projected_kwh"] * 70.0)


def test_exactly_one_evidence_day_is_below_the_default_floor():
    """A single distinct day of evidence is still below the default 5-day
    floor — the honest "not enough" result, not a projection from one
    sample (polish item A3c)."""
    es = compute_energy_summary(_edf([("2026-01-05 12:00", 1000.0)]))
    result = forecast_period_cost(es)
    assert result["status"] == "insufficient_evidence"
    assert result["evidence_days"] == 1
    assert result["projected_kwh"] is None


def test_projection_landing_exactly_at_the_tier_limit(monkeypatch):
    """A projection that lands exactly on the tier limit (not over it): 5
    days at a clean 10 kWh/day, projected across January's 31 days is
    310 kWh — set as the tier limit itself. kwh recorded so far (50 kWh)
    stays under the limit, so this must read as a projected crossing right
    at the period's own end date, not as already_crossed (polish item A3c).
    """
    monkeypatch.setattr(config, "COOPESANTOS_TIER_LIMIT_KWH", 310.0)
    rows = [(f"2026-01-{d:02d} 00:00", 10000.0) for d in range(1, 6)]   # 5 days, 10 kWh/day
    es = compute_energy_summary(_edf(rows))
    result = forecast_period_cost(es)
    assert result["status"] == "projected"
    assert result["evidence_days"] == 5
    assert result["projected_kwh"] == pytest.approx(310.0)
    assert result["already_crossed"] is False
    assert result["tier_cross_date"] == date(2026, 2, 1)


def test_min_days_argument_overrides_config():
    rows = [(f"2026-01-{d:02d} 00:00", 2000.0) for d in range(1, 4)]   # 3 days
    es = compute_energy_summary(_edf(rows))
    assert forecast_period_cost(es)["status"] == "insufficient_evidence"
    assert forecast_period_cost(es, min_days=3.0)["status"] == "projected"


def test_current_period_is_the_most_recent_one():
    """With two billing periods recorded, the forecast targets the most
    recent (matching the Period Comparison card's own current-period
    choice), not the earlier, already-closed one."""
    rows = [(f"2026-04-{d:02d} 12:00", 1000.0) for d in range(20, 31)]
    rows += [(f"2026-05-{d:02d} 12:00", 1000.0) for d in range(1, 7)]
    es = compute_energy_summary(_edf(rows))
    result = forecast_period_cost(es)
    assert result["period_start"] == date(2026, 5, 1)
    assert result["period_end"] == date(2026, 6, 1)
    assert result["evidence_days"] == 6


# ---------------------------------------------------------------- dashboard subtitle
def test_forecast_sub_insufficient():
    from pcss.dashboard import _forecast_sub
    text = _forecast_sub({"status": "insufficient_evidence", "evidence_days": 3,
                          "min_days": 5.0})
    assert "not enough of the period recorded yet" in text
    assert "3" in text and "5" in text


def test_forecast_sub_projected_no_cross():
    from pcss.dashboard import _forecast_sub
    text = _forecast_sub({
        "status": "projected", "projected_kwh": 62.0,
        "period_end": date(2026, 1, 31), "already_crossed": False,
        "tier_cross_date": None,
    })
    assert "projected" in text
    assert "62.0" in text
    assert "2026-01-31" in text
    assert "at the current pace" in text


def test_forecast_sub_tier_cross():
    from pcss.dashboard import _forecast_sub
    text = _forecast_sub({
        "status": "projected", "projected_kwh": 310.0,
        "period_end": date(2026, 1, 31), "already_crossed": False,
        "tier_cross_date": date(2026, 1, 21),
    })
    assert "tier crosses" in text
    assert "2026-01-21" in text


def test_forecast_sub_already_crossed():
    from pcss.dashboard import _forecast_sub
    text = _forecast_sub({
        "status": "projected", "projected_kwh": 1240.0,
        "period_end": date(2026, 1, 31), "already_crossed": True,
        "tier_cross_date": None,
    })
    assert "already" in text.lower()


def test_forecast_sub_spanish(monkeypatch):
    from pcss.dashboard import _forecast_sub
    monkeypatch.setattr(config, "DASHBOARD_LANGUAGE", "es")
    text = _forecast_sub({"status": "insufficient_evidence", "evidence_days": 3,
                          "min_days": 5.0})
    assert "período" in text


def test_forecast_sub_projected_spanish(monkeypatch):
    from pcss.dashboard import _forecast_sub
    monkeypatch.setattr(config, "DASHBOARD_LANGUAGE", "es")
    text = _forecast_sub({
        "status": "projected", "projected_kwh": 62.0,
        "period_end": date(2026, 1, 31), "already_crossed": False,
        "tier_cross_date": None,
    })
    assert "proyectado" in text
    assert "ritmo actual" in text


# ---------------------------------------------------------------- end-to-end (analyze_ups.main)
def _write_energy_agent(agent_dir, days, start=datetime(2026, 1, 1), power_w=200.0):
    """A DataLog + energylog pair spanning `days` full days from `start`, all
    within one calendar month so the billing period stays simple."""
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "energylog").mkdir(exist_ok=True)
    dl = ["Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity"]
    for i in range(days * 72):                              # 20-min cadence
        t = start + timedelta(minutes=20 * i)
        dl.append(f"{t:%m/%d/%Y %H:%M:%S}\t120,0\t27,4\t15,0\t100")
    (agent_dir / "DataLog").write_text("\n".join(dl) + "\n", encoding="utf-8")
    el = [f"# $month={start:%Y-%m}", "# $interval=300", "# $calculatedMaxLoad=1400.0"]
    for i in range(days * 288):                              # 5-min cadence
        secs = (start + timedelta(minutes=5 * i) - datetime(2010, 1, 1)).total_seconds()
        el.append(f"{secs:.0f};null;{power_w / 1400 * 100:.4f};{power_w:.1f}")
    (agent_dir / "energylog" / f"{start:%Y-%m}.log").write_text("\n".join(el) + "\n", encoding="utf-8")
    return agent_dir


def _hermetic_config(tmp_path):
    conf = tmp_path / "config.toml"
    conf.write_text("[archive]\nenabled = false\n", encoding="utf-8")
    return conf


def test_console_shows_insufficient_evidence(tmp_path, capsys):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=2)
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path))])
    out = capsys.readouterr().out
    assert "not enough of the period recorded yet" in out
    assert "2/5" in out


def test_console_shows_projected_forecast(tmp_path, capsys):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=6)
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path))])
    out = capsys.readouterr().out
    assert "Forecast" in out
    assert "projected" in out.lower()
    assert "2026-02-01" in out


def test_console_forecast_names_the_rates_used(tmp_path, capsys):
    """With tariff history active, the console forecast line names which
    rates priced the projection (polish item A3b) — the same bracket-tag
    style the monthly-breakdown line right above it already uses. This is a
    console-only surface, so the localized-wording rule the dashboard's
    _forecast_sub follows does not apply here."""
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=6)
    conf = tmp_path / "config.toml"
    conf.write_text(
        "[archive]\nenabled = false\n"
        "[[tariff.history]]\n"
        'effective_from = "2026-01-01"\n'
        "coopesantos_low = 70.0\ncoopesantos_high = 110.0\n"
        "tier_limit_kwh = 200.0\npcss_flat = 110.0\n",
        encoding="utf-8",
    )
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot", "--config", str(conf)])
    out = capsys.readouterr().out
    forecast_line = next(line for line in out.splitlines() if line.strip().startswith("Forecast"))
    assert "rates from 2026-01-01" in forecast_line


def test_json_summary_has_forecast_key(tmp_path):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=6)
    j = tmp_path / "out.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path)), "--json", str(j)])
    import json as _json
    data = _json.loads(j.read_text())
    assert data["forecast"]["status"] == "projected"
    assert data["forecast"]["evidence_days"] == 6
    assert data["forecast"]["period_end"] == "2026-02-01"


def test_json_summary_forecast_key_insufficient(tmp_path):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=2)
    j = tmp_path / "out.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path)), "--json", str(j)])
    import json as _json
    data = _json.loads(j.read_text())
    assert data["forecast"]["status"] == "insufficient_evidence"
    assert data["forecast"]["projected_kwh"] is None


def test_dashboard_shows_forecast_subtitle(tmp_path):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=6)
    out = tmp_path / "d.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path))])
    html = out.read_text(encoding="utf-8")
    assert "at the current pace" in html


def test_dashboard_shows_insufficient_evidence_subtitle(tmp_path):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=2)
    out = tmp_path / "d.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path))])
    html = out.read_text(encoding="utf-8")
    assert "not enough of the period recorded yet" in html
