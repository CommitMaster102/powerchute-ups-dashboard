"""E2E suite: pause freezes the reveal.

For every cumulative group: ▶, let it run briefly, ⏸, and assert the
reveal froze partway and stays frozen past the full nominal duration.
The play→pause→read sequence runs inside one page.evaluate so CDP
roundtrip latency can't let the short animation finish before the pause
lands. See TestRunner.test_pause_freezes_state.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_pause_freeze.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pytest
from harness import run_suite


def run(runner, anim_data):
    for g in anim_data:
        if anim_data[g]["type"] != "cumulative":
            continue
        print(f"\n=== pause-freeze: group {g!r} ===")
        runner.test_pause_freezes_state(g, anim_data)
        runner.page.wait_for_timeout(150)


pytestmark = pytest.mark.e2e


def test_pause_freeze(runner, anim_data):
    run(runner, anim_data)
    assert not runner.failures, str(runner.failures)


if __name__ == "__main__":
    sys.exit(run_suite(run))
