"""Tests for the [dashboard] max_days payload-budget window — the cheap
alternative to server-side decimation. max_days = 0, the
default, must leave the dashboard byte-identical. A positive value narrows
only the raw per-sample frames fed to build_dashboard (DataLog, energylog,
the size-history growth series, and the gap/anomaly/episode overlays that
ride the same time panels), anchored to the newest DataLog sample rather
than the wall clock, and the footer names the window once something is
actually trimmed. Everything computed before that cut — the console
summary, --json, alerts, the archive append, and every fitted stats surface
(battery replace-by, forecast, cross-validation, ...) — must stay on the
full history regardless of max_days; the end-to-end tests below prove that
by comparing --json output at two different max_days settings against the
identical input data.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

import pcss.config as cfg
import pcss.dashboard as dashboard_module
from analyze_ups import _dashboard_window, _window_df
from pcss.dashboard import build_dashboard
from pcss.stats import compute_energy_summary


class _FrozenDatetime(datetime):
    """A fixed `datetime.now()` so two build_dashboard() calls in the same
    test produce the same "Generated ..." footer text, regardless of how
    much wall-clock time elapses between the calls (fromtimestamp() stays
    the real implementation, since the aria-label summaries use it)."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 5, 1, 12, 0, 0)


EMPTY = pd.DataFrame()


# ---------------------------------------------------------------- helpers
def _datalog(n=60, start="2026-05-01 00:00"):
    ts = pd.date_range(start, periods=n, freq="20min")
    return pd.DataFrame({
        "ts": ts,
        "Line Voltage": np.full(n, 120.0),
        "Battery Voltage": np.full(n, 27.4),
        "UPS Load": np.full(n, 15.0),
        "Battery Capacity": np.full(n, 100.0),
    })


def _energy(n=60, start="2026-05-01 00:00"):
    ts = pd.date_range(start, periods=n, freq="5min")
    return pd.DataFrame({"ts": ts, "power_w": np.full(n, 250.0), "interval_sec": 300})


def _smoke_inputs(n_days=10):
    """The same shape as test_chart_payload.py's _smoke_inputs, kept local
    (per this repo's convention of self-contained test files) and sized by
    day count so the footer-note tests can build a history longer than the
    window they trim it to."""
    n = n_days * 72   # 20-min cadence
    datalog = _datalog(n)
    energy = _energy(n_days * 288)
    hist = pd.DataFrame({
        "timestamp": pd.date_range("2026-05-01", periods=4, freq="1D"),
        "datalog_bytes": [1000, 2000, 3000, 4000],
        "eventlog_bytes": [100, 100, 100, 100],
        "energylog_bytes": [500, 900, 1300, 1700],
        "total_bytes": [1600, 3000, 4400, 5800],
    })
    stats_table = pd.DataFrame([{"Metric": "Line Voltage", "Min": "118.00", "Mean": "120.00",
                                 "Median": "120.00", "p95": "122.00", "Max": "122.00",
                                 "Samples": n}])
    dl_stats = {"daily_bytes": 5000.0, "span_days": float(n_days), "median_interval_sec": 1200.0}
    energy_summary = compute_energy_summary(energy)
    return dict(datalog_df=datalog, energy_df=energy, hist=hist, dl_stats=dl_stats,
                hist_stats={"bytes_per_hour": 100.0, "bytes_per_day": 2400.0, "snapshots": 4},
                sizes={"DataLog": 4000, "EventLog (binary)": 100, "energylog/": 1700},
                energy_summary=energy_summary, stats_table=stats_table,
                gaps=pd.DataFrame(), voltage_anomalies=pd.DataFrame(),
                high_load_episodes=pd.DataFrame(), crossval={})


def _payload(html: str) -> dict:
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    assert m, "embedded payload not found"
    return json.loads(m.group(1).replace("<\\/", "</"))


