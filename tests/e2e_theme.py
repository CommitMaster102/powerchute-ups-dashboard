"""E2E: auto theme (roadmap item 30).

One "auto" build ships both palettes and follows the viewer's
prefers-color-scheme (emulated here via Playwright's emulate_media). The header
toggle overrides that live — cycling auto, dark, light — and the override rides
the permalink hash, restores on reload, and clears back to the config default
on resetAll. Chart ink is resolved from the active palette at draw time, so a
switch redraws the SVG rather than needing a second server build (the old
light_dashboard_path fixture is gone). The PNG-reflects-the-active-palette
assertion lives in tests/e2e_export.py.
"""
from __future__ import annotations

import pytest
from harness import wait_ready

pytestmark = pytest.mark.e2e

DARK_BG = "#101216"
LIGHT_BG = "#f4f6f9"
DARK_BLUE = "#5aa9f0"
LIGHT_BLUE = "#2b7cd3"


def _bg(page):
    return page.evaluate(
        "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()")


def _lv_stroke(page):
    """The Line Voltage series stroke — the "blue" role resolved to the active
    palette's concrete hex (the series path is the one #-prefixed stroke)."""
    return page.evaluate(
        "document.querySelector(\"#panel-lv svg path[stroke^='#']\")"
        ".getAttribute('stroke').toLowerCase()")


@pytest.fixture
def auto_page(_browser, auto_dashboard_path):
    page = _browser.new_page(viewport={"width": 1600, "height": 2400})
    try:
        yield page
    finally:
        page.close()


def _open(page, path, scheme):
    page.emulate_media(color_scheme=scheme)
    page.goto(path.resolve().as_uri())
    wait_ready(page)


def test_auto_resolves_dark_under_prefers_dark(auto_page, auto_dashboard_path):
    _open(auto_page, auto_dashboard_path, "dark")
    assert _bg(auto_page) == DARK_BG
    assert auto_page.evaluate("__chartsDebug.theme()") == "dark"
    assert _lv_stroke(auto_page) == DARK_BLUE
    # The chrome carried both palettes: charts.js resolved the dark one.
    assert auto_page.evaluate("document.querySelectorAll('.chart-box svg').length") >= 12


def test_auto_resolves_light_under_prefers_light(auto_page, auto_dashboard_path):
    _open(auto_page, auto_dashboard_path, "light")
    assert _bg(auto_page) == LIGHT_BG
    assert auto_page.evaluate("__chartsDebug.theme()") == "light"
    assert _lv_stroke(auto_page) == LIGHT_BLUE


def test_auto_reacts_to_live_scheme_change(auto_page, auto_dashboard_path):
    _open(auto_page, auto_dashboard_path, "dark")
    assert auto_page.evaluate("__chartsDebug.theme()") == "dark"
    # Flip the OS preference while in auto mode: the chrome follows via CSS, and
    # charts.js redraws the chart ink to the light palette.
    auto_page.emulate_media(color_scheme="light")
    auto_page.wait_for_function(
        "document.querySelector(\"#panel-lv svg path[stroke^='#']\")"
        f".getAttribute('stroke').toLowerCase() === '{LIGHT_BLUE}'")
    assert _bg(auto_page) == LIGHT_BG


def test_toggle_cycles_auto_dark_light_and_redraws(auto_page, auto_dashboard_path):
    _open(auto_page, auto_dashboard_path, "dark")   # auto resolves to dark
    assert auto_page.evaluate("__chartsDebug.themeMode()") == "auto"
    assert auto_page.evaluate("__chartsDebug.theme()") == "dark"
    btn = auto_page.locator("#theme-btn")

    btn.click()   # auto -> dark (explicit)
    assert auto_page.evaluate("__chartsDebug.themeMode()") == "dark"
    assert _bg(auto_page) == DARK_BG

    btn.click()   # dark -> light: chrome and chart ink both switch
    assert auto_page.evaluate("__chartsDebug.themeMode()") == "light"
    assert _bg(auto_page) == LIGHT_BG
    assert _lv_stroke(auto_page) == LIGHT_BLUE

    btn.click()   # light -> auto: back to following prefers-color-scheme (dark)
    assert auto_page.evaluate("__chartsDebug.themeMode()") == "auto"
    assert auto_page.evaluate("__chartsDebug.theme()") == "dark"


def test_toggle_button_shows_current_state(auto_page, auto_dashboard_path):
    _open(auto_page, auto_dashboard_path, "dark")
    label0 = auto_page.locator("#theme-btn").text_content()
    assert "auto" in label0.lower()
    auto_page.locator("#theme-btn").click()
    assert auto_page.locator("#theme-btn").text_content() != label0


def test_theme_override_rides_permalink_hash(auto_page, auto_dashboard_path):
    _open(auto_page, auto_dashboard_path, "dark")
    # The config default (auto) leaves no theme key in the hash.
    assert "t=" not in auto_page.evaluate("location.hash")
    auto_page.locator("#theme-btn").click()   # -> dark
    auto_page.locator("#theme-btn").click()   # -> light
    assert "t=light" in auto_page.evaluate("location.hash")
    # A reload restores the override silently.
    auto_page.reload()
    wait_ready(auto_page)
    assert auto_page.evaluate("__chartsDebug.themeMode()") == "light"
    assert _bg(auto_page) == LIGHT_BG


def test_reset_all_clears_theme_to_config_default(auto_page, auto_dashboard_path):
    _open(auto_page, auto_dashboard_path, "dark")
    auto_page.locator("#theme-btn").click()   # -> dark
    auto_page.locator("#theme-btn").click()   # -> light
    assert auto_page.evaluate("__chartsDebug.themeMode()") == "light"
    auto_page.evaluate("__chartsDebug.resetAll()")
    assert auto_page.evaluate("__chartsDebug.themeMode()") == "auto"
    assert "t=" not in auto_page.evaluate("location.hash")
