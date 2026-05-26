"""Per-panel cumulative-reveal animation system for the dashboard.

The contract (deliberately non-standard): ``fig.frames`` is kept EMPTY. Python
emits pure metadata dicts (trace indices, cutoffs, labels); the hand-written JS
in ``animation.js`` reconstructs frames client-side on each Play click by
slicing the live trace data. See README / CLAUDE.md for the timezone trap that
``tests/test_animation_slicing.py`` guards.
"""
from __future__ import annotations

import json as _json
from pathlib import Path

import numpy as np
import pandas as pd

from pcss.stats import estimate_runtime

# The JS is maintained as a real .js file (syntax-highlightable, lint-able)
# with a single __ANIM_DATA__ token that we substitute at render time.
_ANIM_JS_TEMPLATE = (Path(__file__).resolve().parent / "animation.js").read_text(encoding="utf-8")


def _replay_metadata(animated: list, n_frames: int = 30):
    """Pure metadata for a cumulative-reveal animation. JS will slice the
    live trace data on Play click — no go.Frame objects are created here,
    so fig.frames stays empty and the initial render shows full data."""
    if not animated:
        return None
    all_x = np.concatenate([np.asarray(a[1]) for a in animated])
    if all_x.size < 5:
        return None
    t = pd.to_datetime(all_x)
    t_min, t_max = t.min(), t.max()
    if t_min == t_max:
        return None
    cutoffs = pd.date_range(start=t_min, end=t_max, periods=n_frames)
    return {
        "type": "cumulative",
        "trace_indices": [int(a[0]) for a in animated],
        "cutoffs_ms": [int(c.value // 1_000_000) for c in cutoffs],
        "labels": [c.strftime("%m-%d %H:%M") for c in cutoffs],
        "n_frames": int(n_frames),
    }


def _runtime_metadata(trace_idx: int | None, energy_df: pd.DataFrame,
                      n_frames: int = 30):
    """Pure metadata for the runtime-marker animation."""
    if trace_idx is None or energy_df.empty:
        return None
    s = energy_df.dropna(subset=["power_w"])
    if s.empty:
        return None
    t = pd.to_datetime(s["ts"])
    t_min, t_max = t.min(), t.max()
    if t_min == t_max:
        return None
    cutoffs = pd.date_range(t_min, t_max, periods=n_frames)
    marker_data = []
    for cutoff in cutoffs:
        mask = t <= cutoff
        w = 0.0 if not mask.any() else float(s.loc[mask.to_numpy(), "power_w"].iloc[-1])
        marker_data.append({"w": w, "rt": float(estimate_runtime(w))})
    return {
        "type": "marker",
        "trace_idx": int(trace_idx),
        "marker_data": marker_data,
        "labels": [c.strftime("%m-%d %H:%M") for c in cutoffs],
        "n_frames": int(n_frames),
    }


def _heatmap_metadata(trace_idx: int | None, pivot: pd.DataFrame):
    """Pure metadata for the heatmap day-by-day reveal."""
    if trace_idx is None or pivot is None or pivot.empty:
        return None
    n = len(pivot)
    if n < 2:
        return None
    z_full = pivot.to_numpy(dtype=float)
    z_list = [[(None if np.isnan(v) else float(v)) for v in row] for row in z_full]
    return {
        "type": "heatmap_reveal",
        "trace_idx": int(trace_idx),
        "z_full": z_list,
        "labels": [d.isoformat() for d in pivot.index],
        "n_frames": int(n),
    }


def _build_custom_controls_html(animations: list[dict]) -> str:
    """One floating ▶/⏸ pair per animation, anchored to its own chart.
    Each pair calls Plotly.animate() with an explicit frame list — no shared
    queue, no auto-play, fully independent."""
    if not animations:
        return ""
    # Layout constants — must match make_subplots(rows=7, vertical_spacing=0.080,
    # horizontal_spacing=0.10) and fig.update_layout(height=2400, margin=...).
    N_ROWS = 7
    V_SPACE = 0.080
    H_SPACE = 0.10
    HEIGHT = 2400
    MARGIN_T = 90
    MARGIN_B = 80
    MARGIN_L = 60
    MARGIN_R = 60
    panel_h = (1 - (N_ROWS - 1) * V_SPACE) / N_ROWS
    plot_h = HEIGHT - MARGIN_T - MARGIN_B

    def _row_top_px(row: int) -> float:
        # paper-y of the top of row R, converted to pixels-from-top-of-fig.
        # The `- 0.1` shifts the anchor DOWN into each panel (~0.1 of plot
        # paper height ≈ 223px). Placing the overlay below the subplot
        # title — inside the plot area — turned out to be the only
        # reliable way to keep alignment consistent across all rows;
        # anchoring above the panel collided with the figure's MARGIN_T
        # on row 1 and with the V_SPACE gap inconsistently on later rows.
        paper_top = 1 - (row - 1) * (panel_h + V_SPACE) - 0.1
        return MARGIN_T + (1 - paper_top) * plot_h

    def _col_right_paper(col: int) -> float:
        # paper-x of the right edge of the column (1 or 2)
        return 0.5 - H_SPACE / 2 if col == 1 else 1.0

    # Each animation gets pinned to one panel.
    POSITIONS = {
        "lv": (1, 1),   # Line Voltage
        "bv": (1, 2),   # Battery Voltage
        "ul": (2, 1),   # UPS Load
        "bc": (2, 2),   # Battery Capacity
        "pw": (3, 1),   # Power consumption
        "hm": (3, 2),   # Heatmap
        "kw": (4, 1),   # Cumulative kWh
        "rt": (5, 1),   # Runtime
    }

    overlays_html = []
    for a in animations:
        g = a["group"]
        if g not in POSITIONS:
            continue
        row, col = POSITIONS[g]
        top_px = (_row_top_px(row))
        rp = _col_right_paper(col)
        # CSS `right` from the right edge of the figure container. The
        # column-dependent inset (4*MARGIN_R for col=2, 2*MARGIN_R for
        # col=1) keeps each overlay tucked inside its own panel's data
        # area: col=2 needs a larger inset to clear the heatmap colorbar
        # on row 3, while col=1 only needs to clear the column gap.
        right_css = (f"calc((100% - {MARGIN_L + MARGIN_R}px) * {1 - rp:.4f}"
                     f" + {MARGIN_R * (4 if col == 2 else 2)}px)")
        first_label = a["labels"][0] if a["labels"] else ""
        overlays_html.append(f"""
<div class="anim-overlay" data-group="{g}" style="top: {top_px:.0f}px; right: {right_css};">
  <button class="anim-btn anim-play" data-group="{g}" type="button" title="Reproducir">▶</button>
  <button class="anim-btn anim-pause" data-group="{g}" type="button" title="Sin animación que pausar" disabled>⏸</button>
  <select class="anim-speed" data-group="{g}" title="Velocidad">
    <option value="0.25">0.25x</option>
    <option value="0.5">0.5x</option>
    <option value="1" selected>1x</option>
    <option value="2">2x</option>
    <option value="4">4x</option>
  </select>
  <select class="anim-easing" data-group="{g}" title="Curva (easing)">
    <option value="linear" selected>linear</option>
    <option value="smooth">smooth</option>
    <option value="ease-in">slow-in</option>
    <option value="ease-out">slow-out</option>
    <option value="ease-in-out">slow-in-out</option>
    <option value="cubic-in">cubic-in</option>
    <option value="cubic-out">cubic-out</option>
    <option value="cubic-in-out">cubic-in-out</option>
  </select>
  <span class="anim-time" data-group="{g}">{first_label}</span>
</div>""")

    overlays_block = "\n".join(overlays_html)
    # Pass the full per-animation metadata to JS — type, trace indices,
    # cutoffs/marker_data/z_full, labels, speed. JS uses this to rebuild
    # frames on each Play click.
    anim_data = {a["group"]: {k: v for k, v in a.items() if k != "title"} for a in animations}

    css = """
<style>
.anim-overlay { position: absolute; z-index: 1000; display: flex;
  align-items: center; gap: 4px; padding: 3px 6px;
  background: rgba(255,255,255,0.92); border: 1px solid #c8c8c8;
  border-radius: 5px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  font-family: Arial, Helvetica, sans-serif; pointer-events: auto; }
.anim-overlay .anim-btn { width: 26px; height: 22px; font-size: 11px;
  cursor: pointer; background: #fff; border: 1px solid #bbb;
  border-radius: 3px; color: #333; padding: 0; line-height: 1; }
.anim-overlay .anim-btn:hover { background: #eef; border-color: #88a; }
.anim-overlay .anim-btn:active { background: #ccd; }
.anim-overlay .anim-btn.is-active { background: #1f77b4; color: #fff; border-color: #1f77b4; }
.anim-overlay .anim-btn.is-resume { background: #2ca02c; color: #fff; border-color: #2ca02c; }
.anim-overlay .anim-btn:disabled { background: #f0f0f0; color: #bbb; border-color: #ddd;
  cursor: not-allowed; box-shadow: none; }
.anim-overlay .anim-btn:disabled:hover { background: #f0f0f0; border-color: #ddd; }
.anim-overlay .anim-speed,
.anim-overlay .anim-easing { height: 22px; font-size: 10px; padding: 0 2px;
  border: 1px solid #bbb; border-radius: 3px; background: #fff; color: #333;
  cursor: pointer; }
.anim-overlay .anim-time { font-size: 10px; color: #555;
  font-family: 'Consolas', 'Monaco', monospace; min-width: 78px; text-align: left; }
</style>"""

    # Inject the live metadata into the JS template (single __ANIM_DATA__ token).
    js = "\n" + _ANIM_JS_TEMPLATE.replace("__ANIM_DATA__", _json.dumps(anim_data))
    return css + overlays_block + js


def _inject_controls_into_html(html: str, animations: list[dict]) -> str:
    """Insert the custom controls just before </body>."""
    block = _build_custom_controls_html(animations)
    if not block:
        return html
    if "</body>" in html:
        return html.replace("</body>", block + "\n</body>", 1)
    return html + block


def _register_animation(*, group, title, speed_ms, build_data):
    """Compose the per-animation metadata dict consumed by the HTML+JS
    post-processor. Crucially we DO NOT touch fig.frames — keeping it empty
    means the initial render shows fig.data (full data) immediately, with
    zero animation interference. JS reconstructs frames on each Play click."""
    if not build_data:
        return None
    return {
        "group": group,
        "title": title,
        "speed_ms": speed_ms,
        **build_data,
    }
