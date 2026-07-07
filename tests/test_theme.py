"""Unit tests for the auto-theme contract (roadmap item 30).

The dashboard ships both palettes and lets a single build follow
prefers-color-scheme, with a header toggle for manual override. These tests pin
the Python side of that contract: the payload carries palette-neutral color
role names (not resolved hex) plus both palettes, the config theme gains an
"auto" default that still lets "dark"/"light" pin the initial theme, and the
page chrome ships both theme blocks as CSS custom properties selected by
prefers-color-scheme. The live toggling, redraw, and PNG export are covered by
tests/e2e_theme.py; here the surface is the generated HTML and its payload.
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd
import pytest

import pcss.config as cfg
from pcss.dashboard import PALETTES, build_dashboard
from pcss.stats import compute_energy_summary


def _datalog(n=72, start="2026-05-01 00:00"):
    ts = pd.date_range(start, periods=n, freq="20min")
    return pd.DataFrame({
        "ts": ts,
        "Line Voltage": np.full(n, 120.0),
        "Battery Voltage": np.full(n, 27.4),
        "UPS Load": np.full(n, 15.0),
        "Battery Capacity": np.full(n, 100.0),
    })


def _energy(n=144, start="2026-05-01 00:00", power=250.0):
    ts = pd.date_range(start, periods=n, freq="5min")
    return pd.DataFrame({"ts": ts, "power_w": np.full(n, power), "interval_sec": 300})


def _smoke_inputs():
    hist = pd.DataFrame({
        "timestamp": pd.date_range("2026-05-01", periods=4, freq="1D"),
        "datalog_bytes": [1000, 2000, 3000, 4000],
        "eventlog_bytes": [100, 100, 100, 100],
        "energylog_bytes": [500, 900, 1300, 1700],
        "total_bytes": [1600, 3000, 4400, 5800],
    })
    stats_table = pd.DataFrame([{"Metric": "Line Voltage", "Min": "118.00", "Mean": "120.00",
                                 "Median": "120.00", "p95": "122.00", "Max": "122.00",
                                 "Samples": 72}])
    return dict(
        datalog_df=_datalog(), energy_df=_energy(), hist=hist,
        dl_stats={"daily_bytes": 5000.0, "span_days": 1.0, "median_interval_sec": 1200.0},
        hist_stats={"bytes_per_hour": 100.0, "bytes_per_day": 2400.0, "snapshots": 4},
        sizes={"DataLog": 4000, "EventLog (binary)": 100, "energylog/": 1700},
        energy_summary=compute_energy_summary(_energy()), stats_table=stats_table,
        gaps=pd.DataFrame(), voltage_anomalies=pd.DataFrame(),
        high_load_episodes=pd.DataFrame(), crossval={})


def _payload(html):
    m = re.search(r"const DATA = (\{.*?\});\n", html, re.DOTALL)
    assert m, "embedded payload not found"
    return json.loads(m.group(1).replace("<\\/", "</"))


# ---------------------------------------------------------------- payload carries roles, not hex
def test_series_colors_are_role_names_not_hex():
    payload = _payload(build_dashboard(**_smoke_inputs()))
    lv = payload["panels"]["lv"]
    assert lv["series"][0]["color"] == "blue"
    # A resolved hex would start with "#"; a palette-neutral role never does.
    assert not str(lv["series"][0]["color"]).startswith("#")


def test_marker_and_bar_colors_are_roles():
    inputs = _smoke_inputs()
    inputs["voltage_anomalies"] = inputs["datalog_df"].iloc[[3, 7]][["ts", "Line Voltage"]].copy()
    payload = _payload(build_dashboard(**inputs))
    # The runtime "now" star marker carries a role, not hex.
    assert payload["panels"]["rt"]["markers"][0]["color"] == "red"
    # The expected-cadence bar is highlighted with the teal role.
    assert any(d.get("color") == "teal" for d in payload["panels"]["cad"]["data"])


def test_spark_colors_are_roles():
    payload = _payload(build_dashboard(**_smoke_inputs()))
    colors = [s["color"] for s in payload["sparks"] if s]
    assert colors and all(not str(c).startswith("#") for c in colors)


def test_payload_ships_both_palettes_and_no_single_palette():
    payload = _payload(build_dashboard(**_smoke_inputs()))
    assert set(payload["palettes"]) == {"dark", "light"}
    assert payload["palettes"]["dark"]["blue"] == PALETTES["dark"]["blue"] == "#5aa9f0"
    assert payload["palettes"]["light"]["blue"] == PALETTES["light"]["blue"] == "#2b7cd3"
    # The single build-time-resolved palette is gone: charts.js resolves roles
    # against whichever palette is active at draw time.
    assert "palette" not in payload


def test_palettes_carry_a_heat_scale_for_the_heatmap():
    payload = _payload(build_dashboard(**_smoke_inputs()))
    for name in ("dark", "light"):
        heat = payload["palettes"][name]["heat"]
        assert isinstance(heat, list) and len(heat) >= 2
        assert all(len(stop) == 3 for stop in heat)   # rgb triples


# ---------------------------------------------------------------- config theme value
def test_payload_theme_defaults_to_auto(monkeypatch):
    monkeypatch.setattr(cfg, "DASHBOARD_THEME", "auto")
    assert _payload(build_dashboard(**_smoke_inputs()))["theme"] == "auto"


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_explicit_theme_pins_initial(monkeypatch, theme):
    monkeypatch.setattr(cfg, "DASHBOARD_THEME", theme)
    html = build_dashboard(**_smoke_inputs())
    assert _payload(html)["theme"] == theme
    assert f'data-theme="{theme}"' in html


# ---------------------------------------------------------------- chrome ships both theme blocks
def test_chrome_ships_both_theme_blocks(monkeypatch):
    monkeypatch.setattr(cfg, "DASHBOARD_THEME", "auto")
    html = build_dashboard(**_smoke_inputs())
    assert ':root[data-theme="light"]' in html
    assert "prefers-color-scheme: light" in html
    assert 'data-theme="auto"' in html
    # Both background colors must ship so a live switch has both to use.
    assert PALETTES["dark"]["bg"] in html
    assert PALETTES["light"]["bg"] in html


def test_kpi_and_health_accents_use_css_vars():
    html = build_dashboard(**_smoke_inputs())
    # The health pill drives its color through the theme-aware CSS variable, so
    # it follows the active theme without a rebuild.
    assert "--health:var(--" in html
    # KPI accents are CSS vars too.
    assert "background:var(--" in html


# ---------------------------------------------------------------- header toggle
def test_theme_toggle_button_present_and_localized(monkeypatch):
    html_en = build_dashboard(**_smoke_inputs())
    assert 'id="theme-btn"' in html_en
    payload = _payload(html_en)
    for key in ("themeLabel", "themeAuto", "themeDark", "themeLight"):
        assert payload["strings"][key], f"missing theme string {key}"
    monkeypatch.setattr(cfg, "DASHBOARD_LANGUAGE", "es")
    payload_es = _payload(build_dashboard(**_smoke_inputs()))
    # At least one mode word translates (dark -> oscuro), like the rest of the
    # page chrome.
    assert payload_es["strings"]["themeDark"] != payload["strings"]["themeDark"]
