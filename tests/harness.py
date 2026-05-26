"""Shared harness for the dashboard E2E suites.

`test_e2e.py` used to be one ~730-line monolith that drove every animation
overlay in a single Playwright session. It was split into focused
`e2e_*.py` files (one suite each) so a single concern can be run on its own
without paying for the whole battery — the full run is slow. This module
holds everything those files share:

  * TestRunner — the assertion + Plotly-introspection helpers, plus the
    per-group test methods (play / isolation / pause / resume / label).
  * Session helpers — launch Chromium, open the rendered dashboard, wait
    for Plotly to settle, and extract the page's ANIM_DATA contract.
  * run_suite() — the standalone entry point each e2e_*.py uses under its
    own `if __name__ == "__main__"`, so `python tests/e2e_pause_freeze.py`
    spins up its own browser and reports pass/fail just for that file.

Playwright (+ chromium) is required but intentionally NOT in
requirements.txt — it's a test-only dependency:
    .venv\\Scripts\\pip install playwright
    .venv\\Scripts\\python -m playwright install chromium

The dashboard must exist first: run `analyze_ups.py` to (re)generate
output/dashboard.html before running any E2E suite.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import Page, sync_playwright
from playwright.sync_api import TimeoutError as PWTimeout

# output/dashboard.html lives one level up from tests/.
DASHBOARD = Path(__file__).resolve().parent.parent / "output" / "dashboard.html"

# Expected per-group animation speeds (ms/frame). Ground truth is ANIM_DATA
# on the page; this dict is the generator-vs-test alignment check — if
# analyze_ups.py changes a speed and forgets to update here, the session
# setup asserts loudly. Keep in sync with the _register_animation() calls.
EXPECTED_GROUPS = {
    "lv": 40,
    "bv": 40,
    "ul": 40,
    "bc": 40,
    "pw": 40,
    "hm": 180,
    "kw": 40,
    "rt": 40,
}

# Headroom for rAF drift + Plotly redraw cost on top of the nominal
# duration (n_frames * speed_ms). The rAF engine drives by wall clock, so
# a busy/dense panel finishes near nominal but its final redraw can land a
# little late.
JITTER_MS = 2000

# Extra budget for the Playwright<->CDP click roundtrip and the
# wait-for-state poll. Measured click latency is ~300-500ms; we allow more
# so a loaded CI box doesn't flap, while a real >2x animation hang still
# trips the elapsed upper bound.
RPC_OVERHEAD_MS = 1500


# ----------------------------------------------------------------------
# Test harness
# ----------------------------------------------------------------------
class TestRunner:
    def __init__(self, page: Page):
        self.page = page
        self.failures: list[str] = []
        self.passed: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed.append(name)
            print(f"  PASS  {name}")
        else:
            msg = f"{name}: {detail}"
            self.failures.append(msg)
            print(f"  FAIL  {msg}")

    def js(self, expr: str):
        return self.page.evaluate(expr)

    def gd_handle(self) -> str:
        return "document.getElementsByClassName('plotly-graph-div')[0]"

    def trace_lengths(self, indices: list[int]) -> list[int]:
        gd = self.gd_handle()
        return self.js(
            f"({json.dumps(indices)}).map(i => "
            f"({{xs: ({gd}).data[i] && ({gd}).data[i].x ? ({gd}).data[i].x.length : 0}}.xs))"
        )

    def heatmap_filled_rows(self, idx: int) -> int:
        """Count z-rows that have any non-null cell. Modern Plotly serializes
        z as a binary blob in `gd.data[i].z` ({dtype, bdata, shape}); the
        decoded form lives on `gd._fullData[i].z` as Array<Float64Array>.
        Restyle output also lands on _fullData, so it's the right place
        to read post-animation state."""
        gd = self.gd_handle()
        return self.js(
            f"(() => {{"
            f"  const fd = ({gd})._fullData;"
            f"  const z = (fd && fd[{idx}] && fd[{idx}].z) || ({gd}).data[{idx}].z;"
            f"  if (!z) return 0;"
            f"  let n = 0;"
            f"  for (const r of Array.from(z)) {{"
            f"    const cells = Array.from(r || []);"
            f"    if (cells.some(c => c != null && !Number.isNaN(c))) n++;"
            f"  }}"
            f"  return n;"
            f"}})()"
        )

    def heatmap_z_shape(self, idx: int) -> dict:
        """Diagnostic: what kind of z does Plotly hand us?"""
        gd = self.gd_handle()
        return self.js(
            f"(() => {{"
            f"  const z = ({gd}).data[{idx}].z;"
            f"  if (!z) return {{kind: 'null'}};"
            f"  const keys = Object.keys(z);"
            f"  const fullZ = ({gd})._fullData ? ({gd})._fullData[{idx}].z : null;"
            f"  return {{"
            f"    ctor: z.constructor && z.constructor.name,"
            f"    keys: keys.slice(0, 8),"
            f"    sample: z[keys[0]],"
            f"    fullCtor: fullZ && fullZ.constructor && fullZ.constructor.name,"
            f"    fullLen: fullZ && fullZ.length,"
            f"    fullRow0Type: fullZ && fullZ[0] && (fullZ[0].constructor.name + '@' + (fullZ[0].length || 'no-len'))"
            f"  }};"
            f"}})()"
        )

    def get_anim_data(self) -> dict:
        # Re-parse the inline metadata from the actual HTML by reading
        # ANIM_DATA via a safe re-evaluation. The JS closure scopes the
        # const, so we extract it from the script tag.
        return self.js(
            "(() => {const s = Array.from(document.scripts).map(x => x.textContent).join('\\n');"
            " const m = s.match(/const ANIM_DATA = (\\{[\\s\\S]*?\\});/);"
            " return m ? JSON.parse(m[1]) : null;})()"
        )

    def click(self, selector: str) -> None:
        # The play/pause buttons are stateful (disabled while a previous
        # animation is mid-flight). Wait up to 30s for it to land in an
        # actionable state — that's longer than any single animation
        # plus jitter so a hung playback shows as a real failure.
        try:
            self.page.wait_for_function(
                f"() => {{ const e = document.querySelector({json.dumps(selector)});"
                f" return e && !e.disabled; }}",
                timeout=30000,
            )
        except Exception:
            # Surface whatever state info we have to diagnose the hang.
            try:
                info = self.page.evaluate(
                    f"(() => {{ const e = document.querySelector({json.dumps(selector)});"
                    f" const g = e && e.dataset && e.dataset.group;"
                    f" return {{disabled: e && e.disabled, classes: e && e.className,"
                    f" state: g && window.__animDebug.getState(g),"
                    f" savedT: g && window.__animDebug.getSavedT(g),"
                    f" playing: g && window.__animDebug.isPlaying(g)}}; }})()"
                )
                print(f"  >>> click({selector!r}) timed out — {info}")
            except Exception:
                pass
            raise
        self.page.click(selector)

    # ------------------------------------------------------------------
    # Individual tests
    # ------------------------------------------------------------------
    def test_initial_state_has_full_data(self, anim_data: dict) -> None:
        """Right after page load, every animated trace must already hold
        the full data. fig.frames is empty, so nothing should erase or
        partially-reveal anything."""
        for g, a in anim_data.items():
            if a["type"] == "cumulative":
                lens = self.trace_lengths(a["trace_indices"])
                self.check(
                    f"[init] {g}: every trace non-empty",
                    all(L > 0 for L in lens),
                    f"got lengths {lens}",
                )
            elif a["type"] == "heatmap_reveal":
                rows = self.heatmap_filled_rows(a["trace_idx"])
                self.check(
                    f"[init] {g}: heatmap fully populated ({rows} rows)",
                    rows == a["n_frames"],
                    f"expected {a['n_frames']} rows, got {rows}",
                )
            elif a["type"] == "marker":
                lens = self.trace_lengths([a["trace_idx"]])
                self.check(
                    f"[init] {g}: marker has data",
                    lens[0] >= 1,
                    f"got length {lens[0]}",
                )

    def test_play_completes_with_full_data(self, group: str, anim_data: dict) -> None:
        """▶ → wait for the engine to actually report 'ended' → assert the
        panel lands back on the original full data and that the real
        elapsed time sits in the expected window.

        Why wait on the 'ended' state instead of a fixed sleep: the rAF
        engine runs for ~nominal_ms of wall-clock, but the click→observe
        path costs CDP latency and redraws can run a touch late. Polling
        the real completion signal makes the elapsed measurement mean
        "how long the animation took" rather than "how long we slept",
        and bounds it by [nominal-300, nominal+JITTER+RPC_OVERHEAD]."""
        a = anim_data[group]
        speed = a["speed_ms"]
        n = a["n_frames"]
        nominal_ms = speed * n
        budget_ms = nominal_ms + JITTER_MS + RPC_OVERHEAD_MS

        # Capture pre-state
        if a["type"] == "cumulative":
            pre = self.trace_lengths(a["trace_indices"])
        elif a["type"] == "heatmap_reveal":
            pre = self.heatmap_filled_rows(a["trace_idx"])
        else:  # marker
            pre = self.trace_lengths([a["trace_idx"]])

        t0 = time.monotonic()
        self.click(f'.anim-play[data-group="{group}"]')
        reached_end = True
        try:
            self.page.wait_for_function(
                f"window.__animDebug.getState({json.dumps(group)}) === 'ended'",
                timeout=budget_ms,
            )
        except PWTimeout:
            reached_end = False
        elapsed = (time.monotonic() - t0) * 1000

        if a["type"] == "cumulative":
            post = self.trace_lengths(a["trace_indices"])
            self.check(
                f"[play {group}] returns to full data after ~{nominal_ms}ms",
                post == pre and all(L > 0 for L in post),
                f"pre={pre} post={post}",
            )
        elif a["type"] == "heatmap_reveal":
            post = self.heatmap_filled_rows(a["trace_idx"])
            self.check(
                f"[play {group}] heatmap ends fully revealed",
                post == a["n_frames"],
                f"expected {a['n_frames']} got {post}",
            )
        else:
            post = self.trace_lengths([a["trace_idx"]])
            self.check(
                f"[play {group}] marker ends with one point",
                post[0] >= 1,
                f"got {post}",
            )
        self.check(
            f"[play {group}] reached 'ended' state",
            reached_end,
            f"never reached 'ended' within {budget_ms}ms",
        )
        self.check(
            f"[play {group}] elapsed {elapsed:.0f}ms within "
            f"[{nominal_ms - 300},{budget_ms}]ms",
            nominal_ms - 300 <= elapsed <= budget_ms,
            f"actual {elapsed:.0f}ms",
        )

    def test_play_does_not_affect_other_groups(self, group: str, anim_data: dict) -> None:
        """While `group` plays, OTHER groups' trace data must stay full
        the entire time — no shared queue, no cross-bleed."""
        a = anim_data[group]
        others = {og: oa for og, oa in anim_data.items() if og != group}

        # Snapshot all other groups' state.
        def snap_others():
            snap = {}
            for og, oa in others.items():
                if oa["type"] == "cumulative":
                    snap[og] = self.trace_lengths(oa["trace_indices"])
                elif oa["type"] == "heatmap_reveal":
                    snap[og] = self.heatmap_filled_rows(oa["trace_idx"])
                else:
                    snap[og] = self.trace_lengths([oa["trace_idx"]])
            return snap

        pre = snap_others()
        self.click(f'.anim-play[data-group="{group}"]')
        # Sample mid-animation
        self.page.wait_for_timeout(int(a["speed_ms"] * a["n_frames"] * 0.5))
        mid = snap_others()
        # Wait for completion + jitter, sample again
        self.page.wait_for_timeout(int(a["speed_ms"] * a["n_frames"] * 0.5) + JITTER_MS)
        post = snap_others()

        self.check(
            f"[isolation while {group} plays] mid-state untouched",
            mid == pre,
            f"diff: pre={pre} mid={mid}",
        )
        self.check(
            f"[isolation while {group} plays] post-state untouched",
            post == pre,
            f"diff: pre={pre} post={post}",
        )

    def test_pause_freezes_state(self, group: str, anim_data: dict) -> None:
        """▶, let it run briefly, ⏸ — the reveal must freeze partway and
        stay frozen past the full nominal duration.

        The whole play→pause→read dance runs inside ONE page.evaluate so
        no Python<->CDP roundtrip latency can let the (short, ~2.4s)
        animation finish before the pause click lands — the exact failure
        mode the old fixed-sleep version hit on dense/laggy panels, and
        the same in-browser technique the state-machine suite uses."""
        a = anim_data[group]
        if a["type"] != "cumulative":
            # Only cumulative animations have an obvious "partial" length.
            return
        nominal_ms = a["speed_ms"] * a["n_frames"]
        # Pause well under nominal so the partial frame is unambiguous even
        # on heavy panels whose per-frame restyle lags.
        pause_at_ms = min(400, max(150, int(nominal_ms * 0.2)))
        result = self.page.evaluate(f"""(async () => {{
          const g = {json.dumps(group)};
          const idxs = {json.dumps(a['trace_indices'])};
          const gd = document.getElementsByClassName('plotly-graph-div')[0];
          const dbg = window.__animDebug;
          const lens = () => idxs.map(i =>
            (gd.data[i] && gd.data[i].x) ? gd.data[i].x.length : 0);
          const full = lens();   // idle/ended state shows full data
          const playBtn = document.querySelector('.anim-play[data-group="' + g + '"]');
          const pauseBtn = document.querySelector('.anim-pause[data-group="' + g + '"]');
          playBtn.click();
          await new Promise(r => setTimeout(r, {pause_at_ms}));
          pauseBtn.click();
          await new Promise(r => setTimeout(r, 30));
          const atPause = lens();
          const stateAtPause = dbg.getState(g);
          // If pause truly froze it, waiting past the full nominal
          // duration must NOT advance the reveal.
          await new Promise(r => setTimeout(r, {nominal_ms} + 1000));
          const afterWait = lens();
          const stateAfter = dbg.getState(g);
          return {{ full, atPause, stateAtPause, afterWait, stateAfter }};
        }})()""")

        full = result["full"]
        at_pause = result["atPause"]
        after_wait = result["afterWait"]
        self.check(
            f"[pause {group}] caught mid-flight (state=paused)",
            result["stateAtPause"] == "paused",
            f"state was {result['stateAtPause']!r} (atPause={at_pause} full={full})",
        )
        self.check(
            f"[pause {group}] at least one trace stays partial",
            any(p < f for p, f in zip(at_pause, full)),
            f"atPause={at_pause} full={full}",
        )
        self.check(
            f"[pause {group}] stays frozen past nominal duration",
            after_wait == at_pause and result["stateAfter"] == "paused",
            f"atPause={at_pause} afterWait={after_wait} stateAfter={result['stateAfter']!r}",
        )

    def test_play_resumes_full_after_pause_then_play(self, group: str, anim_data: dict) -> None:
        """▶ → ⏸ mid-anim → ▶ again → should complete and return to full."""
        a = anim_data[group]
        if a["type"] != "cumulative":
            return
        nominal_ms = a["speed_ms"] * a["n_frames"]
        self.click(f'.anim-play[data-group="{group}"]')
        self.page.wait_for_timeout(int(nominal_ms * 0.3))
        self.click(f'.anim-pause[data-group="{group}"]')
        self.page.wait_for_timeout(150)
        self.click(f'.anim-play[data-group="{group}"]')
        self.page.wait_for_timeout(nominal_ms + JITTER_MS)
        post = self.trace_lengths(a["trace_indices"])
        self.check(
            f"[replay {group}] re-Play after Pause restores full data",
            all(L > 0 for L in post),
            f"got {post}",
        )

    def test_time_label_advances(self, group: str, anim_data: dict) -> None:
        """The time label next to ▶ must move through the labels list."""
        a = anim_data[group]
        sel_js = f"document.querySelector('.anim-time[data-group=\"{group}\"]').textContent"
        # Click first — JS sets the label to labels[0] synchronously, so
        # we read AFTER click to capture the start-of-animation state.
        self.click(f'.anim-play[data-group="{group}"]')
        first = self.js(sel_js)
        # Sample roughly 70% through to give a stable advanced reading.
        self.page.wait_for_timeout(int(a["speed_ms"] * a["n_frames"] * 0.7))
        mid = self.js(sel_js)
        # Drain to the end before continuing.
        self.page.wait_for_timeout(a["speed_ms"] * a["n_frames"] + JITTER_MS)
        # rAF + Playwright RPC roundtrip can land 1-3 frames in. For tiny
        # animations like hm (6 frames) that's already 30%+. Accept the
        # first read in the first third of the timeline.
        early_window = a["labels"][: max(2, len(a["labels"]) // 3)]
        self.check(
            f"[label {group}] starts within early-window labels",
            first in early_window,
            f"first={first!r} early_window head={early_window[:3]}",
        )
        self.check(
            f"[label {group}] advances during play",
            mid != first or a["n_frames"] <= 1,
            f"first={first!r} mid={mid!r} (n_frames={a['n_frames']})",
        )


# ----------------------------------------------------------------------
# Session helpers
# ----------------------------------------------------------------------
def wait_ready(page: Page) -> None:
    """Block until Plotly has rendered traces and setup() has run."""
    page.wait_for_function(
        "document.getElementsByClassName('plotly-graph-div')[0]"
        " && document.getElementsByClassName('plotly-graph-div')[0].data"
        " && document.getElementsByClassName('plotly-graph-div')[0].data.length > 0",
        timeout=15000,
    )
    # Settle a bit so handlers attach.
    page.wait_for_timeout(800)


def stash_full_lengths(page: Page, anim_data: dict) -> None:
    """Save the original (full) trace lengths to window so any check that
    wants a reference for 'expected full state' can read them. (The pause
    suite computes its own reference in-browser and doesn't need this, but
    older/auxiliary checks may.)"""
    for g, a in anim_data.items():
        if a["type"] == "cumulative":
            page.evaluate(
                f"window._origLens_{g} = "
                f"({json.dumps(a['trace_indices'])}).map(i => "
                f"document.getElementsByClassName('plotly-graph-div')[0].data[i].x.length)"
            )


def assert_expected_speeds(anim_data: dict) -> None:
    """Generator-vs-test alignment guard (see EXPECTED_GROUPS)."""
    for g, expected_speed in EXPECTED_GROUPS.items():
        if g not in anim_data:
            print(f"WARNING: {g} not present in ANIM_DATA (data may be sparse)")
            continue
        assert anim_data[g]["speed_ms"] == expected_speed, (
            f"speed mismatch for {g}: got {anim_data[g]['speed_ms']}, "
            f"expected {expected_speed}")


def run_suite(suite: Callable[[TestRunner, dict], None], *, reload_first: bool = False) -> int:
    """Standalone entry point used by each e2e_*.py file's __main__.

    Launches its own Chromium, opens the dashboard, runs `suite(runner,
    anim_data)`, and returns a process exit code (0 ok, 1 failures, 2/3
    setup errors). `reload_first` re-navigates to a clean page before the
    suite runs (suites that depend on pristine state set it True)."""
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

        if reload_first:
            page.reload()
            wait_ready(page)
            stash_full_lengths(page, anim_data)

        try:
            suite(runner, anim_data)
        finally:
            code = summarize(runner)
            browser.close()
        return code


def summarize(runner: TestRunner) -> int:
    print("\n" + "=" * 70)
    print(f"PASSED: {len(runner.passed)}    FAILED: {len(runner.failures)}")
    if runner.failures:
        print("\nFailures:")
        for f in runner.failures:
            print(f"  - {f}")
        return 1
    print("All checks in this suite passed.")
    return 0
