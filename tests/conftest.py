"""Pytest fixtures for the E2E suites.

Hermetic: a session fixture synthesizes a small DataLog + energylog in a temp
dir (including a sampling gap and two voltage anomalies so the gap-shading,
marker, and health-pill paths are exercised), runs the real analyzer pipeline
to a temp dashboard.html, and serves that to one shared headless Chromium — so
the browser tests do not depend on real PCSS logs. Set STATEOFUPS_E2E_REAL=1
to test the generated output/dashboard.html instead (output/ is gitignored, so
it must exist locally from a prior analyze_ups.py run).

Fixtures:
  dashboard_path       — the dark-theme dashboard HTML (session)
  light_dashboard_path — a light-theme build for the theme suite (session)
  dash                 — the shared page, reset in place before every test
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))          # harness
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from pcss.common import EPOCH_2010  # noqa: E402  (single source of truth)

# Playwright + the browser harness are only needed for the E2E suites. Import
# them inside a guard so the unit suite collects and runs without Playwright
# installed — CI's lint-unit job installs only `.[lint,test]` (no ~36 MiB
# Playwright download). When it's absent, skip collecting the e2e_*.py files
# entirely instead of erroring at import time.
try:
    from harness import (  # noqa: E402
        DASHBOARD,  # noqa: E402
        PANELS,
        wait_ready,
    )
    from playwright.sync_api import sync_playwright  # noqa: E402
except ImportError:
    collect_ignore_glob = ["e2e_*.py"]


def _write_synthetic_agent(agent: Path) -> Path:
    """Create a minimal but realistic DataLog + energylog so the generated
    dashboard exercises every panel: 2 days at 20-min cadence with one 2-hour
    gap and two out-of-envelope voltage samples mid-series (the last sample
    stays healthy so the KPI severities are deterministic)."""
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "energylog").mkdir(exist_ok=True)

    # DataLog: TSV, Spanish-locale decimals, 20-min cadence over ~2 days.
    start = datetime(2026, 5, 1, 0, 0, 0)
    dl_lines = ["Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity"]
    for i in range(144):  # 2 days @ 20 min
        if 60 <= i < 66:
            continue                      # one 2-hour sampling gap
        t = start + timedelta(minutes=20 * i)
        lv = 120.0 + (i % 5) - 2          # 118..122
        if i == 30:
            lv = 130.5                    # above the 126 V envelope
        if i == 100:
            lv = 108.0                    # below the 114 V envelope
        bv = 27.0 + (i % 3) * 0.2
        ul = 15.0 + (i % 7)
        bc = 100.0 if i % 11 else 96.0
        if i in (120, 121):
            # One corroborated on-battery episode: line voltage collapses
            # while the battery capacity falls (exercises the ep-strip path).
            lv = 0.5
            bc = 90.0 if i == 120 else 84.0
        vals = f"{lv:.1f}\t{bv:.1f}\t{ul:.1f}\t{bc:.1f}".replace(".", ",")  # Spanish decimals
        dl_lines.append(f"{t:%m/%d/%Y %H:%M:%S}\t{vals}")
    (agent / "DataLog").write_text("\n".join(dl_lines) + "\n", encoding="utf-8")

    # energylog: ';'-delimited, dot-decimal, ts = seconds since 2010 LOCAL,
    # 5-min cadence over ~2 days (2 distinct dates -> heatmap has >=2 rows).
    # A second file covers the tail of April so the period-comparison panel
    # has two billing periods; May 1 2026 is a Friday and May 2 a Saturday,
    # so the weekday/weekend panel gets both profiles.
    for month_start, month_label, n in [
        (start - timedelta(days=2), "2026-04", 576),   # Apr 29-30
        (start, "2026-05", 576),                       # May 1-2
    ]:
        el = [f"# $month={month_label}", "# $interval=300", "# $calculatedMaxLoad=1400.0"]
        for i in range(n):  # @ 5 min
            t = month_start + timedelta(minutes=5 * i)
            secs = (t - EPOCH_2010).total_seconds()
            load = 15.0 + (i % 9)
            power = load / 100.0 * 1400.0
            el.append(f"{secs:.0f};null;{load:.1f};{power:.1f}")
        (agent / "energylog" / f"{month_label}.log").write_text(
            "\n".join(el) + "\n", encoding="utf-8")
    return agent


def _build_dashboard(tmp_path_factory, theme: str) -> Path:
    """Run the real pipeline against the synthetic agent with an explicit
    config file (hermetic: a developer's local config.toml must not leak into
    the tests, and neither may a developer's real output/archive — hence
    archive disabled) and return the written HTML path."""
    agent = _write_synthetic_agent(tmp_path_factory.mktemp(f"agent-{theme}"))
    out_dir = tmp_path_factory.mktemp(f"out-{theme}")
    out = out_dir / "dashboard.html"
    conf = out_dir / "config.toml"
    conf.write_text(f'[dashboard]\ntheme = "{theme}"\n\n[archive]\nenabled = false\n',
                    encoding="utf-8")
    import analyze_ups
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out), "--config", str(conf),
                      "--no-browser", "--quiet", "--no-snapshot"])
    assert out.exists(), "synthetic dashboard was not written"
    return out


@pytest.fixture(scope="session")
def dashboard_path(tmp_path_factory) -> Path:
    if os.environ.get("STATEOFUPS_E2E_REAL") == "1":
        if not DASHBOARD.exists():
            pytest.skip(f"real dashboard missing at {DASHBOARD}; run analyze_ups.py")
        return DASHBOARD
    return _build_dashboard(tmp_path_factory, "dark")


@pytest.fixture(scope="session")
def light_dashboard_path(tmp_path_factory) -> Path:
    return _build_dashboard(tmp_path_factory, "light")


@pytest.fixture(scope="session")
def _browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="session")
def _page(_browser, dashboard_path):
    page = _browser.new_page(viewport={"width": 1600, "height": 2400})
    page.goto(dashboard_path.resolve().as_uri())
    wait_ready(page)
    # Generator-vs-test alignment guard: the page must have rendered exactly
    # the panels the harness parametrizes over.
    keys = sorted(page.evaluate("__chartsDebug.panelKeys()"))
    assert keys == sorted(PANELS), f"page panels {keys} != harness PANELS {sorted(PANELS)}"
    return page


@pytest.fixture
def dash(_page):
    """The shared page, reset to its pristine state (full window, nothing
    pinned or hidden, lightbox closed) so the tests are order-independent —
    they can run in any sequence and across xdist workers (which share one
    page per worker) without a prior test's leftover zoom contaminating this
    one. Reset is done in-page via ``__chartsDebug.resetAll()`` rather than a
    full ``page.reload()``: it is far cheaper, which is what keeps the
    parallel suite fast on low-core CI runners."""
    _page.evaluate("window.__chartsDebug.resetAll()")
    _page.mouse.move(0, 0)
    _page.wait_for_timeout(50)
    return _page
