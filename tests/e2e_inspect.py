"""E2E: keyboard sample step-through (roadmap item 21). Enter toggles inspect
mode on a focused line-kind chart, ArrowLeft/ArrowRight walk its full sample
array one entry at a time (the tooltip follows via the same code path as a
hover), Escape leaves inspect mode, and arrows resume panning once it is off.
Bar, heatmap, and the event timeline are deliberately not inspectable — their
"samples" are not a single ordered index the way a line panel's are."""
from __future__ import annotations

import pytest
from harness import xwin

pytestmark = pytest.mark.e2e


def _inspect(dash):
    return dash.evaluate("__chartsDebug.inspect()")


def test_enter_toggles_inspect_mode(dash):
    dash.locator("#panel-lv").focus()
    assert _inspect(dash) is None
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    state = _inspect(dash)
    assert state and state["key"] == "lv"
    assert isinstance(state["idx"], int)
    # Enter again toggles it back off.
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is None


def test_arrow_right_and_left_step_one_sample(dash):
    dash.locator("#panel-lv").focus()
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    idx0 = _inspect(dash)["idx"]
    dash.keyboard.press("ArrowRight")
    dash.wait_for_timeout(60)
    assert _inspect(dash)["idx"] == idx0 + 1
    dash.keyboard.press("ArrowRight")
    dash.wait_for_timeout(60)
    assert _inspect(dash)["idx"] == idx0 + 2
    dash.keyboard.press("ArrowLeft")
    dash.wait_for_timeout(60)
    assert _inspect(dash)["idx"] == idx0 + 1


def test_arrows_do_not_pan_while_inspecting(dash):
    # Zoom to a mid-range window first — panning right from the full window
    # immediately clamps back to full and would pass even unguarded, so this
    # needs room to pan on both sides to be a meaningful check.
    dash.evaluate("(() => { const f = __chartsDebug.fullXwin('lv');"
                  "__chartsDebug.setWindow(f[0] + (f[1]-f[0])*0.3, f[0] + (f[1]-f[0])*0.7); })()")
    dash.wait_for_timeout(60)
    win_before = xwin(dash, "lv")
    dash.locator("#panel-lv").focus()
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    for _ in range(5):
        dash.keyboard.press("ArrowRight")
    dash.wait_for_timeout(60)
    assert xwin(dash, "lv") == win_before


def test_arrow_right_advances_tooltip_and_aria_live(dash):
    dash.locator("#panel-lv").focus()
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    hov0 = dash.evaluate("__chartsDebug.hover('lv')")
    live0 = dash.locator("#inspect-live").text_content()
    assert live0 == ""   # entering inspect mode is not itself a step
    dash.keyboard.press("ArrowRight")
    dash.wait_for_timeout(60)
    hov1 = dash.evaluate("__chartsDebug.hover('lv')")
    assert hov1["ts"] != hov0["ts"], "stepping did not move the tooltip snap"
    live1 = dash.locator("#inspect-live").text_content()
    assert live1, "aria-live region was not updated on step"
    tt = dash.locator(".chart-tooltip:not(.is-pinned)")
    assert not tt.evaluate("el => el.hidden")


def test_escape_exits_and_arrows_pan_again(dash):
    # Zoom in first — see the comment in test_arrows_do_not_pan_while_inspecting
    # on why panning from the full window is not a meaningful check here.
    dash.evaluate("(() => { const f = __chartsDebug.fullXwin('lv');"
                  "__chartsDebug.setWindow(f[0] + (f[1]-f[0])*0.3, f[0] + (f[1]-f[0])*0.7); })()")
    dash.wait_for_timeout(60)
    dash.locator("#panel-lv").focus()
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is not None
    dash.keyboard.press("Escape")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is None
    win0 = xwin(dash, "lv")
    dash.keyboard.press("ArrowRight")
    dash.wait_for_timeout(60)
    assert xwin(dash, "lv") != win0, "arrows did not resume panning after Escape"


def test_other_panel_unaffected_while_inspecting(dash):
    dash.locator("#panel-lv").focus()
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    ul_win = xwin(dash, "ul")
    for _ in range(3):
        dash.keyboard.press("ArrowRight")
    dash.wait_for_timeout(60)
    assert xwin(dash, "ul") == ul_win
    assert dash.evaluate("__chartsDebug.zoom('ul')") is None
    assert _inspect(dash)["key"] == "lv"


def test_inspect_badge_and_outline_visible_only_while_active(dash):
    badge = dash.locator('.inspect-badge[data-panel="lv"]')
    assert badge.evaluate("el => el.hidden")
    dash.locator("#panel-lv").focus()
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    assert not badge.evaluate("el => el.hidden")
    assert dash.locator("#panel-lv").evaluate("el => el.classList.contains('is-inspecting')")
    dash.keyboard.press("Escape")
    dash.wait_for_timeout(60)
    assert badge.evaluate("el => el.hidden")
    assert not dash.locator("#panel-lv").evaluate("el => el.classList.contains('is-inspecting')")


def test_reset_all_clears_inspect_mode(dash):
    dash.locator("#panel-lv").focus()
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is not None
    dash.evaluate("window.__chartsDebug.resetAll()")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is None
    assert dash.locator("#panel-lv").evaluate("el => el.classList.contains('is-inspecting')") is False
    assert dash.locator('.inspect-badge[data-panel="lv"]').evaluate("el => el.hidden")


@pytest.mark.parametrize("key", ["daily", "hm", "ev"])
def test_non_line_panels_are_not_inspectable(dash, key):
    dash.locator(f"#panel-{key}").focus()
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is None
