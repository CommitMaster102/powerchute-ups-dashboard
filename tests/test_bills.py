"""Unit tests for bill reconciliation.

A user-owned bills.csv records what Coopesantos actually billed for the
whole house per billing period (period_start, kwh, amount_crc).
`pcss.loaders.load_bills` reads it defensively — a missing file silently
disables the feature, and a malformed row is reported and skipped rather
than raising — and `pcss.stats.reconcile_bills` joins the parsed rows
against the analyzer's own per-billing-period UPS kWh (the "monthly" frame
from `compute_energy_summary`), refusing to join a bill whose period_start
does not exactly match a `_billing_period_start` boundary for the
configured `billing_cycle_start_day`. Every human-facing surface says "the
UPS-metered share of the billed consumption" — the UPS sees only its own
outlets, never a whole-house meter.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd
import pytest

from pcss import config
from pcss.loaders import load_bills
from pcss.stats import compute_energy_summary, reconcile_bills


def _write_bills(tmp_path, text):
    p = tmp_path / "bills.csv"
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------- loader
def test_default_bills_file_is_repo_root_bills_csv():
    assert config.BILLS_FILE.name == "bills.csv"


def test_missing_file_returns_empty_with_no_warnings(tmp_path):
    df, warnings = load_bills(tmp_path / "does-not-exist.csv")
    assert df.empty
    assert warnings == []


def test_loads_a_valid_file(tmp_path):
    p = _write_bills(
        tmp_path,
        "period_start,kwh,amount_crc\n"
        "2026-01-01,412.5,45123.00\n"
        "2026-02-01,398.0,43850.50\n",
    )
    df, warnings = load_bills(p)
    assert warnings == []
    assert list(df["period_start"]) == [date(2026, 1, 1), date(2026, 2, 1)]
    assert df["kwh"].tolist() == pytest.approx([412.5, 398.0])
    assert df["amount_crc"].tolist() == pytest.approx([45123.00, 43850.50])


def test_malformed_date_is_skipped_and_reported(tmp_path):
    p = _write_bills(
        tmp_path,
        "period_start,kwh,amount_crc\n"
        "not-a-date,412.5,45123.00\n"
        "2026-02-01,398.0,43850.50\n",
    )
    df, warnings = load_bills(p)
    assert len(df) == 1
    assert df["period_start"].iloc[0] == date(2026, 2, 1)
    assert len(warnings) == 1
    assert "not-a-date" in warnings[0]


def test_non_numeric_value_is_skipped_and_reported(tmp_path):
    p = _write_bills(
        tmp_path,
        "period_start,kwh,amount_crc\n"
        "2026-01-01,not-a-number,45123.00\n"
        "2026-02-01,398.0,43850.50\n",
    )
    df, warnings = load_bills(p)
    assert len(df) == 1
    assert df["period_start"].iloc[0] == date(2026, 2, 1)
    assert len(warnings) == 1
    assert "2026-01-01" in warnings[0]


def test_zero_kwh_row_is_skipped_and_reported(tmp_path):
    p = _write_bills(
        tmp_path,
        "period_start,kwh,amount_crc\n"
        "2026-01-01,0,45123.00\n"
        "2026-02-01,398.0,43850.50\n",
    )
    df, warnings = load_bills(p)
    assert len(df) == 1
    assert df["period_start"].iloc[0] == date(2026, 2, 1)
    assert len(warnings) == 1
    assert "2026-01-01" in warnings[0]


def test_negative_kwh_row_is_skipped_and_reported(tmp_path):
    p = _write_bills(
        tmp_path,
        "period_start,kwh,amount_crc\n"
        "2026-01-01,-10,45123.00\n"
        "2026-02-01,398.0,43850.50\n",
    )
    df, warnings = load_bills(p)
    assert len(df) == 1
    assert df["period_start"].iloc[0] == date(2026, 2, 1)
    assert len(warnings) == 1
    assert "2026-01-01" in warnings[0]


def test_negative_amount_row_is_skipped_and_reported(tmp_path):
    p = _write_bills(
        tmp_path,
        "period_start,kwh,amount_crc\n"
        "2026-01-01,412.5,-45123.00\n"
        "2026-02-01,398.0,43850.50\n",
    )
    df, warnings = load_bills(p)
    assert len(df) == 1
    assert df["period_start"].iloc[0] == date(2026, 2, 1)
    assert len(warnings) == 1
    assert "2026-01-01" in warnings[0]


def test_blank_line_does_not_shift_reported_line_number(tmp_path):
    """A blank line inside bills.csv must not shift which physical line
    number a later malformed row is reported at: csv.DictReader silently
    skips blank lines internally, so a naive enumerate() counter over the
    yielded rows would undercount by one for every blank line seen before
    the bad row (polish item A4b). Physical lines here: 1=header,
    2=good row, 3=blank, 4=bad row."""
    p = _write_bills(
        tmp_path,
        "period_start,kwh,amount_crc\n"
        "2026-01-01,412.5,45123.00\n"
        "\n"
        "not-a-date,398.0,43850.50\n",
    )
    df, warnings = load_bills(p)
    assert len(df) == 1
    assert len(warnings) == 1
    assert "line 4" in warnings[0]


def test_missing_required_column_reports_and_ignores_the_file(tmp_path):
    p = _write_bills(tmp_path, "period_start,kwh\n2026-01-01,412.5\n")
    df, warnings = load_bills(p)
    assert df.empty
    assert len(warnings) == 1
    assert "amount_crc" in warnings[0]


# ---------------------------------------------------------------- reconcile_bills
def _edf(rows):
    """(ts, power_w) tuples -> energylog-shaped frame at 300 s intervals."""
    df = pd.DataFrame(rows, columns=["ts", "power_w"])
    df["ts"] = pd.to_datetime(df["ts"])
    df["interval_sec"] = 300
    return df.sort_values("ts").reset_index(drop=True)


def _bills(rows):
    return pd.DataFrame(rows, columns=["period_start", "kwh", "amount_crc"])


def test_empty_bills_yields_empty_reconciliation_with_no_warnings():
    es = compute_energy_summary(_edf([("2026-05-14 12:00", 1200.0)]))
    reconciled, warnings = reconcile_bills(_bills([]), es)
    assert reconciled.empty
    assert warnings == []


def test_empty_energy_summary_excludes_every_bill_with_a_report():
    bills = _bills([(date(2026, 5, 1), 300.0, 40000.0)])
    reconciled, warnings = reconcile_bills(bills, {})
    assert reconciled.empty
    assert len(warnings) == 1
    assert "2026-05-01" in warnings[0]


def test_aligned_bill_reconciles_with_known_numbers():
    """A full calendar month (May) of a constant 1000 W draw is exactly
    1000 W * 24 h * 31 d / 1000 = 744 kWh of UPS-metered energy. Billing the
    whole house for exactly ups_kwh / 0.24 kWh at CRC 130/kWh gives an exact
    24% UPS-metered share and a 130.00 implied rate — both computed from the
    analyzer's own numbers rather than hardcoded, so the test only pins the
    arithmetic, not the pipeline's kWh total."""
    rows = [(ts, 1000.0) for ts in pd.date_range("2026-05-01", "2026-05-31 23:55", freq="5min")]
    es = compute_energy_summary(_edf(rows))
    ups_kwh = float(es["monthly"]["kwh"].iloc[0])
    billed_kwh = ups_kwh / 0.24
    amount_crc = billed_kwh * 130.0
    bills = _bills([(date(2026, 5, 1), billed_kwh, amount_crc)])
    reconciled, warnings = reconcile_bills(bills, es)
    assert warnings == []
    assert len(reconciled) == 1
    row = reconciled.iloc[0]
    assert row["ups_kwh"] == pytest.approx(ups_kwh)
    assert row["billed_kwh"] == pytest.approx(billed_kwh)
    assert row["share_pct"] == pytest.approx(24.0, rel=1e-6)
    assert row["implied_rate_crc_per_kwh"] == pytest.approx(130.0)
    assert row["ups_cost_tiered"] == pytest.approx(float(es["monthly"]["cost_tiered"].iloc[0]))
    assert row["rate_tag"] == "current rates"
    assert bool(row["partial"]) is False


