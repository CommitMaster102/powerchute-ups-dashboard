"""E2E: keyboard sample step-through (roadmap item 21). Enter toggles inspect
mode on a focused line-kind chart, ArrowLeft/ArrowRight walk its full sample
array one entry at a time (the tooltip follows via the same code path as a
hover), Escape leaves inspect mode, and arrows resume panning once it is off.
Bar, heatmap, and the event timeline are deliberately not inspectable — their
"samples" are not a single ordered index the way a line panel's are."""
from __future__ import annotations

import pytest
from harness import hover_panel, xwin

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


def test_inspect_exits_when_hover_moves_to_another_panel(dash):
    # Finding 1: inspect mode must follow the panel. Entering it on "lv" and
    # then hovering a different panel should drop the mode entirely — the
    # cue disappears from "lv" and the arrow keys resume acting on "ul".
    # Zoom "ul" to a mid-range window first: panning right from the full
    # window immediately clamps back to full and would pass even unguarded
    # (see the same caveat in test_arrows_do_not_pan_while_inspecting above).
    dash.evaluate("(() => { const f = __chartsDebug.fullXwin('ul');"
                  "__chartsDebug.setWindow(f[0] + (f[1]-f[0])*0.3, f[0] + (f[1]-f[0])*0.7); })()")
    dash.wait_for_timeout(60)
    # hover_panel dispatches a real pointermove every call (unlike .focus(),
    # which is a no-op re-firing on an already-focused element), so it is
    # what reliably (re-)establishes ACTIVE_KEY here regardless of whichever
    # panel a previous test happened to leave focused on this shared page.
    hover_panel(dash, "lv")
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is not None
    hover_panel(dash, "ul")
    assert _inspect(dash) is None
    assert not dash.locator("#panel-lv").evaluate("el => el.classList.contains('is-inspecting')")
    assert dash.locator('.inspect-badge[data-panel="lv"]').evaluate("el => el.hidden")
    win_before = xwin(dash, "ul")
    dash.keyboard.press("ArrowRight")
    dash.wait_for_timeout(60)
    assert xwin(dash, "ul") != win_before, "arrows did not resume panning the newly active panel"


def test_inspect_exits_when_focus_moves_to_another_panel(dash):
    # Same as above, but via keyboard Tab (focus) rather than mouse hover.
    hover_panel(dash, "lv")
    dash.locator("#panel-lv").focus()
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is not None
    dash.locator("#panel-ul").focus()
    dash.wait_for_timeout(60)
    assert _inspect(dash) is None
    assert not dash.locator("#panel-lv").evaluate("el => el.classList.contains('is-inspecting')")
    assert dash.locator('.inspect-badge[data-panel="lv"]').evaluate("el => el.hidden")


def test_inspect_survives_hover_of_the_same_panel_lightbox(dash):
    # Guardrail for the lightbox path named in the finding: opening the
    # lightbox for the panel already being inspected and moving the mouse
    # inside it must NOT exit inspect mode (same key, not a different panel).
    hover_panel(dash, "lv")
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is not None
    dash.evaluate("__chartsDebug.openLightbox('lv')")
    dash.wait_for_timeout(60)
    box = dash.locator("#lightbox-chart svg").bounding_box()
    dash.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    dash.wait_for_timeout(60)
    assert _inspect(dash) is not None
    assert _inspect(dash)["key"] == "lv"


def test_inspect_cue_shows_on_lightbox_of_inspected_panel(dash):
    # Item B2(a): opening the lightbox for the panel already being inspected
    # must carry the inspect cue (outline + badge) onto the expanded view too.
    hover_panel(dash, "lv")
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is not None
    dash.evaluate("__chartsDebug.openLightbox('lv')")
    dash.wait_for_timeout(60)
    assert dash.locator("#lightbox-chart").evaluate(
        "el => el.classList.contains('is-inspecting')"), "lightbox view missing inspect outline"
    assert not dash.locator("#lightbox-inspect-badge").evaluate("el => el.hidden"), \
        "lightbox inspect badge stayed hidden"
    # Closing the lightbox clears its mirrored cue but leaves grid inspect on.
    dash.keyboard.press("Escape")
    dash.wait_for_timeout(60)


def test_opening_a_different_panels_lightbox_exits_inspect(dash):
    # Item B2(b): expanding a DIFFERENT panel via its tool (no hover/focus
    # crossing) must drop inspect mode rather than leave it orphaned on "lv".
    hover_panel(dash, "lv")
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    assert _inspect(dash)["key"] == "lv"
    dash.evaluate("__chartsDebug.openLightbox('bv')")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is None, "inspect mode survived opening another panel's lightbox"
    assert not dash.locator("#panel-lv").evaluate("el => el.classList.contains('is-inspecting')")
    assert dash.locator('.inspect-badge[data-panel="lv"]').evaluate("el => el.hidden")


def test_zoom_key_during_inspect_resyncs_tooltip(dash):
    # Finding 2: +/-/0 during inspect re-render the SVG (a fresh, empty
    # overlay) without re-invoking the inspect tooltip/crosshair — fixed by
    # re-showing the inspected sample after the re-render.
    hover_panel(dash, "lv")
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    dash.keyboard.press("ArrowRight")
    dash.wait_for_timeout(60)
    idx_before = _inspect(dash)["idx"]
    hov_before = dash.evaluate("__chartsDebug.hover('lv')")
    dash.keyboard.press("+")
    dash.wait_for_timeout(60)
    assert _inspect(dash)["idx"] == idx_before, "a zoom key must not itself step the sample"
    hov_after = dash.evaluate("__chartsDebug.hover('lv')")
    assert hov_after is not None and hov_after["ts"] == hov_before["ts"]
    overlay_children = dash.evaluate(
        "document.querySelector('#panel-lv .chart-overlay').children.length")
    assert overlay_children > 0, "crosshair did not re-sync after a zoom key during inspect"
    tt = dash.locator(".chart-tooltip:not(.is-pinned)")
    assert not tt.evaluate("el => el.hidden"), "tooltip went stale/hidden after a zoom key during inspect"


def test_reset_key_during_inspect_resyncs_tooltip(dash):
    # Same as above for the "0" reset-zoom key.
    dash.evaluate("(() => { const f = __chartsDebug.fullXwin('lv');"
                  "__chartsDebug.setWindow(f[0] + (f[1]-f[0])*0.3, f[0] + (f[1]-f[0])*0.7); })()")
    dash.wait_for_timeout(60)
    hover_panel(dash, "lv")
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    dash.keyboard.press("0")
    dash.wait_for_timeout(60)
    overlay_children = dash.evaluate(
        "document.querySelector('#panel-lv .chart-overlay').children.length")
    assert overlay_children > 0, "crosshair did not re-sync after the reset-zoom key during inspect"


def test_legend_toggle_exits_inspect_mode(dash):
    # Finding 3: toggling the inspected panel's own legend can re-target the
    # walking reference series against a different array — simplest clean
    # semantics is to just exit inspect mode on that toggle.
    hover_panel(dash, "bv")
    dash.keyboard.press("Enter")
    dash.wait_for_timeout(60)
    assert _inspect(dash) is not None
    assert _inspect(dash)["key"] == "bv"
    dash.evaluate("document.querySelectorAll('#panel-bv .legend-chip')[0]"
                  ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    dash.wait_for_timeout(80)
    assert _inspect(dash) is None
    assert not dash.locator("#panel-bv").evaluate("el => el.classList.contains('is-inspecting')")
    assert dash.locator('.inspect-badge[data-panel="bv"]').evaluate("el => el.hidden")
