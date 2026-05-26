"""Run every E2E suite in ONE Chromium session.

The suites also run standalone (`python tests/e2e_pause_freeze.py`), but
launching a browser per file is slow. This runner opens the dashboard
once, shares a single TestRunner across all e2e_* suites, reloads between
the suites that need a pristine page, and prints one aggregate result.

Run (regenerate the dashboard first):
    .venv\\Scripts\\python.exe analyze_ups.py
    .venv\\Scripts\\python.exe tests\\run_e2e.py

Requires playwright + chromium (test-only, not in requirements.txt):
    .venv\\Scripts\\pip install playwright
    .venv\\Scripts\\python -m playwright install chromium
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright
from harness import (
    DASHBOARD, TestRunner, wait_ready, stash_full_lengths,
    assert_expected_speeds, summarize,
)

import e2e_diagnostics
import e2e_initial_render
import e2e_no_autoplay
import e2e_play_completion
import e2e_isolation
import e2e_pause_freeze
import e2e_resume
import e2e_time_label
import e2e_realistic_play
import e2e_axis_range
import e2e_state_machine
import e2e_concurrency


def main() -> int:
    if not DASHBOARD.exists():
        print(f"Dashboard not found at {DASHBOARD}. Run analyze_ups.py first.")
        return 2

    url = DASHBOARD.resolve().as_uri()
    print(f"Opening {url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1920, "height": 2800})
        page.goto(url)
        wait_ready(page)

        runner = TestRunner(page)
        anim_data = runner.get_anim_data()
        if not anim_data:
            print("FATAL: could not extract ANIM_DATA from page")
            browser.close()
            return 3
        assert_expected_speeds(anim_data)
        stash_full_lengths(page, anim_data)

        def reload():
            page.reload()
            wait_ready(page)
            stash_full_lengths(page, anim_data)

        # Suites that read fresh page state run first; the per-group suites
        # are independent (each does its own play and leaves data full), so
        # their order relative to each other doesn't matter.
        e2e_diagnostics.run(runner, anim_data)
        e2e_initial_render.run(runner, anim_data)
        e2e_no_autoplay.run(runner, anim_data)
        e2e_play_completion.run(runner, anim_data)
        e2e_isolation.run(runner, anim_data)
        e2e_pause_freeze.run(runner, anim_data)
        e2e_resume.run(runner, anim_data)
        e2e_time_label.run(runner, anim_data)

        # These want a clean page.
        reload()
        e2e_realistic_play.run(runner, anim_data)
        e2e_axis_range.run(runner, anim_data)   # reloads internally mid-suite
        reload()
        e2e_state_machine.run(runner, anim_data)
        e2e_concurrency.run(runner, anim_data)

        code = summarize(runner)
        browser.close()
        return code


if __name__ == "__main__":
    sys.exit(main())
