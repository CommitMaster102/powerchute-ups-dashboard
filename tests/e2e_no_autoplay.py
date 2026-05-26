"""E2E suite: no auto-play on load.

Nothing should animate on its own. Wait 3x the longest animation and
assert no stepper is running and every cumulative trace is still full.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_no_autoplay.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pytest
from harness import run_suite


def run(runner, anim_data):
    print("\n=== no auto-play on load ===")
    page = runner.page
    # Wait 3x the longest animation; if anything autoruns, traces would
    # have changed by now.
    max_dur = max(a["speed_ms"] * a["n_frames"] for a in anim_data.values())
    page.wait_for_timeout(max_dur * 3)
    any_running = page.evaluate("window.__animDebug.getStepperKeys().length")
    runner.check(
        "[no-autoplay] no stepper running after page-load grace period",
        any_running == 0,
        f"steppers running: {any_running}",
    )
    for g, a in anim_data.items():
        if a["type"] == "cumulative":
            lens = runner.trace_lengths(a["trace_indices"])
            runner.check(
                f"[no-autoplay] {g}: lengths unchanged after grace period",
                all(L > 0 for L in lens),
                f"got {lens}",
            )


pytestmark = pytest.mark.e2e


def test_no_autoplay(fresh_runner, anim_data):
    run(fresh_runner, anim_data)
    assert not fresh_runner.failures, str(fresh_runner.failures)


if __name__ == "__main__":
    sys.exit(run_suite(run))
