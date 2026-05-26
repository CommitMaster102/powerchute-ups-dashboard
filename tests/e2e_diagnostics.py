"""E2E suite: ORIG-snapshot integrity (+ diagnostics).

Guards the bug where Plotly 3.x serializes y as a binary blob, safeArray()
returned [] for it, so stashOriginals captured y=[] for every trace —
applyFrame then restyled y to [] each frame and the lines went invisible
even though x was full and the axis range was right. Asserts the captured
ORIG snapshot holds real, length-matched x/y for every group. The leading
prints are diagnostics (z type, dl trace layout).

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_diagnostics.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import run_suite


def run(runner, anim_data):
    page = runner.page

    # ---- diagnostics (prints only) ----
    if "hm" in anim_data:
        print("\n=== Diagnostic: heatmap z type ===")
        print(f"  z shape: {runner.heatmap_z_shape(anim_data['hm']['trace_idx'])}")
    if "lv" in anim_data:
        print("\n=== Diagnostic: dl trace x layout ===")
        full_orig = page.evaluate(
            "(() => {const o = window.__animDebug.getOrig('lv');"
            " if (!o) return null;"
            " return o.map(e => ({len: e.x.length, xms0: e.xms[0], xmsN: e.xms[e.xms.length-1]}));})()"
        )
        print(f"  ORIG.dl capture by trace: {full_orig}")
        print(f"  cutoffs_ms[0..2] = {anim_data['lv']['cutoffs_ms'][:3]}")
        print(f"  cutoffs_ms[-3..]  = {anim_data['lv']['cutoffs_ms'][-3:]}")
        for ti, idx in enumerate(anim_data["lv"]["trace_indices"][:2]):
            shape = page.evaluate(
                f"(() => {{const tr = ({runner.gd_handle()}).data[{idx}];"
                f" const x = tr.x; if (!x) return {{kind:'null'}};"
                f" return {{ctor: x.constructor && x.constructor.name,"
                f"  isArr: Array.isArray(x), len: x.length,"
                f"  type0: typeof x[0], val0: String(x[0]).slice(0, 30),"
                f"  hasInputArr: 'bdata' in x || '_inputArray' in x,"
                f"  keys: Object.keys(x).slice(0, 6)}};}})()"
            )
            print(f"  trace[{idx}]: {shape}")
        captured = page.evaluate(
            "(() => {const orig = window.__animDebug.getOrig('lv');"
            " if (!orig || !orig[0]) return {kind:'no-orig'};"
            " const x = orig[0].x;"
            " return {ctor: x.constructor.name, isArr: Array.isArray(x),"
            "  len: x.length, val0: String(x[0]).slice(0, 30),"
            "  xms0: orig[0].xms && orig[0].xms[0]};})()"
        )
        print(f"  ORIG.dl[0].x: {captured}")

    # ---- bug guard: ORIG snapshot has real data ----
    print("\n=== ORIG snapshot has real data (bug guard) ===")
    for g, a in anim_data.items():
        if a["type"] == "cumulative":
            orig = page.evaluate(
                f"window.__animDebug.getOrig({json.dumps(g)}).map(o => "
                f"({{xlen: o.x.length, ylen: o.y.length}}))"
            )
            for ti, lens in enumerate(orig):
                runner.check(
                    f"[orig {g}#{ti}] x and y both non-empty",
                    lens["xlen"] > 0 and lens["ylen"] > 0
                    and lens["xlen"] == lens["ylen"],
                    f"x={lens['xlen']} y={lens['ylen']}",
                )
        elif a["type"] == "marker":
            orig = page.evaluate(
                f"(() => {{const o = window.__animDebug.getOrig({json.dumps(g)});"
                f" return {{xlen: o.x.length, ylen: o.y.length}};}})()"
            )
            runner.check(
                f"[orig {g}] marker x and y captured",
                orig["xlen"] > 0 and orig["ylen"] > 0,
                f"x={orig['xlen']} y={orig['ylen']}",
            )


if __name__ == "__main__":
    sys.exit(run_suite(run))
