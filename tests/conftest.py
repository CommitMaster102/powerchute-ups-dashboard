"""Pytest fixtures for the E2E suites.

Hermetic: a session fixture synthesizes a small DataLog + energylog in a temp
dir, runs the real analyzer pipeline to a temp dashboard.html, and serves that
to one shared headless Chromium — so the browser tests no longer depend on real
PCSS logs being present. Set STATEOFUPS_E2E_REAL=1 to test the committed
output/dashboard.html instead.

Fixtures:
  anim_data       — the page's ANIM_DATA contract (session)
  runner          — a fresh TestRunner over the shared page (function)
  fresh_runner    — reloads the page first (pristine state), then a TestRunner
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))          # harness
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

from harness import (  # noqa: E402
    DASHBOARD,
    TestRunner,
    assert_expected_speeds,
    stash_full_lengths,
    wait_ready,
)
from playwright.sync_api import sync_playwright  # noqa: E402

EPOCH_2010 = datetime(2010, 1, 1)


def _write_synthetic_agent(agent: Path) -> Path:
    """Create a minimal but realistic DataLog + energylog so the generated
    dashboard exercises all 8 animation groups (lv/bv/ul/bc/pw/hm/kw/rt)."""
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "energylog").mkdir(exist_ok=True)

    # DataLog: TSV, Spanish-locale decimals, 20-min cadence over ~2 days.
    start = datetime(2026, 5, 1, 0, 0, 0)
    dl_lines = ["Date and Time\tLine Voltage\tBattery Voltage\tUPS Load\tBattery Capacity"]
    for i in range(144):  # 2 days @ 20 min
        t = start + timedelta(minutes=20 * i)
        lv = 120.0 + (i % 5) - 2          # 118..122
        bv = 27.0 + (i % 3) * 0.2
        ul = 15.0 + (i % 7)
        bc = 100.0 if i % 11 else 96.0
        vals = f"{lv:.1f}\t{bv:.1f}\t{ul:.1f}\t{bc:.1f}".replace(".", ",")  # Spanish decimals
        dl_lines.append(f"{t:%m/%d/%Y %H:%M:%S}\t{vals}")
    (agent / "DataLog").write_text("\n".join(dl_lines) + "\n", encoding="utf-8")

    # energylog: ';'-delimited, dot-decimal, ts = seconds since 2010 LOCAL,
    # 5-min cadence over ~2 days (2 distinct dates -> heatmap has >=2 rows).
    el = ["# $month=2026-05", "# $interval=300", "# $calculatedMaxLoad=1400.0"]
    for i in range(576):  # 2 days @ 5 min
        t = start + timedelta(minutes=5 * i)
        secs = (t - EPOCH_2010).total_seconds()
        load = 15.0 + (i % 9)
        power = load / 100.0 * 1400.0
        el.append(f"{secs:.0f};null;{load:.1f};{power:.1f}")
    (agent / "energylog" / "2026-05.log").write_text("\n".join(el) + "\n", encoding="utf-8")
    return agent


@pytest.fixture(scope="session")
def dashboard_path(tmp_path_factory) -> Path:
    if os.environ.get("STATEOFUPS_E2E_REAL") == "1":
        if not DASHBOARD.exists():
            pytest.skip(f"real dashboard missing at {DASHBOARD}; run analyze_ups.py")
        return DASHBOARD
    agent = _write_synthetic_agent(tmp_path_factory.mktemp("agent"))
    out = tmp_path_factory.mktemp("out") / "dashboard.html"
    import analyze_ups
    analyze_ups.main(["--agent-dir", str(agent), "-o", str(out),
                      "--no-browser", "--quiet", "--no-snapshot"])
    assert out.exists(), "synthetic dashboard was not written"
    return out


@pytest.fixture(scope="session")
def _browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="session")
def _page(_browser, dashboard_path):
    page = _browser.new_page(viewport={"width": 1920, "height": 2800})
    page.goto(dashboard_path.resolve().as_uri())
    wait_ready(page)
    return page


@pytest.fixture(scope="session")
def anim_data(_page) -> dict:
    ad = TestRunner(_page).get_anim_data()
    assert ad, "could not extract ANIM_DATA from the page"
    assert_expected_speeds(ad)
    stash_full_lengths(_page, ad)
    return ad


@pytest.fixture
def runner(_page, anim_data) -> TestRunner:
    """Fresh TestRunner over the shared page (no reload). Use for tests that
    play-from-current-state and don't require pristine data."""
    return TestRunner(_page)


@pytest.fixture
def fresh_runner(_page, anim_data) -> TestRunner:
    """Reload the page to a pristine (full-data, nothing playing) state first."""
    _page.reload()
    wait_ready(_page)
    stash_full_lengths(_page, anim_data)
    return TestRunner(_page)
