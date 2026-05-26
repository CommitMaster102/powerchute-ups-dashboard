"""E2E suite: time label advances.

For every group, the time label next to ▶ must start in the early part of
the timeline and move forward as the animation plays. See
TestRunner.test_time_label_advances.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_time_label.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import run_suite


def run(runner, anim_data):
    for g in anim_data:
        print(f"\n=== time-label: group {g!r} ===")
        runner.test_time_label_advances(g, anim_data)
        runner.page.wait_for_timeout(150)


if __name__ == "__main__":
    sys.exit(run_suite(run))
