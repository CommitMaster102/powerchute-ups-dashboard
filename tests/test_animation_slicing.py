"""Verify that the cumulative-reveal logic the JS will execute lines up
with the cutoffs_ms metadata produced by Python. The bug was: Plotly
serializes datetimes as ISO strings WITHOUT a timezone (e.g.
"2026-04-27T23:18:52.000000"), and JavaScript's Date.parse interprets
those as LOCAL time — but our Python cutoffs are pd.Timestamp.value
which treats naive timestamps as UTC. In a non-UTC locale that produced
a multi-hour offset, so cutoff[0] fell BEFORE every data point and the
"end" pointer in the JS slicing loop stayed at 0 → empty arrays →
panels rendered with the default Jan 2000–Jan 2001 axis.

This test simulates the JS slicing in Python under both interpretations
and asserts that, with the UTC-forcing fix, frame 0 contains at least
one point and the last frame contains every point.

Run (a unit test — no browser needed):
    .venv\\Scripts\\python.exe tests\\test_animation_slicing.py"""

from __future__ import annotations

import os
import sys
import time

import numpy as np
import pandas as pd

# Import from the repo root regardless of the working dir.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pcss.animation import _replay_metadata


def _ms_local(iso: str) -> int:
    """Mimic JS Date.parse(iso_without_tz) — interprets as LOCAL time."""
    # Python's strptime + mktime treats the parsed time as local.
    t = time.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S")
    return int(time.mktime(t)) * 1000


def _ms_utc(iso: str) -> int:
    """Mimic JS Date.parse(iso + 'Z') — interprets as UTC."""
    return int(pd.Timestamp(iso).value // 1_000_000)


def _slice_end(xs_ms: list[int], cutoff_ms: int) -> int:
    """The exact loop the JS in dashboard.html runs."""
    end = 0
    for j, x in enumerate(xs_ms):
        if x > cutoff_ms:
            break
        end = j + 1
    return end


def make_fixture():
    """Build a small `animated`-shaped fixture: 3 traces, ~20 points each,
    five-minute cadence, mid-day start so we'd cross a TZ boundary."""
    start = pd.Timestamp("2026-04-27 23:18:00")
    x = pd.date_range(start, periods=20, freq="5min").to_numpy()
    return [
        (0, x, np.random.rand(20)),
        (1, x, np.random.rand(20)),
        (2, x, np.random.rand(20)),
    ]


def main() -> int:
    animated = make_fixture()
    meta = _replay_metadata(animated, n_frames=10)
    assert meta is not None, "metadata builder returned None"
    cutoffs_ms = meta["cutoffs_ms"]
    assert len(cutoffs_ms) == 10

    # Plotly serializes the datetimes as ISO strings without 'Z'.
    iso_xs = [pd.Timestamp(v).strftime("%Y-%m-%dT%H:%M:%S.%f") for v in animated[0][1]]

    # ---- Pre-fix behaviour: JS used Date.parse(iso) → LOCAL ms.
    xs_ms_local = [_ms_local(s) for s in iso_xs]
    end_local_first = _slice_end(xs_ms_local, cutoffs_ms[0])
    end_local_last = _slice_end(xs_ms_local, cutoffs_ms[-1])

    # ---- Post-fix behaviour: JS appends 'Z' → UTC ms.
    xs_ms_utc = [_ms_utc(s) for s in iso_xs]
    end_utc_first = _slice_end(xs_ms_utc, cutoffs_ms[0])
    end_utc_last = _slice_end(xs_ms_utc, cutoffs_ms[-1])

    print(f"local-time interpretation : frame[0]={end_local_first}, frame[-1]={end_local_last}")
    print(f"utc-forced interpretation : frame[0]={end_utc_first}, frame[-1]={end_utc_last}")
    print(f"total points              : {len(iso_xs)}")

    failures = []
    if end_local_first != 0:
        # The bug only reproduces in non-UTC locales. Don't fail in UTC,
        # but flag if we see it.
        print("(running in UTC locale — the bug is invisible here)")
    if end_utc_first < 1:
        failures.append("UTC fix should include at least 1 point in frame 0")
    if end_utc_last != len(iso_xs):
        failures.append(
            f"UTC fix should include all {len(iso_xs)} points in last frame, got {end_utc_last}")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("OK: with UTC parsing both sides agree, animation slicing is non-empty.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
