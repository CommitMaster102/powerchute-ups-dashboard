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
from harness import hover_panel, wait_ready

pytestmark = pytest.mark.e2e

DARK_BG = "#101216"
LIGHT_BG = "#f4f6f9"
DARK_BLUE = "#5aa9f0"
LIGHT_BLUE = "#2b7cd3"
DARK_TEXT = "#e8eaef"
LIGHT_TEXT = "#1c2530"


def _bg(page):
    return page.evaluate(
        "getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()")


def _crosshair_stroke(page, key="lv"):
    """The hover crosshair guide's stroke attribute (a <line> in that panel's
    overlay group)."""
    return page.evaluate(
        f"document.querySelector('#panel-{key} svg .chart-overlay line')"
        ".getAttribute('stroke')")


def _heatmap_outline_strokes(page):
    """Stroke attributes of every outline rect drawn in the heatmap's overlay
    on hover (the day-row outline and the hour-cell outline)."""
    return page.evaluate(
        "Array.from(document.querySelectorAll("
        "'#panel-hm svg .chart-overlay rect')).map(r => r.getAttribute('stroke'))")


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


# Review finding 1 (task 30, fix round 1): three hover overlays — the
# crosshair guide, the bar-hover highlight, and the heatmap hover outline —
# were hardcoded white and vanished against the light chrome. They now
# resolve from the active palette's "text" role at draw time, like every
# other chart mark.
def test_light_theme_crosshair_overlay_is_not_white(auto_page, auto_dashboard_path):
    _open(auto_page, auto_dashboard_path, "light")
    hover_panel(auto_page, "lv")
    stroke = _crosshair_stroke(auto_page)
    assert stroke is not None
    assert "255,255,255" not in stroke and stroke.lower() != "#fff"
    assert stroke.lower() == LIGHT_TEXT


def test_dark_theme_crosshair_overlay_unchanged(auto_page, auto_dashboard_path):
    # The dark palette's text role reads near-white, so the fix stays
    # visually equivalent to the old literal white on the theme it always
    # worked on.
    _open(auto_page, auto_dashboard_path, "dark")
    hover_panel(auto_page, "lv")
    assert _crosshair_stroke(auto_page).lower() == DARK_TEXT


def test_light_theme_heatmap_hover_outline_is_not_white(auto_page, auto_dashboard_path):
    _open(auto_page, auto_dashboard_path, "light")
    hover_panel(auto_page, "hm", fx=0.6, fy=0.4)
    strokes = _heatmap_outline_strokes(auto_page)
    assert strokes, "no heatmap overlay outline found"
    for s in strokes:
        assert s is not None
        assert "255,255,255" not in s and s.lower() != "#fff"
        assert s.lower() == LIGHT_TEXT


# Review finding 2 (task 30, fix round 1): applyTheme redrew every panel and
# the sparklines but left a currently-pinned floating tooltip showing the
# prior palette's dot colors until the next hover. A pinned tooltip is a
# frozen copy of innerHTML captured at pin time and LAST_HOVER does not carry
# the resolved series color needed to rebuild it, so the simpler and honest
# fix is to clear the pin on a live switch rather than show stale hues.
def test_pinned_tooltip_hides_on_live_theme_switch(auto_page, auto_dashboard_path):
    _open(auto_page, auto_dashboard_path, "dark")
    box = hover_panel(auto_page, "lv")
    auto_page.mouse.click(box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5)
    auto_page.wait_for_timeout(60)
    assert auto_page.evaluate("__chartsDebug.pinned()") == {"key": "lv"}
    btn = auto_page.locator("#theme-btn")
    btn.click()   # auto -> dark (no-op palette-wise)
    btn.click()   # dark -> light: the palette actually switches now
    assert auto_page.evaluate("__chartsDebug.pinned()") is None
    assert auto_page.locator(".chart-tooltip.is-pinned").evaluate("el => el.hidden")
