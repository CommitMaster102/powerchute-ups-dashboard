"""E2E suite: per-group animation isolation.

While one group plays, every OTHER group's trace data must stay full the
whole time — no shared frame queue, no cross-bleed between panels. See
TestRunner.test_play_does_not_affect_other_groups.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_isolation.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import run_suite


def run(runner, anim_data):
    for g in anim_data:
        print(f"\n=== isolation: group {g!r} plays ===")
        runner.test_play_does_not_affect_other_groups(g, anim_data)
        runner.page.wait_for_timeout(150)


if __name__ == "__main__":
    sys.exit(run_suite(run))
