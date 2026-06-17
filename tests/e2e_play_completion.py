"""E2E suite: play-to-completion.

For every animation group, click ▶, wait for the engine to report the
'ended' state, and assert the panel lands back on the original full data
within the expected time window. See TestRunner.test_play_completes_with_full_data.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_play_completion.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pytest
from harness import ALL_GROUPS, run_suite


def run(runner, anim_data):
    for g in anim_data:
        print(f"\n=== play-completion: group {g!r} ===")
        runner.test_play_completes_with_full_data(g, anim_data)
        runner.page.wait_for_timeout(150)


pytestmark = pytest.mark.e2e


# One item per group. `runner` reloads first, so each asserts its panel returns
# to FULL from a pristine baseline regardless of what ran before it.
@pytest.mark.parametrize("group", ALL_GROUPS)
def test_play_completion(runner, anim_data, group):
    if group not in anim_data:
        pytest.skip(f"group {group!r} absent from ANIM_DATA")
    runner.test_play_completes_with_full_data(group, anim_data)
    assert not runner.failures, str(runner.failures)


if __name__ == "__main__":
    sys.exit(run_suite(run))