def test_boundary_mismatch_in_second_half_reports_the_following_boundary(monkeypatch):
    monkeypatch.setattr(config, "BILLING_CYCLE_START_DAY", 15)
    es = compute_energy_summary(_edf([("2026-05-20 12:00", 1200.0)]))
    # Billing periods run April 15 -> May 15 -> ...; May 1 is not a boundary.
    # It sits 16 days after April 15 but only 14 days before May 15, so the
    # truly nearest boundary is the FOLLOWING one, May 15 — not April 15.
    bills = _bills([(date(2026, 5, 1), 500.0, 60000.0)])
    reconciled, warnings = reconcile_bills(bills, es)
    assert reconciled.empty
    assert len(warnings) == 1
    assert "2026-05-01" in warnings[0]
    assert "2026-05-15" in warnings[0]


def test_boundary_mismatch_in_first_half_reports_the_preceding_boundary(monkeypatch):
    monkeypatch.setattr(config, "BILLING_CYCLE_START_DAY", 15)
    es = compute_energy_summary(_edf([("2026-04-20 12:00", 1200.0)]))
    # Same April 15 -> May 15 period; April 20 sits only 5 days after the
    # preceding boundary and 25 days before the following one, so the
    # nearest boundary is the PRECEDING one, April 15.
    bills = _bills([(date(2026, 4, 20), 500.0, 60000.0)])
    reconciled, warnings = reconcile_bills(bills, es)
    assert reconciled.empty
    assert len(warnings) == 1
    assert "2026-04-20" in warnings[0]
    assert "2026-04-15" in warnings[0]


