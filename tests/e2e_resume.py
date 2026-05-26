"""E2E suite: resume after pause.

For every cumulative group: ▶ → ⏸ mid-anim → ▶ again. The second Play
must restart cleanly and the panel must end on the full data. See
TestRunner.test_play_resumes_full_after_pause_then_play.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_resume.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import run_suite


def run(runner, anim_data):
    for g in anim_data:
        if anim_data[g]["type"] != "cumulative":
            continue
        print(f"\n=== resume: group {g!r} ===")
        runner.test_play_resumes_full_after_pause_then_play(g, anim_data)
        runner.page.wait_for_timeout(150)


if __name__ == "__main__":
    sys.exit(run_suite(run))
