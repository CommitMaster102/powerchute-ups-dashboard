# stateOfUPS

PowerChute Serial Shutdown (PCSS) log analyzer for the APC BX2000M-LM.

## Why this exists

PCSS logs are written in three places under `C:\Program Files\APC\PowerChute Serial Shutdown\agent\`:

- `DataLog` — TSV with sampled measurements (line voltage, battery V, load %, battery %, etc.)
- `EventLog` — binary, Java-serialized
- `energylog/*.log` — monthly energy / cost / CO2 rollups

The PCSS GUI shows current values but does not give a long-horizon view, and there is no built-in way to know how fast logs grow on disk. This analyzer answers two questions:

1. **What does the UPS look like over time?** — Plotly dashboard with voltage / load / battery time series, sample-interval histogram, and latest-readings table.
2. **How fast do PCSS logs grow?** — every run appends a snapshot to `output/size_history.csv`, then plots the growth curve and projects forward.

The second question matters because PCSS exposes data-log retention and sample-interval settings; without measured growth data, picking values is guesswork. This script gives the empirical answer.

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

The more often you run it, the more accurate the growth-rate plot becomes — every run is a new data point on the size-history curve.

## First-time setup

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Tests

The dashboard's interactive replay controls (per-panel ▶/⏸, the cumulative-reveal animation, the play/pause/resume state machine) are covered by tests under `tests/`.

```
# Regenerate the dashboard the tests read, then:
.venv\Scripts\python.exe analyze_ups.py

# All browser-driven E2E suites in one Chromium session:
.venv\Scripts\python.exe tests\run_e2e.py

# Or a single focused suite (the full run is slow):
.venv\Scripts\python.exe tests\e2e_pause_freeze.py

# Pure unit test (no browser):
.venv\Scripts\python.exe tests\test_animation_slicing.py
```

The E2E suites need **Playwright**, which is intentionally *not* in `requirements.txt` (test-only):

```
.venv\Scripts\pip install playwright
.venv\Scripts\python -m playwright install chromium
```

## Files

| File | What it is |
|---|---|
| `analyze_ups.py` | Main script. Loads logs, computes stats, builds dashboard. |
| `run_analyzer.bat` | Double-click launcher (activates venv, runs script). |
| `requirements.txt` | pandas, numpy, plotly. |
| `tests/` | `harness.py` (shared helpers) + `run_e2e.py` (one-browser runner) + one `e2e_*.py` per suite + `test_animation_slicing.py`. |
| `output/dashboard.html` | Latest dashboard. Overwritten each run. |
| `output/size_history.csv` | Append-only growth log. **Don't delete** — the longer it runs, the better the projection. |

## Dashboard layout (7 rows × 2 cols = 14 panels)

| Row | Left | Right |
|---|---|---|
| 1 | Line Voltage (V) — anomalies marked red, normal envelope shaded | Battery Voltage (V) |
| 2 | UPS Load (%) — 80% threshold line | Battery Capacity (%) |
| 3 | Power consumption (W) from energylog (5-min granularity) | Hour-of-day power heatmap (W per hour×date) |
| 4 | Cumulative kWh + cost (dual y-axis) | Daily kWh bar chart |
| 5 | Estimated runtime curve (W → min) + current point as red star | Sample-interval histogram |
| 6 | Log-size growth (multi-line) | Projected DataLog size (1 yr) |
| 7 | Per-metric statistics table (min/mean/median/p95/max) | Latest readings + cost summary + anomaly counts |

## What gets analyzed

**Energy & cost (from `energylog/*.log`):**
- Total kWh over recorded period
- Cost calculated **two ways**: PCSS flat-rate (what PCSS itself reports) vs Coopesantos T-RE Residencial tiered (₡78.17 first 200 kWh, ₡126.51 above) — the tiered figure is what your bill actually uses
- CO₂ emissions at 0.098 kg/kWh (CR grid intensity, Low Carbon Power 2024)
- Per-month breakdown
- Daily and hourly profiles (heatmap shows when in the day you draw power)

**Anomaly detection:**
- Line voltage outside the 114-126V envelope (NEC ±5% of nominal 120V)
- Sustained high-load episodes (≥80% for ≥10 min) from energylog
- DataLog timestamp gaps > 2× expected interval (PCSS down / PC off)

**Cross-validation:**
- DataLog `UPS Load` (20-min) vs energylog `relativeLoad` (5-min) — MAE comparison confirms both sources agree. If they ever diverge significantly, something's broken.

**Runtime estimate:**
- Empirical runtime curve from `ups_profile` memory (Idle 30min @ 150W → Peak 3.5min @ 600W). Plots full curve with current power reading marked.

**Statistics summary:**
- Per-column min / mean / median / p95 / max for every numeric DataLog field.

## Things worth knowing

- **DataLog sample interval is set in PCSS, not here.** Default is 20 min. The histogram in row 3 confirms whatever PCSS is actually doing.
- **EventLog is binary.** Only its size is read — contents are not parsed. This is intentional; events are visible in the PCSS UI.
- **`size_history.csv` is the long-running record.** It survives across PCSS log rotations (defaults to 1-month retention) so you can still see trends after PCSS truncates its own logs.
- **Empirical growth rate (measured 2026-04-28 → 2026-05-01):** ~7.9 KB/day across all three logs combined → ~2.9 MB/year. PCSS defaults (1-month retention, 20-min interval) are appropriate for this workload; no need to tune them.
- **DataLog uses Spanish locale numbers** (`1.234,56` → 1234.56). `parse_es_number()` handles this.
- **energylog uses dot-decimal** numbers and timestamps as **seconds since 2010-01-01 LOCAL** (not UTC, not Unix epoch). Verified empirically by aligning the first energylog timestamp with the first DataLog timestamp.
- **Real wattage is `null`** in energylog — the BX2000M has no wattmeter. PCSS calculates power as `relativeLoad × calculatedMaxLoad / 100`, where `calculatedMaxLoad` is 1400W (declared in the energylog header).
- **PCSS cost is wrong by design.** PCSS supports only a single rate, but Coopesantos T-RE is tiered. The dashboard reports both so the discrepancy is visible. The "Cost (Coopesantos tiered)" row in the summary table is the one that matches an actual bill.
- **Tariff constants are hardcoded** in `analyze_ups.py` under the `CONFIG` section. When the quarterly Coopesantos rate changes, update `COOPESANTOS_LOW_RATE`, `COOPESANTOS_HIGH_RATE`, `PCSS_FLAT_RATE`. (The scheduled `trig_01PgYk7Fb6HXqGjcJ5CiAbC1` agent will email when this needs to happen.)

## Paths assumed

Hardcoded at the top of `analyze_ups.py`:

```
PCSS_AGENT = C:\Program Files\APC\PowerChute Serial Shutdown\agent
```

If PCSS is reinstalled elsewhere, update those constants.