# ---------------------------------------------------------------- _dashboard_window
def test_dashboard_window_disabled_by_default():
    """max_days = 0 must never trim anything, however long the history —
    this is what keeps the dashboard byte-identical at the default."""
    df = _datalog(120)
    assert _dashboard_window(df, 0.0) is None


def test_dashboard_window_disabled_on_negative_max_days():
    df = _datalog(120)
    assert _dashboard_window(df, -5.0) is None


def test_dashboard_window_disabled_on_empty_frame():
    assert _dashboard_window(EMPTY, 30.0) is None


def test_dashboard_window_trims_when_span_exceeds_max_days():
    df = _datalog(90 * 72)          # 90 days at 20-min cadence
    cutoff = _dashboard_window(df, 30.0)
    assert cutoff is not None
    anchor = df["ts"].iloc[-1]
    assert cutoff == anchor - pd.Timedelta(days=30)
    assert (df["ts"] < cutoff).any()   # something actually falls before it


def test_dashboard_window_noop_when_max_days_exceeds_span():
    """A max_days larger than the recorded span must trim nothing."""
    df = _datalog(10 * 72)           # 10 days
    assert _dashboard_window(df, 30.0) is None


# ---------------------------------------------------------------- _window_df
def test_window_df_filters_rows_at_or_after_cutoff():
    df = _datalog(30 * 72)
    cutoff = df["ts"].iloc[-1] - pd.Timedelta(days=10)
    windowed = _window_df(df, "ts", cutoff)
    assert len(windowed) < len(df)
    assert (windowed["ts"] >= cutoff).all()


def test_window_df_passes_through_empty_or_missing_column():
    assert _window_df(EMPTY, "ts", pd.Timestamp("2026-01-01")) is EMPTY
    assert _window_df(None, "ts", pd.Timestamp("2026-01-01")) is None
    no_col = pd.DataFrame({"x": [1, 2]})
    assert _window_df(no_col, "ts", pd.Timestamp("2026-01-01")) is no_col


# ---------------------------------------------------------------- build_dashboard footer note
def test_footer_no_note_by_default():
    html = build_dashboard(**_smoke_inputs())
    assert "output/archive" not in html


def test_footer_no_note_when_explicitly_none():
    html = build_dashboard(**_smoke_inputs(), dashboard_window_days=None)
    assert "output/archive" not in html


def test_footer_note_appears_when_trimmed():
    html = build_dashboard(**_smoke_inputs(), dashboard_window_days=30)
    assert "last 30 days" in html
    assert "output/archive/" in html


def test_footer_note_localized_es(monkeypatch):
    monkeypatch.setattr(cfg, "DASHBOARD_LANGUAGE", "es")
    html = build_dashboard(**_smoke_inputs(), dashboard_window_days=30)
    assert "últimos 30 días" in html
    assert "output/archive/" in html
    assert "Showing the last" not in html


def test_max_days_zero_dashboard_byte_identical(monkeypatch):
    """Omitting dashboard_window_days and passing it explicitly as None
    (the parameter's own no-op case) must produce byte-identical output."""
    monkeypatch.setattr(dashboard_module, "datetime", _FrozenDatetime)
    inputs = _smoke_inputs()
    a = build_dashboard(**inputs)
    b = build_dashboard(**inputs, dashboard_window_days=None)
    assert a == b


# ---------------------------------------------------------------- end to end (analyze_ups.main())
@pytest.fixture
def restore_config_analyze():
    """Snapshot the config globals these tests' --config files touch, and
    restore them after — config is module-level state (see pcss/config.py's
    load_config docstring), so a leftover value from one test could leak
    into the next one in the same worker process."""
    names = ["PCSS_AGENT", "DATALOG", "EVENTLOG", "ENERGYLOG_DIR", "DASHBOARD_HTML",
             "ARCHIVE_ENABLED", "DASHBOARD_LANGUAGE", "DASHBOARD_MAX_DAYS"]
    saved = {n: getattr(cfg, n) for n in names}
    yield
    for n, v in saved.items():
        setattr(cfg, n, v)