def test_boundary_mismatch_exactly_midway_reports_the_earlier_boundary(monkeypatch):
    monkeypatch.setattr(config, "BILLING_CYCLE_START_DAY", 15)
    es = compute_energy_summary(_edf([("2026-04-30 12:00", 1200.0)]))
    # April 15 -> May 15 is 30 days long; April 30 is exactly 15 days from
    # each boundary — an exact tie, which resolves to the earlier boundary.
    bills = _bills([(date(2026, 4, 30), 500.0, 60000.0)])
    reconciled, warnings = reconcile_bills(bills, es)
    assert reconciled.empty
    assert len(warnings) == 1
    assert "2026-04-30" in warnings[0]
    assert "2026-04-15" in warnings[0]


def test_period_with_no_matching_ups_data_is_excluded_and_reported():
    es = compute_energy_summary(_edf([("2026-05-14 12:00", 1200.0)]))
    bills = _bills([(date(2026, 6, 1), 300.0, 40000.0)])  # aligned, but no June data
    reconciled, warnings = reconcile_bills(bills, es)
    assert reconciled.empty
    assert len(warnings) == 1
    assert "2026-06-01" in warnings[0]


def test_one_good_and_one_bad_entry_reconciles_only_the_good_one():
    es = compute_energy_summary(_edf([("2026-05-14 12:00", 1200.0)]))
    bills = _bills([
        (date(2026, 5, 1), 300.0, 40000.0),   # aligned, matches May
        (date(2026, 6, 15), 300.0, 40000.0),  # not a calendar-month boundary
    ])
    reconciled, warnings = reconcile_bills(bills, es)
    assert len(reconciled) == 1
    assert reconciled.iloc[0]["period"] == "2026-05"
    assert len(warnings) == 1


def test_duplicate_bill_for_same_period_reconciles_both_with_a_warning():
    """Two bills for the same billing period both still reconcile — dropping
    either one would silently discard real billed data the user entered —
    but a warning names the duplicated period so it doesn't slip by
    unnoticed (polish item A4a)."""
    es = compute_energy_summary(_edf([("2026-05-14 12:00", 1200.0)]))
    bills = _bills([
        (date(2026, 5, 1), 300.0, 40000.0),
        (date(2026, 5, 1), 310.0, 41000.0),
    ])
    reconciled, warnings = reconcile_bills(bills, es)
    assert len(reconciled) == 2
    assert list(reconciled["period"]) == ["2026-05", "2026-05"]
    assert len(warnings) == 1
    assert "2026-05" in warnings[0]


