"""Convenience wrapper: run the E2E (browser) suites via pytest.

The suites are pytest tests (see conftest.py + e2e_*.py). This just shells
pytest for the `e2e` marker so the old `python tests/run_e2e.py` entry point
keeps working. Equivalent to:  pytest tests -m e2e
Pass extra pytest args through, e.g.:  python tests/run_e2e.py -n auto -q

A single suite runs via pytest:  pytest tests/e2e_zoom.py
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    extra = sys.argv[1:] or ["-v"]
    return subprocess.call([sys.executable, "-m", "pytest", HERE, "-m", "e2e", *extra])


if __name__ == "__main__":
    sys.exit(main())
