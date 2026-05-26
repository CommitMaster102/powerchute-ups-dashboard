"""E2E suite: axis range stays sane after play (visual-bug guard).

plotly.js #2546/#2823: restyling a date-typed trace with x=[] flips the
axis to a numeric default and silently drops the saved date range, so a
panel snaps to the empty Jan 2000–2001 axis. This pins every animated
axis and confirms each datalog panel's x-axis still starts in 2026 after
a full play.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_axis_range.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import JITTER_MS, run_suite, wait_ready

AXIS_DUMP_JS = """(() => {
  const gd = document.getElementsByClassName('plotly-graph-div')[0];
  const fl = gd._fullLayout || {};
  const out = {};
  Object.keys(fl).filter(k => k.startsWith('xaxis') || k.startsWith('yaxis')).forEach(k => {
    const ax = fl[k];
    if (ax && ax.range) out[k] = {
      range: ax.range.map(v => String(v).slice(0, 12)),
      autorange: ax.autorange,
      type: ax.type,
    };
  });
  return out;
})()"""


def run(runner, anim_data):
    print("\n=== axis range stays sane after play ===")
    page = runner.page

    # Capture all axis ranges before play.
    axis_dump = page.evaluate(AXIS_DUMP_JS)
    # Pretty-print datalog panel axes (xaxis..xaxis4); pw lives on xaxis5.
    ts_axes = ["xaxis", "xaxis2", "xaxis3", "xaxis4"]
    print("  pre-play axis ranges (dl panels):")
    for ax in ts_axes:
        if ax in axis_dump:
            print(f"    {ax}: {axis_dump[ax]}")

    # Reload & re-test: click play, wait for finish, dump again.
    page.reload()
    wait_ready(page)
    runner.click('.anim-play[data-group="lv"]')
    nominal = anim_data["lv"]["speed_ms"] * anim_data["lv"]["n_frames"]
    page.wait_for_timeout(nominal + JITTER_MS)
    post_axis_dump = page.evaluate(AXIS_DUMP_JS)
    print("  post-play axis ranges (ts panels):")
    for ax in ts_axes:
        if ax in post_axis_dump:
            print(f"    {ax}: {post_axis_dump[ax]}")
    # Assert: every ts panel x-axis must still be in 2026.
    for ax in ts_axes:
        if ax not in post_axis_dump:
            continue
        r = post_axis_dump[ax].get("range", ["", ""])
        r_lower = str(r[0])
        runner.check(
            f"[axis {ax}] post-play range starts in 2026 (not default 2000)",
            r_lower.startswith("2026"),
            f"post range = {r}",
        )


if __name__ == "__main__":
    sys.exit(run_suite(run))
