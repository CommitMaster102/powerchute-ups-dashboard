# stateOfUPS

PowerChute Serial Shutdown (PCSS) log analyzer for the APC BX2000M-LM.

## Why this exists

PCSS logs are written in three places under `C:\Program Files\APC\PowerChute Serial Shutdown\agent\`:

- `DataLog` — TSV with sampled measurements (line voltage, battery V, load %, battery %, etc.)
- `EventLog` — binary, Java-serialized
- `energylog/*.log` — monthly energy / cost / CO2 rollups

The PCSS GUI shows current values but does not show history over time, and has no way to report how fast the logs grow on disk. This analyzer provides two things:

1. **UPS state over time** — a dark, card-based dashboard (KPI header row, health pill, and 12 chart cards grouped into Power Quality / Battery Health / Energy & Cost / Logs & Storage / Reference sections). Charts are self-contained inline SVG rendered by `pcss/charts.js` — no chart library, no network access — with hover tooltips, a shared zoomable time window across the sample panels, and per-card PNG/CSV export.
2. **Log growth rate** — each run appends a snapshot to `output/size_history.csv`, then plots the growth curve and projects it forward.

The second is useful because PCSS exposes data-log retention and sample-interval settings; the size history provides measured data for choosing those values instead of estimating them.

## How to run

Double-click `run_analyzer.bat`, or from a terminal:

```
.venv\Scripts\python.exe analyze_ups.py
```

It will:
1. Read all three PCSS logs.
2. Append a row to `output/size_history.csv` with the current file sizes.
3. Print a console summary (sizes, sample counts, projections, growth rates).
4. Write `output/dashboard.html` and open it in your browser.

Each run adds a data point to the size-history curve, which improves the growth-rate projection over time.

## First-time setup

```
python -m venv .venv
.venv\Scripts\pip install -e .[dev]                    # runtime + test/lint tooling
.venv\Scripts\python -m playwright install chromium    # for the E2E tests
```
(`pip install -r requirements.txt` installs runtime deps only.)

## Command-line options

`analyze_ups.py` runs with no arguments (console summary + dashboard, opens browser). Flags:

```
-o, --output PATH     dashboard HTML path (default output/dashboard.html)
--no-browser          don't open a browser
--since / --until     YYYY-MM-DD — only analyze samples in this date range
-q, --quiet           suppress the console summary (still writes the dashboard)
--no-snapshot         don't append to size_history.csv
--config PATH         config.toml path (default: ./config.toml if present)
--agent-dir PATH      override the PCSS agent directory
--json PATH           also write a machine-readable summary as JSON
-v, --verbose         print the resolved config + agent dir
--version
```

## Configuration

Optional `config.toml` (auto-loaded from the repo root, or `--config PATH`) overrides the built-in defaults — copy `config.example.toml` to start. It holds the PCSS agent path, the Coopesantos/PCSS tariff rates (update when the quarterly rate changes), the CO₂ factor, voltage/load thresholds, the KPI status-pill cut points (battery charge and estimated runtime), the runtime curve, the dashboard theme and UPS model name (`[dashboard] theme = "dark" | "light"`, `model`), and an opt-in `[alerts]` switch.

## Scheduled daily run (Windows)

`register_scheduled_task.ps1` creates a per-user Windows Task Scheduler job that runs the analyzer once a day. It does not need admin rights.

```
powershell -ExecutionPolicy Bypass -File register_scheduled_task.ps1            # default: 09:00
powershell -ExecutionPolicy Bypass -File register_scheduled_task.ps1 -RunTime 22:30
powershell -ExecutionPolicy Bypass -File register_scheduled_task.ps1 -Force     # replace an existing task
```

The task runs `scheduled_run.ps1`, which runs `analyze_ups.py --no-browser --quiet`. It does not call `run_analyzer.bat` because that file ends in `pause` and opens a browser, which would hang an unattended task. `scheduled_run.ps1` has three guards: a single mutex so two scheduled copies never overlap, a check that skips if any analyzer run is already in progress, and a once-a-day marker (`output/last_scheduled_run.txt`) so it runs at most once per day. The marker is only written on success, so a failed run retries on the next trigger. All output is appended to `output/scheduled_run.log`.

Remove the task with:

```
Unregister-ScheduledTask -TaskName 'stateOfUPS Daily Analyzer' -Confirm:$false
```

## Tests

Tests are **pytest** under `tests/` (math + tray + payload-contract unit tests, plus browser-driven E2E of the chart engine: render, tooltips and pinning, zoom/pan/wheel, the synced time window and presets, PNG/CSV export, the lightbox, the load reveal, themes, and offline-ness). The E2E fixture is hermetic — it synthesizes data and builds a temp dashboard, so no real PCSS logs are needed.

```
# Fast unit tests (no browser) — the everyday loop, ~2 s:
.venv\Scripts\python.exe -m pytest tests -m "not e2e"

# Whole suite in parallel — unit + all browser suites, well under 32 s:
.venv\Scripts\python.exe -m pytest tests -n auto --dist worksteal --reruns 2 --reruns-delay 1

# Browser E2E only:
.venv\Scripts\python.exe -m pytest tests -m e2e -n auto --dist worksteal

# A single suite / one test / one panel:
.venv\Scripts\python.exe -m pytest tests\e2e_zoom.py
.venv\Scripts\python.exe -m pytest "tests\e2e_render.py::test_panel_renders_svg[lv]"
.venv\Scripts\python.exe -m pytest tests\test_math.py -k tiered
```

**Speed:** the render/tooltip/sync suites are parametrized **per chart panel** (so `pytest-xdist`'s `-n auto` spreads the short browser checks across cores), and every E2E test resets the page in place between tests (`__chartsDebug.resetAll()` — full time window, nothing pinned or hidden, no full reload) so it is order-independent under parallel workers and stays fast even on low-core CI. `--reruns` (pytest-rerunfailures) absorbs the rare browser-timing flake. On a many-core machine the full suite finishes in seconds; the unit-only run is ~2 s.

**CI** (`.github/workflows/ci.yml`, Windows) always runs lint + types + the unit suite on a code change (docs-only changes skip even that). The slow browser suite is **opt-in**, because it's expensive: it runs only when a pull request carries the **`e2e`** label (add the label to trigger a run) or when the workflow is dispatched manually (Actions → Run workflow). Ordinary pushes don't run it. The lint/unit job installs only `.[lint,test]`, so it never downloads Playwright.

Playwright is intentionally *not* in `requirements.txt` (it's in the `dev` extra, with `pytest-xdist` and `pytest-rerunfailures`). Set `STATEOFUPS_E2E_REAL=1` to run E2E against the generated `output/dashboard.html` (which you must have produced locally — `output/` is gitignored) instead of synthetic data.

## Development (lint + types)

The project is kept **ruff-clean and mypy-clean**. Both are installed by the `dev` extra and configured in `pyproject.toml` (`[tool.ruff]` — pycodestyle/pyflakes/isort/pyupgrade/bugbear/simplify; `[tool.mypy]`). Run before committing:

```
.venv\Scripts\python.exe -m ruff check .              # lint (add --fix to auto-fix)
.venv\Scripts\python.exe -m mypy pcss analyze_ups.py tray_status.py
```

`ruff format` is available too, but the codebase is hand-formatted — run lint, not the formatter, unless you intend a reformat.

## Files

| File | What it is |
|---|---|
| `analyze_ups.py` | CLI orchestrator. Loads logs, computes stats, builds the dashboard. |
| `pcss/` | The package: `config`, `common`, `loaders`, `stats`, `dashboard` (+ `charts.js`, the SVG chart engine). |
| `tray_status.py` | System-tray battery icon; reads the PCSS web UI. |
| `config.example.toml` | Template config; copy to `config.toml`. |
| `run_analyzer.bat` / `run_tray.bat` | Double-click launchers. |
| `register_scheduled_task.ps1` / `scheduled_run.ps1` | Set up and run a guarded daily analyzer task (Windows Task Scheduler). |
| `pyproject.toml` / `requirements.txt` | Packaging + deps (ruff/mypy/pytest config in pyproject). |
| `ROADMAP.md` | Candidate future features, each with the technical challenges it has to solve. |
| `tests/` | pytest: `test_math.py`, `test_pipeline.py`, `test_tray.py`, `test_chart_payload.py`, `conftest.py` (hermetic fixture), `harness.py`, one `e2e_*.py` per browser suite (render, tooltip, zoom, sync, export, lightbox, reveal, theme, offline). |
| `output/dashboard.html` | Latest dashboard. Overwritten each run. |
| `output/size_history.csv` | Append-only growth log. Do not delete; more snapshots improve the projection. |

## Dashboard layout

A header (UPS model, sample counts, data-staleness badge, health pill) and a five-card KPI row (Line Voltage, UPS Load, Battery Charge, Est. Runtime, Power Draw — each with a 3-day sparkline and an OK/WARN/ALERT pill driven by the configured thresholds) sit above five titled sections on a 12-column card grid:

| Section | Cards |
|---|---|
| Power Quality | Line Voltage (anomalies marked red, normal envelope shaded, DataLog gaps marked on the axis) · UPS Load (80% threshold line) · Power Draw (5-min energylog) · Hourly Power Map (hour×date heatmap) |
| Battery Health | Battery Voltage (raw + 8h rolling mean + degradation trend line) · Battery Charge · Estimated Runtime (curve + current operating point as a star) |
| Energy & Cost | Cumulative kWh + cost (dual y-axis) · Daily kWh bars |
| Logs & Storage | Log-size growth (multi-line) · Projected DataLog size (1 yr) · Sample Cadence (interval distribution) |
| Reference | Per-metric statistics table (min/mean/median/p95/max) · Latest readings + energy/files/growth/anomaly summary |

**Interactions** (`pcss/charts.js`, no dependencies): hover any chart for a crosshair tooltip and click to pin it; the crosshair mirrors the hovered timestamp across the six sample panels. Zoom and pan are strictly per chart — on the hovered chart, drag always selects a zoom range, `Shift`+drag pans, mouse-wheel zooms around the cursor, double-click (or the `reset` pill) restores; arrow keys pan, `+`/`-` zoom, `0` resets. The `All · 30 d · 7 d · 24 h` pills next to the Power Quality header are the one deliberate global control: they set every time chart's window, each anchored to its own newest sample. Each card's hover menu exports PNG or CSV and expands the chart into a lightbox. Charts draw in once while the page loads (skipped under `prefers-reduced-motion` or `?noanim=1`).

## What gets analyzed

**Energy & cost (from `energylog/*.log`):**
- Total kWh over recorded period
- Cost calculated **two ways**: PCSS flat-rate (what PCSS reports) and Coopesantos T-RE Residencial tiered (₡78.17 for the first 200 kWh, ₡126.51 above); the tiered figure matches the actual electricity bill
- CO₂ emissions at 0.098 kg/kWh (CR grid intensity, Low Carbon Power 2024)
- Per-month breakdown
- Daily and hourly profiles (the heatmap shows power draw by hour of day)

**Anomaly detection:**
- Line voltage outside the 114-126V envelope (NEC ±5% of nominal 120V)
- Sustained high-load episodes (≥80% for ≥10 min) from energylog
- DataLog timestamp gaps > 2× expected interval (PCSS down / PC off)

**Cross-validation:**
- DataLog `UPS Load` (20-min) vs energylog `relativeLoad` (5-min). The mean absolute error compares the two sources; a large divergence indicates a data problem.

**Runtime estimate:**
- Empirical runtime curve (defaults in `pcss/config.py`, overridable via `config.toml` `[runtime_curve]`): Idle 30 min @ 150W to Peak 3.5 min @ 600W. Plots the full curve with the current power reading marked.

**Statistics summary:**
- Per-column min / mean / median / p95 / max for every numeric DataLog field.

## Notes

- **DataLog sample interval is set in PCSS, not here.** Default is 20 min. The Sample Cadence card shows the interval PCSS actually uses.
- **The dashboard is fully offline.** All charts are inline SVG generated by the embedded `pcss/charts.js`; the page loads no external scripts, styles, or fonts (the E2E suite asserts zero network requests).
- **Timestamps in the chart payload are epoch-ms with the log's wall-clock time encoded as if UTC**, and the chart engine formats labels with UTC getters only — so labels always match the log no matter the viewer's browser timezone. `tests/test_chart_payload.py` pins this contract.
- **EventLog is binary.** Only its size is read; contents are not parsed. Events are visible in the PCSS UI.
- **`size_history.csv` is the long-running record.** It is retained across PCSS log rotations (default 1-month retention), so trends remain visible after PCSS truncates its own logs.
- **Empirical growth rate (measured 2026-04-28 → 2026-05-01):** ~7.9 KB/day across all three logs combined → ~2.9 MB/year. PCSS defaults (1-month retention, 20-min interval) are appropriate for this workload; no need to tune them.
- **DataLog uses Spanish locale numbers** (`1.234,56` → 1234.56), parsed by `read_csv(decimal=",")` in `pcss/loaders.py`.
- **energylog uses dot-decimal** numbers and timestamps as **seconds since 2010-01-01 LOCAL** (not UTC, not Unix epoch). Verified empirically by aligning the first energylog timestamp with the first DataLog timestamp.
- **Real wattage is `null`** in energylog — the BX2000M has no wattmeter. PCSS calculates power as `relativeLoad × calculatedMaxLoad / 100`, where `calculatedMaxLoad` is 1400W (declared in the energylog header).
- **PCSS reports a single-rate cost.** PCSS supports only one rate, but Coopesantos T-RE is tiered, so the PCSS figure differs from the bill. The dashboard reports both. The "Cost (Coopesantos tiered)" row in the summary table matches the actual bill.
- **Tariff constants** default in `pcss/config.py` and are overridable via `config.toml` `[tariff]` (`coopesantos_low/high`, `tier_limit_kwh`, `pcss_flat`). When the quarterly Coopesantos rate changes, edit `config.toml` — no code change.
- **High-load episode duration** counts each sample as one sampling interval, so a k-sample run is ~k×interval (not the (k−1)×interval span between first/last timestamps); this can surface short episodes the old span-based measure missed.

## Paths assumed

The PCSS agent directory defaults in `pcss/config.py`:

```
PCSS_AGENT = C:\Program Files\APC\PowerChute Serial Shutdown\agent
```

This default is the standard Windows install path (PCSS is a Windows product). If PCSS is installed elsewhere — or you are running the analyzer on Linux/macOS against a copy of exported logs — set `[paths] pcss_agent` in `config.toml` or pass `--agent-dir PATH`. The analyzer itself (`analyze_ups.py` + the `pcss` package) is pure Python and runs on any OS; only `tray_status.py` is Windows-only (it uses the Windows Credential Manager and a tray backend). If the agent directory does not exist, the analyzer prints how to point it at the right path instead of failing.
