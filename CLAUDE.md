# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PowerChute Serial Shutdown (PCSS) log analyzer + live status tray for the APC BX2000M-LM UPS on Windows. PCSS writes logs under `C:\Program Files\APC\PowerChute Serial Shutdown\agent\`; this repo turns them into a dashboard and a system-tray battery icon. Read `README.md` for the domain rationale (why both PCSS-flat and Coopesantos-tiered cost are reported, the empirical growth-rate question, etc.).

There are **two independent entry points** with no shared module — `analyze_ups.py` (batch → HTML dashboard) and `tray_status.py` (long-running tray icon). They only share the `output/` directory.

## Commands

First-time setup (no venv is committed):
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

| Task | Command |
|---|---|
| Run the analyzer (console summary + `output/dashboard.html`, opens browser) | `.venv\Scripts\python.exe analyze_ups.py` — or double-click `run_analyzer.bat` |
| Run the tray icon (silent, no console) | `run_tray.bat` (launches `pythonw.exe tray_status.py`) |
| All E2E suites, one browser session | `.venv\Scripts\python.exe tests\run_e2e.py` |
| A single E2E suite (own browser) | `.venv\Scripts\python.exe tests\e2e_pause_freeze.py` (any `tests\e2e_*.py`) |
| Animation slicing unit test (no browser) | `.venv\Scripts\python.exe tests\test_animation_slicing.py` |

Tests live under `tests/` and are plain scripts with a `main()`/`run()` returning an exit code — **not** pytest. `tests/run_e2e.py` opens Chromium once and runs every `e2e_*` suite (the fast path; aggregate pass/fail). Each `tests/e2e_*.py` is also runnable on its own (it launches its own browser via `harness.run_suite`) — useful because the full E2E run is slow. Shared assertions/helpers live in `tests/harness.py`.

**E2E gotchas:** the suites drive real Chromium via **Playwright, which is installed in the venv but is NOT in `requirements.txt`** (`pip install playwright && python -m playwright install chromium`). They read `output/dashboard.html`, so **run `analyze_ups.py` first** to generate a current dashboard, or they assert against stale/missing output. `tests/harness.py:EXPECTED_GROUPS` must stay in sync with the `_register_animation()` speeds in `analyze_ups.py` — the session setup asserts on mismatch. Timing tolerances live in `harness.py` (`JITTER_MS`, `RPC_OVERHEAD_MS`); the play-completion check waits on the engine's real `'ended'` state rather than a fixed sleep, and the pause/state-machine suites drive play→pause inside a single `page.evaluate` so CDP latency can't race a short (~2.4 s) animation to completion.

## Architecture

### `analyze_ups.py` — the analyzer (single file, ~1500 lines)

Pipeline in `main()`: load three logs → compute stats/anomalies/energy/cross-validation → `record_size_snapshot()` (append to `output/size_history.csv`) → `build_dashboard()` → write+open HTML.

Three data sources, **three different formats** — the parsing quirks are the heart of this code and easy to break:
- **DataLog** (TSV, ~20-min samples): Spanish-locale numbers (`1.234,56` → `1234.56`) handled by `parse_es_number()`.
- **energylog/*.log** (`;`-delimited, 5-min): dot-decimal numbers; `real_w` is always `null` (no wattmeter — power is computed as `relativeLoad × calculatedMaxLoad/100`, max load 1400W from the file header). Timestamps are **seconds since 2010-01-01 LOCAL time** (not Unix, not UTC) — `ts_2010_to_dt()`. This was verified empirically by aligning the first energylog and DataLog timestamps.
- **EventLog** (binary Java-serialized): only its file size is read; contents are never parsed.

`size_history.csv` is append-only and intentionally outlives PCSS's own log rotation — never truncate it; it's the long-horizon growth record.

### Dashboard animation system (the subtle part)

The dashboard is a 7×2 Plotly subplot grid. Per-panel Play/Pause "cumulative reveal" animations use a deliberate non-standard contract:

- **`fig.frames` is kept EMPTY.** Python never builds `go.Frame` objects. `_replay_metadata()` / `_runtime_metadata()` / `_heatmap_metadata()` emit pure metadata dicts (trace indices, `cutoffs_ms`, labels). `_register_animation()` collects them.
- The initial render therefore shows full data immediately, with zero animation interference. On a Play click, hand-written JS (injected by `_inject_controls_into_html()` / `_build_custom_controls_html()`, appended before `</body>`) reconstructs frames client-side by slicing the live trace data against `cutoffs_ms`.
- **Timezone trap:** Plotly serializes datetimes as ISO strings *without* a timezone; JS `Date.parse` reads those as LOCAL while Python cutoffs (`pd.Timestamp.value`) are UTC. In a non-UTC locale this made every panel render empty. The fix forces UTC parsing in JS. `tests/test_animation_slicing.py` exists solely to guard this — it simulates both interpretations and asserts frame 0 is non-empty and the last frame contains every point. If you touch the slicing/cutoff logic, this test is your tripwire. The browser-driven counterparts of this contract (no-autoplay, full-data-on-init, axis stays in-range, play/pause/resume state machine) live in `tests/e2e_*.py`.

### `tray_status.py` — the live tray icon

Long-running pystray loop that scrapes the PCSS web UI (`https://localhost:6547`, self-signed cert → TLS verification disabled). `PCSSClient` handles the Java form-token login (`j_security_check`, Spanish button text), auto re-login on `SessionExpired`, and distinguishes `UserAlreadyConnected` (the human opened the PCSS UI in a browser, holding the single allowed session) so it backs off instead of hammering. `parse_status()` + BeautifulSoup extract battery % from Spanish-locale HTML; `render_icon()` draws a battery silhouette + number colored by charge level. Single-instance guard via `acquire_single_instance()`. File-only logging (`output/tray_status.log`) since `pythonw` has no console.

`credentials.txt` (gitignore-worthy, plaintext, localhost-only) is auto-created from a template on first run; the user fills in username/password.

## Hardcoded config — where to edit

All in the `CONFIG` block at the top of `analyze_ups.py`:
- `PCSS_AGENT` path — change if PCSS is reinstalled elsewhere.
- Tariff constants `COOPESANTOS_LOW_RATE` / `COOPESANTOS_HIGH_RATE` / `PCSS_FLAT_RATE` (CRC/kWh) — update when the quarterly Coopesantos rate changes. `COOPESANTOS_TIER_LIMIT_KWH=200` splits the tiers.
- `CO2_KG_PER_KWH`, voltage envelope (`VOLTAGE_NORMAL_LOW/HIGH`, NEC ±5% of 120V), `HIGH_LOAD_PCT`, `DATALOG_EXPECTED_INTERVAL_MIN`, and the empirical `RUNTIME_CURVE_*` arrays.

`tray_status.py` paths/URL/poll-interval live in `credentials.txt`, not in code.