def _write_multiday_agent(agent, days=30, start=None):
    """A synthetic PCSS agent directory spanning `days` days: DataLog at the
    factory 20-minute cadence, energylog at 5 minutes. Values are constant
    (this suite only exercises the windowing cut, not anomaly detection).
    `start` defaults to 2026-05-01 (the original fixture date); a caller that
    needs the window to cross a calendar-month boundary (for example, to
    exercise the cmp panel's two-billing-period overlay) can pass its own."""
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "energylog").mkdir(exist_ok=True)
    start = start or datetime(2026, 5, 1)
    n = days * 72
    dl = ["Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity"]
    for i in range(n):
        t = start + pd.Timedelta(minutes=20 * i)
        dl.append(f"{t:%m/%d/%Y %H:%M:%S}\t120,0\t27,4\t15,0\t100")
    (agent / "DataLog").write_text("\n".join(dl) + "\n", encoding="utf-8")
    el = ["# $month=2026-05", "# $interval=300", "# $calculatedMaxLoad=1400.0"]
    for i in range(days * 288):
        secs = (start + pd.Timedelta(minutes=5 * i) - datetime(2010, 1, 1)).total_seconds()
        el.append(f"{secs:.0f};null;15.0;210.0")
    (agent / "energylog" / "2026-05.log").write_text("\n".join(el) + "\n", encoding="utf-8")
    return agent, n


def _config(tmp_path, max_days=0):
    """A hermetic config (archive disabled, so a developer's real
    output/archive never merges in) with an explicit [dashboard] max_days,
    so every test in this file states its own window rather than relying on
    whatever the module-level default happens to be at that moment."""
    conf = tmp_path / "config.toml"
    conf.write_text(
        f"[archive]\nenabled = false\n\n[dashboard]\nmax_days = {max_days}\n",
        encoding="utf-8",
    )
    return conf


def _lv_series_x(html: str) -> list[int]:
    return _payload(html)["panels"]["lv"]["series"][0]["x"]


def test_analyze_max_days_zero_is_full_history(tmp_path, restore_config_analyze):
    import analyze_ups
    agent, n = _write_multiday_agent(tmp_path / "agent", days=30)
    out = tmp_path / "dash.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out), "--no-browser",
                      "--quiet", "--no-snapshot", "--config", str(_config(tmp_path, 0))])
    html = out.read_text(encoding="utf-8")
    assert len(_lv_series_x(html)) == n
    assert "output/archive" not in html


def test_analyze_max_days_trims_dashboard_series_and_shows_note(tmp_path, restore_config_analyze):
    import analyze_ups
    agent, n = _write_multiday_agent(tmp_path / "agent", days=30)
    out = tmp_path / "dash.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out), "--no-browser",
                      "--quiet", "--no-snapshot", "--config", str(_config(tmp_path, 10))])
    html = out.read_text(encoding="utf-8")
    xs = _lv_series_x(html)
    assert 0 < len(xs) < n
    assert xs[-1] - xs[0] <= 10 * 86_400_000
    assert "last 10 days" in html
    assert "output/archive/" in html


def test_analyze_max_days_larger_than_span_is_noop(tmp_path, restore_config_analyze):
    import analyze_ups
    agent, n = _write_multiday_agent(tmp_path / "agent", days=10)
    out = tmp_path / "dash.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out), "--no-browser",
                      "--quiet", "--no-snapshot", "--config", str(_config(tmp_path, 9999))])
    html = out.read_text(encoding="utf-8")
    assert len(_lv_series_x(html)) == n
    assert "output/archive" not in html


