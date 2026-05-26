"""E2E suite: pause/resume state machine (per user spec).

idle -> playing -> paused -> playing -> ended, with the play/pause button
enabled/disabled states correct at each step, and no auto-restart at the
end. The whole play→pause→resume dance runs in ONE page.evaluate so the
~100-300ms Python<->CDP hop per click can't push the short animation to
completion before we can pause it.

Standalone:  .venv\\Scripts\\python.exe tests\\e2e_state_machine.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import run_suite


def run(runner, anim_data):
    print("\n=== pause/resume state machine ===")
    page = runner.page
    g = "lv"
    a = anim_data[g]
    play_sel = f'.anim-play[data-group="{g}"]'
    pause_sel = f'.anim-pause[data-group="{g}"]'

    def state(): return page.evaluate(f"window.__animDebug.getState('{g}')")
    def play_disabled(): return page.evaluate(f"document.querySelector('{play_sel}').disabled")
    def pause_disabled(): return page.evaluate(f"document.querySelector('{pause_sel}').disabled")

    speed_val = page.evaluate(f"window.__animDebug.getSpeed('{g}')")
    print(f"  diag: SPEED[{g}]={speed_val}  speed_ms={a['speed_ms']}  "
          f"n_frames={a['n_frames']}  totalMs={a['speed_ms']*a['n_frames']/speed_val}")
    runner.check("[state] starts idle", state() == "idle", state())
    runner.check("[btn] play enabled at idle", not play_disabled(), "play disabled at idle")
    runner.check("[btn] pause disabled at idle", pause_disabled(), "pause was enabled at idle")

    # Drive the whole play→pause→snapshot dance inside ONE page.evaluate so
    # we don't pay 6x RPC roundtrip latency between clicks (each Python<->CDP
    # hop is ~100-300ms, enough to push the animation almost to completion
    # before we can pause).
    result = page.evaluate(f"""(async () => {{
      const g = '{g}';
      const playBtn = document.querySelector('.anim-play[data-group="' + g + '"]');
      const pauseBtn = document.querySelector('.anim-pause[data-group="' + g + '"]');
      const dbg = window.__animDebug;
      const log = {{}};
      log.pre = {{ state: dbg.getState(g), playDis: playBtn.disabled, pauseDis: pauseBtn.disabled }};
      playBtn.click();
      await new Promise(r => setTimeout(r, 400));
      log.during = {{ state: dbg.getState(g), savedT: dbg.getSavedT(g),
                      playDis: playBtn.disabled, pauseDis: pauseBtn.disabled }};
      pauseBtn.click();
      await new Promise(r => setTimeout(r, 30));
      log.afterPause = {{ state: dbg.getState(g), savedT: dbg.getSavedT(g),
                          playDis: playBtn.disabled, pauseDis: pauseBtn.disabled }};
      pauseBtn.click();   // resume
      await new Promise(r => setTimeout(r, 30));
      log.afterResume = {{ state: dbg.getState(g), savedT: dbg.getSavedT(g),
                            playDis: playBtn.disabled, pauseDis: pauseBtn.disabled }};
      // Wait for the whole thing to finish naturally
      await new Promise(r => setTimeout(r, {a['speed_ms']*a['n_frames']} + 1000));
      log.afterEnd = {{ state: dbg.getState(g), savedT: dbg.getSavedT(g),
                         playDis: playBtn.disabled, pauseDis: pauseBtn.disabled }};
      return log;
    }})()""")

    runner.check("[state] starts idle (pre-click)", result["pre"]["state"] == "idle",
                 str(result["pre"]))
    runner.check("[btn] play enabled at idle", not result["pre"]["playDis"], "")
    runner.check("[btn] pause disabled at idle", result["pre"]["pauseDis"], "")
    runner.check("[state] playing after click", result["during"]["state"] == "playing",
                 str(result["during"]))
    runner.check("[btn] pause enabled while playing", not result["during"]["pauseDis"], "")
    runner.check("[state] paused after pause click",
                 result["afterPause"]["state"] == "paused", str(result["afterPause"]))
    runner.check("[btn] play enabled when paused",
                 not result["afterPause"]["playDis"], "")
    runner.check("[state] resume -> playing",
                 result["afterResume"]["state"] == "playing", str(result["afterResume"]))
    runner.check("[state] reaches ended after resume",
                 result["afterEnd"]["state"] == "ended", str(result["afterEnd"]))
    runner.check("[btn] play re-enabled at end",
                 not result["afterEnd"]["playDis"], "")
    runner.check("[btn] pause disabled at end (no auto-restart)",
                 result["afterEnd"]["pauseDis"], "")


if __name__ == "__main__":
    sys.exit(run_suite(run, reload_first=True))