# ---------------------------------------------------------------- end-to-end (analyze_ups.main)
def _write_energy_agent(agent_dir, days, start=datetime(2026, 5, 1), power_w=1000.0):
    """A DataLog + energylog pair spanning `days` full days from `start`, all
    within one calendar month, mirroring tests/test_forecast.py's helper."""
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


def _hermetic_config(tmp_path, bills_file=None):
    lines = ["[archive]", "enabled = false"]
    if bills_file is not None:
        lines += ["", "[paths]", f"bills_file = '{bills_file}'"]
    conf = tmp_path / "config.toml"
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return conf


def _write_reconciling_bill(tmp_path, ups_kwh):
    """A bills.csv with exactly one aligned May-2026 entry whose whole-house
    kwh is set so the UPS-metered share comes out to an exact, easy-to-match
    24% at an implied rate of CRC 130.00/kWh."""
    billed_kwh = ups_kwh / 0.24
    amount_crc = billed_kwh * 130.0
    p = tmp_path / "bills.csv"
    p.write_text(
        "period_start,kwh,amount_crc\n"
        f"2026-05-01,{billed_kwh:.6f},{amount_crc:.6f}\n",
        encoding="utf-8",
    )
    return p, billed_kwh, amount_crc


def test_console_shows_bill_reconciliation_section(tmp_path, capsys):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=31)
    # ups_kwh here must match what the pipeline itself computes; run once
    # with a placeholder bill to read the real UPS kWh back out of --json,
    # then re-run with the bill scaled to it for an exact 24% share. Pin the
    # probe pass's bills_file to a guaranteed-absent path — config.BILLS_FILE
    # is module-level state that load_config() only ever overrides (never
    # resets), so an earlier test in this process may have already pointed
    # it at a real bills.csv, which would leak into this probe run.
    probe_missing = tmp_path / "no-such-bills-probe.csv"
    probe_json = tmp_path / "probe.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "probe.html"),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, bills_file=str(probe_missing))),
                      "--json", str(probe_json)])
    import json as _json
    ups_kwh = _json.loads(probe_json.read_text())["energy"]["total_kwh"]

    bills_path, billed_kwh, amount_crc = _write_reconciling_bill(tmp_path, ups_kwh)
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, bills_file=str(bills_path)))])
    out = capsys.readouterr().out
    assert "BILL RECONCILIATION" in out
    assert "UPS-metered share of the billed consumption" in out
    assert "24.0%" in out


def test_console_omits_bill_reconciliation_when_file_missing(tmp_path, capsys):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=31)
    # Point bills_file at a path that is guaranteed not to exist, rather than
    # relying on the untouched module default: config.BILLS_FILE is
    # module-level state that load_config() only ever overrides (never
    # resets), so an earlier test in this process may have already pointed
    # it elsewhere.
    missing = tmp_path / "no-such-bills.csv"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, bills_file=str(missing)))])
    out = capsys.readouterr().out
    assert "BILL RECONCILIATION" not in out


def test_zero_kwh_bill_produces_no_nan_anywhere(tmp_path, capsys):
    """A degenerate kwh=0 row must never reach the share/rate arithmetic:
    load_bills reports and skips it, so nothing reconciles and no "nan"
    leaks into either the console or the --json summary."""
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=31)
    bills_path = tmp_path / "bills.csv"
    bills_path.write_text(
        "period_start,kwh,amount_crc\n2026-05-01,0,45123.00\n", encoding="utf-8")
    j = tmp_path / "out.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, bills_file=str(bills_path))),
                      "--json", str(j)])
    out = capsys.readouterr().out
    assert "nan" not in out.lower()
    assert "BILL RECONCILIATION" not in out
    assert "row skipped" in out
    import json as _json
    data = _json.loads(j.read_text())
    assert "bills" not in data
    assert "nan" not in j.read_text().lower()