def test_analyze_max_days_leaves_stats_surfaces_full_history(tmp_path, restore_config_analyze):
    """The console/--json/alerts/archive-append/stats-surface computations
    all run before the dashboard window is applied, so --json must be
    identical regardless of max_days — the analyze-vs-visualize split the
    brief requires. (The dashboard.html files, by contrast, must differ.)"""
    import analyze_ups
    agent, _ = _write_multiday_agent(tmp_path / "agent", days=30)
    j0 = tmp_path / "j0.json"
    j10 = tmp_path / "j10.json"
    html0_path = tmp_path / "a.html"
    html10_path = tmp_path / "b.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(html0_path),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_config(tmp_path, 0)), "--json", str(j0)])
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(html10_path),
                      "--no-browser", "--quiet", "--no-snapshot",
                      "--config", str(_config(tmp_path, 10)), "--json", str(j10)])
    assert json.loads(j0.read_text()) == json.loads(j10.read_text())
    assert html0_path.read_text(encoding="utf-8") != html10_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- review round 1: every heavy
# series must shrink, not just lv/pw (Finding 3), and a span straddling the
# cutoff must keep its visible tail (Finding 4).
def test_analyze_max_days_shrinks_kw_daily_hm_panels(tmp_path, restore_config_analyze):
    """Before this fix, energy_summary was computed from the full energylog
    before the window cut and reused as-is for every energy-derived panel,
    so kw/daily/hm shipped the complete history regardless of max_days —
    only lv/pw (built from datalog_df/energy_df directly, already windowed)
    actually shrank. A 60-day history windowed to the last 10 days must
    shrink kw's point count, daily's bar count, and hm's day-row count."""
    import analyze_ups
    agent, _ = _write_multiday_agent(tmp_path / "agent", days=60)
    out0 = tmp_path / "dash0.html"
    out10 = tmp_path / "dash10.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out0), "--no-browser",
                      "--quiet", "--no-snapshot", "--config", str(_config(tmp_path, 0))])
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out10), "--no-browser",
                      "--quiet", "--no-snapshot", "--config", str(_config(tmp_path, 10))])
    p0 = _payload(out0.read_text(encoding="utf-8"))
    p10 = _payload(out10.read_text(encoding="utf-8"))

    kw0 = len(p0["panels"]["kw"]["series"][0]["x"])
    kw10 = len(p10["panels"]["kw"]["series"][0]["x"])
    assert kw0 == 60 * 288
    assert 0 < kw10 < kw0
    assert kw10 < 12 * 288   # ~10 days plus a fractional boundary day, nowhere near 60 days

    daily0 = len(p0["panels"]["daily"]["data"])
    daily10 = len(p10["panels"]["daily"]["data"])
    assert daily0 == 60
    assert 0 < daily10 < daily0
    assert daily10 <= 12

    hm0 = len(p0["panels"]["hm"]["days"])
    hm10 = len(p10["panels"]["hm"]["days"])
    assert hm0 == 60
    assert 0 < hm10 < hm0
    assert hm10 <= 12


def test_windowed_energy_summary_previous_period_reads_partial():
    """Finding 1 note: analyze_ups.py recomputes energy_summary from the
    windowed energylog frame rather than reusing the full-history one, so
    the cmp panel's previous billing period comes back partial once the
    window truncates its start. compute_energy_summary's own partial-period
    detection (a period is partial when the recorded span starts after or
    ends before the period's own calendar bounds) needs no special case for
    this — windowing the input is enough, which this proves directly."""
    energy_df = _energy(n=46 * 288, start="2026-04-20 00:00")   # spans April 20 -> June 4

    full_summary = compute_energy_summary(energy_df)
    full_monthly = full_summary["monthly"].set_index("month")
    assert not bool(full_monthly.loc["2026-05", "partial"])   # May is fully recorded

    cutoff = pd.Timestamp("2026-05-21")   # inside May: truncates only the previous period
    windowed_df = energy_df[energy_df["ts"] >= cutoff].reset_index(drop=True)
    windowed_summary = compute_energy_summary(windowed_df)
    windowed_monthly = windowed_summary["monthly"].set_index("month")
    assert bool(windowed_monthly.loc["2026-05", "partial"])   # now truncated by the window


