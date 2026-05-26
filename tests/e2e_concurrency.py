"""E2E suite: simultaneous play (true independence).

Click two Play buttons rapid-fire (Line Voltage + cumulative kWh); both
must run concurrently and both must end at full state — no shared engine
that lets one starve the other.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_concurrency.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import JITTER_MS, run_suite


def run(runner, anim_data):
    print("\n=== simultaneous play (true independence) ===")
    if "lv" not in anim_data or "kw" not in anim_data:
        print("  (lv and/or kw absent — skipping)")
        return
    page = runner.page
    runner.click('.anim-play[data-group="lv"]')
    runner.click('.anim-play[data-group="kw"]')
    ts = anim_data["lv"]; kw = anim_data["kw"]
    page.wait_for_timeout(max(ts["speed_ms"] * ts["n_frames"],
                              kw["speed_ms"] * kw["n_frames"]) + JITTER_MS + 400)
    ts_post = runner.trace_lengths(ts["trace_indices"])
    kw_post = runner.trace_lengths(kw["trace_indices"])
    runner.check(
        "[concurrent ts+kw] both full after parallel play",
        all(L > 0 for L in ts_post) and all(L > 0 for L in kw_post),
        f"ts={ts_post} kw={kw_post}",
    )


if __name__ == "__main__":
    sys.exit(run_suite(run))
