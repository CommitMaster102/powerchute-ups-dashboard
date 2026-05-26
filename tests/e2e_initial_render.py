"""E2E suite: initial render.

Right after load — before any Play click — every animated trace must
already hold its full data (fig.frames is empty, so nothing should erase
or partially reveal anything). See TestRunner.test_initial_state_has_full_data.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_initial_render.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pytest
from harness import run_suite


def run(runner, anim_data):
    print("\n=== initial render ===")
    runner.test_initial_state_has_full_data(anim_data)


pytestmark = pytest.mark.e2e


def test_initial_render(fresh_runner, anim_data):
    run(fresh_runner, anim_data)
    assert not fresh_runner.failures, str(fresh_runner.failures)


if __name__ == "__main__":
    sys.exit(run_suite(run))
