"""E2E suite: realistic single-click play (per user report).

The plain user gesture: click ▶ once on Line Voltage, wait until the time
label reaches the last label (the real "finished" signal), and confirm
every trace is back to full and no stepper is still running.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_realistic_play.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import run_suite, JITTER_MS
from playwright.sync_api import TimeoutError as PWTimeout


def run(runner, anim_data):
    print("\n=== realistic single-click play (per user report) ===")
    page = runner.page
    ts = anim_data["lv"]
    nominal = ts["speed_ms"] * ts["n_frames"]
    gd = runner.gd_handle()

    pre_lens = runner.trace_lengths(ts["trace_indices"])
    print(f"  pre lengths: {pre_lens}")
    runner.click('.anim-play[data-group="lv"]')
    # Wait until the time label hits the last label (real user signal that
    # the animation finished).
    last_label = ts["labels"][-1]
    try:
        page.wait_for_function(
            f"document.querySelector('.anim-time[data-group=\"lv\"]').textContent"
            f" === {json.dumps(last_label)}",
            timeout=nominal * 4 + 2000,
        )
    except PWTimeout:
        print("  WARN: never reached last label")
    # Give restoreOriginals one more tick to land.
    page.wait_for_timeout(200)
    post_lens = runner.trace_lengths(ts["trace_indices"])
    is_stepping = page.evaluate("window.__animDebug.isStepping('lv')")
    # Read what each trace's x[0] and last-x look like to spot empty arrays.
    edges = page.evaluate(
        f"(() => ({json.dumps(ts['trace_indices'])}).map(i => {{"
        f"  const tr = ({gd}).data[i];"
        f"  const xs = tr.x; const len = xs ? xs.length : 0;"
        f"  return {{idx: i, len, first: len ? String(xs[0]).slice(0,20) : null,"
        f"   last: len ? String(xs[len-1]).slice(0,20) : null}};}}))()"
    )
    print(f"  post lengths: {post_lens}  stepping={is_stepping}")
    print(f"  per-trace x edges: {edges}")
    runner.check(
        "[realistic ts play] all 6 traces back to full",
        post_lens == pre_lens,
        f"pre={pre_lens} post={post_lens}",
    )
    runner.check(
        "[realistic ts play] no stepper still running",
        not is_stepping,
        "stepper still running"
    )


if __name__ == "__main__":
    sys.exit(run_suite(run, reload_first=True))
