"""E2E suite: per-group animation isolation.

While one group plays, every OTHER group's trace data must stay full the
whole time — no shared frame queue, no cross-bleed between panels. See
TestRunner.test_play_does_not_affect_other_groups.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_isolation.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pytest
from harness import ALL_GROUPS, run_suite


def run(runner, anim_data):
    for g in anim_data:
        print(f"\n=== isolation: group {g!r} plays ===")
        runner.test_play_does_not_affect_other_groups(g, anim_data)
        runner.page.wait_for_timeout(150)


pytestmark = pytest.mark.e2e


# One test item per group so pytest-xdist spreads the 8 plays across cores
# instead of looping them serially in a single test.
@pytest.mark.parametrize("group", ALL_GROUPS)
def test_isolation(runner, anim_data, group):
    if group not in anim_data:
        pytest.skip(f"group {group!r} absent from ANIM_DATA")
    runner.test_play_does_not_affect_other_groups(group, anim_data)
    assert not runner.failures, str(runner.failures)


if __name__ == "__main__":
    sys.exit(run_suite(run))