def test_json_summary_has_bills_key_when_reconciled(tmp_path):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=31)
    # Pin the probe pass's bills_file to a guaranteed-absent path — see the
    # comment in test_console_shows_bill_reconciliation_section.
    probe_missing = tmp_path / "no-such-bills-probe.csv"
    probe_json = tmp_path / "probe.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "probe.html"),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, bills_file=str(probe_missing))),
                      "--json", str(probe_json)])
    import json as _json
    ups_kwh = _json.loads(probe_json.read_text())["energy"]["total_kwh"]
    bills_path, billed_kwh, amount_crc = _write_reconciling_bill(tmp_path, ups_kwh)

    j = tmp_path / "out.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, bills_file=str(bills_path))),
                      "--json", str(j)])
    data = _json.loads(j.read_text())
    assert "bills" in data
    assert len(data["bills"]) == 1
    entry = data["bills"][0]
    assert entry["period"] == "2026-05"
    assert entry["share_pct"] == pytest.approx(24.0, rel=1e-6)
    assert entry["billed_kwh"] == pytest.approx(billed_kwh)
    assert entry["implied_rate_crc_per_kwh"] == pytest.approx(130.0)


def test_json_summary_omits_bills_key_when_file_missing(tmp_path):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=31)
    missing = tmp_path / "no-such-bills.csv"
    j = tmp_path / "out.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "d.html"),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, bills_file=str(missing))),
                      "--json", str(j)])
    import json as _json
    data = _json.loads(j.read_text())
    assert "bills" not in data


def test_dashboard_shows_bill_reconciliation_table(tmp_path):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=31)
    # Pin the probe pass's bills_file to a guaranteed-absent path — see the
    # comment in test_console_shows_bill_reconciliation_section.
    probe_missing = tmp_path / "no-such-bills-probe.csv"
    probe_json = tmp_path / "probe.json"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(tmp_path / "probe.html"),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, bills_file=str(probe_missing))),
                      "--json", str(probe_json)])
    import json as _json
    ups_kwh = _json.loads(probe_json.read_text())["energy"]["total_kwh"]
    bills_path, _billed_kwh, _amount_crc = _write_reconciling_bill(tmp_path, ups_kwh)

    out = tmp_path / "d.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, bills_file=str(bills_path)))])
    html = out.read_text(encoding="utf-8")
    assert "Bill Reconciliation" in html
    assert "UPS-metered share of the billed consumption" in html


def test_dashboard_omits_bill_reconciliation_table_when_file_missing(tmp_path):
    import analyze_ups
    agent = _write_energy_agent(tmp_path / "agent", days=31)
    missing = tmp_path / "no-such-bills.csv"
    out = tmp_path / "d.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_hermetic_config(tmp_path, bills_file=str(missing)))])
    html = out.read_text(encoding="utf-8")
    assert "Bill Reconciliation" not in html


def test_hermetic_e2e_build_pins_bills_file_against_repo_root_leak(
        tmp_path, tmp_path_factory, monkeypatch):
    """The hermetic E2E fixture (tests/conftest.py) must pin [paths]
    bills_file at a guaranteed-absent path, exactly as it already does for
    annotations_file. Otherwise a real bills.csv at the repo-root default
    (config.BILLS_FILE) reconciles against the synthetic billing periods and
    silently changes the supposedly hermetic page.

    This drives the actual fixture build. It is RED before the fixture fix
    (with no bills_file override the repo-root bill leaks the Bill
    Reconciliation table into the built page) and GREEN after (the absent-path
    override keeps the page clean). The bill aligns to the synthetic July
    billing period, whose partial energy still reconciles.
    """
    import conftest
    bill = tmp_path / "bills.csv"
    bill.write_text(
        "period_start,kwh,amount_crc\n2026-07-01,100.0,13000.0\n", encoding="utf-8")
    monkeypatch.setattr(config, "BILLS_FILE", bill)
    # _build_dashboard runs the real pipeline, which mutates module-level
    # config state via load_config (theme "dark", archive off, ...). Snapshot
    # and restore it so this test does not leak "dark" into a later test that
    # asserts the "auto" default.
    saved = {n: getattr(config, n) for n in ("DASHBOARD_THEME", "DASHBOARD_LANGUAGE",
                                             "ARCHIVE_ENABLED")}
    try:
        out = conftest._build_dashboard(tmp_path_factory, "dark")
        assert "Bill Reconciliation" not in out.read_text(encoding="utf-8")
    finally:
        for n, v in saved.items():
            setattr(config, n, v)