def test_analyze_cmp_previous_period_truncated_when_window_crosses_month(
    tmp_path, restore_config_analyze,
):
    """End-to-end companion to the unit test above: analyze_ups.py's own
    dash_energy_summary swap must actually reach the cmp panel. A fixture
    spanning April 20 -> June 4 windowed to the last 15 days (May 20 ->
    June 4) crosses the April/May.../May/June boundary, so the cmp panel's
    previous-period series (May) starts partway through May instead of at
    its own day-0 — while the unwindowed run starts that same series at
    day 0, because before this fix dash_energy_summary was just the
    full-history energy_summary regardless of max_days."""
    import analyze_ups
    agent, _ = _write_multiday_agent(tmp_path / "agent", days=46, start=datetime(2026, 4, 20))
    out0 = tmp_path / "dash0.html"
    out15 = tmp_path / "dash15.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out0), "--no-browser",
                      "--quiet", "--no-snapshot", "--config", str(_config(tmp_path, 0))])
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out15), "--no-browser",
                      "--quiet", "--no-snapshot", "--config", str(_config(tmp_path, 15))])
    cmp0 = _payload(out0.read_text(encoding="utf-8"))["panels"]["cmp"]
    cmp15 = _payload(out15.read_text(encoding="utf-8"))["panels"]["cmp"]
    assert cmp0 is not None and cmp15 is not None   # both runs still show two periods

    prev_x0 = cmp0["series"][0]["x"]     # the previous-period (May) series
    prev_x15 = cmp15["series"][0]["x"]
    assert min(prev_x0) < 1        # full history: May's own series starts at day 0
    assert min(prev_x15) > 5       # windowed: truncated partway through May


def test_analyze_windows_self_tests_before_dashboard(tmp_path, restore_config_analyze, monkeypatch):
    """Finding 1 note: analyze_ups.py must window self_tests the same way it
    windows every other dashboard overlay. The bc panel places self-test
    markers with merge_asof(direction="nearest") against Battery Capacity
    readings, so an unwindowed test from well before the cutoff would
    otherwise snap onto the first surviving reading and draw a marker for a
    test that never happened inside the visible window."""
    import analyze_ups
    agent, _ = _write_multiday_agent(tmp_path / "agent", days=30)

    # A fixed detect_self_tests double: one test well before the max_days=10
    # cutoff (near the start of the 30-day history), one comfortably inside it.
    def fake_detect_self_tests(datalog_df, events=None):
        ts_sorted = datalog_df["ts"].sort_values().reset_index(drop=True)
        return pd.DataFrame({"ts": [ts_sorted.iloc[5], ts_sorted.iloc[-5]]})
    monkeypatch.setattr(analyze_ups, "detect_self_tests", fake_detect_self_tests)

    out = tmp_path / "dash.html"
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out), "--no-browser",
                      "--quiet", "--no-snapshot", "--config", str(_config(tmp_path, 10))])
    html = out.read_text(encoding="utf-8")
    markers = _payload(html)["panels"]["bc"]["markers"]
    assert len(markers) == 1


def test_window_df_keeps_span_straddling_cutoff_via_end_col():
    """Finding 4 (review): gaps/high-load episodes/
    on-battery episodes were windowed on their start column alone, so an
    entry that began before the cutoff but extends into the window was
    dropped whole rather than showing its still-visible tail. Passing
    end_col keeps a straddling entry, filtering on its end instead."""
    base = pd.Timestamp("2026-06-01 00:00")
    gaps = pd.DataFrame({
        "from": [base, base + pd.Timedelta(days=5)],
        "to": [base + pd.Timedelta(hours=2), base + pd.Timedelta(days=5, hours=1)],
        "duration_min": [120.0, 60.0],
    })
    cutoff = base + pd.Timedelta(hours=1)   # inside the first gap: starts before, ends after

    by_start_only = _window_df(gaps, "from", cutoff)
    assert len(by_start_only) == 1   # the bug: the straddling gap is dropped whole

    windowed = _window_df(gaps, "from", cutoff, end_col="to")
    assert len(windowed) == 2
    assert (windowed["to"] >= cutoff).all()
