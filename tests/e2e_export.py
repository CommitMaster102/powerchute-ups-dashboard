"""E2E: per-card PNG and CSV export, and legend series toggling."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("key,kind", [("lv", "line"), ("hm", "heatmap"), ("daily", "bar")])
def test_csv_export(dash, key, kind, tmp_path):
    with dash.expect_download() as dl:
        dash.evaluate(f"document.querySelector('.tool-csv[data-panel={key}]').click()")
    d = dl.value
    assert d.suggested_filename == f"ups-{key}.csv"
    path = tmp_path / d.suggested_filename
    d.save_as(path)
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 3, "csv has no data rows"
    header = lines[0]
    if kind == "line":
        assert header.startswith("time,")
    elif kind == "bar":
        assert header == "label,value"
    else:
        assert header.startswith("day,00:00")


def test_csv_respects_hidden_series(dash, tmp_path):
    # Hide the first bv series, export, and check the column is gone.
    dash.evaluate("document.querySelectorAll('#panel-bv .legend-chip')[0]"
                  ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    dash.wait_for_timeout(80)
    assert dash.evaluate("__chartsDebug.hidden('bv')") == [0]
    with dash.expect_download() as dl:
        dash.evaluate("document.querySelector('.tool-csv[data-panel=bv]').click()")
    path = tmp_path / "bv.csv"
    dl.value.save_as(path)
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert "reading" not in header
    assert "8h mean" in header


def test_png_export(dash, tmp_path):
    with dash.expect_download() as dl:
        dash.evaluate("document.querySelector('.tool-png[data-panel=lv]').click()")
    d = dl.value
    assert d.suggested_filename == "ups-lv.png"
    path = tmp_path / d.suggested_filename
    d.save_as(path)
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG file"
    assert len(data) > 5_000, "png suspiciously small — likely blank"


# The corner pixel of an exported panel is its baked card background — the
# active palette's panel color. Reading it back proves the PNG follows a theme
# switch (roadmap item 30): chart ink is concrete SVG attributes resolved from
# the active palette, so the serialized image reflects the switch by
# construction — but the roadmap called this out as a real risk, so it is
# checked rather than assumed.
_CORNER_JS = """async (key) => {
    const url = await __chartsDebug.pngDataUrl(key);
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url; });
    const c = document.createElement('canvas');
    c.width = img.width; c.height = img.height;
    const ctx = c.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const p = ctx.getImageData(3, 3, 1, 1).data;
    return [p[0], p[1], p[2]];
}"""


def test_png_reflects_active_theme_after_switch(dash):
    # Light panel background (#ffffff) exports a bright corner.
    dash.evaluate("__chartsDebug.setTheme('light')")
    dash.wait_for_function("__chartsDebug.theme() === 'light'")
    light_corner = dash.evaluate(_CORNER_JS, "lv")
    assert sum(light_corner) > 600, f"corner {light_corner} not light after switching to light"
    # Dark panel background (#181b21) exports a dark corner.
    dash.evaluate("__chartsDebug.setTheme('dark')")
    dash.wait_for_function("__chartsDebug.theme() === 'dark'")
    dark_corner = dash.evaluate(_CORNER_JS, "lv")
    assert sum(dark_corner) < 200, f"corner {dark_corner} not dark after switching to dark"


def test_legend_toggle_rerenders(dash):
    n_before = dash.evaluate("document.querySelectorAll('#panel-bv svg path[stroke]').length")
    dash.evaluate("document.querySelectorAll('#panel-bv .legend-chip')[0]"
                  ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    dash.wait_for_timeout(80)
    n_after = dash.evaluate("document.querySelectorAll('#panel-bv svg path[stroke]').length")
    assert n_after < n_before, "hiding a series did not remove its path"
    # Toggling back restores it.
    dash.evaluate("document.querySelectorAll('#panel-bv .legend-chip')[0]"
                  ".dispatchEvent(new MouseEvent('click', {bubbles: true}))")
    dash.wait_for_timeout(80)
    assert dash.evaluate("document.querySelectorAll('#panel-bv svg path[stroke]').length") == n_before
