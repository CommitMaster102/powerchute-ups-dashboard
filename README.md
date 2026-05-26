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
--version
```

## Configuration

Optional `config.toml` (auto-loaded from the repo root, or `--config PATH`) overrides the built-in defaults — copy `config.example.toml` to start. It holds the PCSS agent path, the Coopesantos/PCSS tariff rates (update when the quarterly rate changes), the CO₂ factor, voltage/load thresholds, the runtime curve, and an opt-in `[alerts]` switch.

## Tests

Tests are **pytest** under `tests/` (math + tray unit tests, plus browser-driven E2E of the replay controls). The E2E fixture is hermetic — it synthesizes data and builds a temp dashboard, so no real PCSS logs are needed.

```
# Fast unit tests (no browser):
.venv\Scripts\python.exe -m pytest tests -m "not e2e"

# Browser E2E (Playwright + chromium; add -n auto to parallelize):
.venv\Scripts\python.exe -m pytest tests -m e2e

# A single suite / one test:
.venv\Scripts\python.exe -m pytest tests\e2e_pause_freeze.py
.venv\Scripts\python.exe -m pytest tests\test_math.py -k tiered
```

Playwright is intentionally *not* in `requirements.txt` (it's in the `dev` extra). Set `STATEOFUPS_E2E_REAL=1` to run E2E against the committed `output/dashboard.html` instead of synthetic data.

## Files

| File | What it is |
|---|---|
| `analyze_ups.py` | CLI orchestrator. Loads logs, computes stats, builds the dashboard. |
| `pcss/` | The package: `config`, `common`, `loaders`, `stats`, `dashboard`, `animation` (+ `animation.js`). |
| `tray_status.py` | Live system-tray battery icon (scrapes the PCSS web UI). |
| `config.example.toml` | Template config; copy to `config.toml`. |
| `run_analyzer.bat` / `run_tray.bat` | Double-click launchers. |
| `pyproject.toml` / `requirements.txt` | Packaging + deps (ruff/mypy/pytest config in pyproject). |
| `tests/` | pytest: `test_math.py`, `test_tray.py`, `test_animation_slicing.py`, `conftest.py` (hermetic fixture), `harness.py`, one `e2e_*.py` per browser suite. |
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
- **DataLog uses Spanish locale numbers** (`1.234,56` → 1234.56), parsed by `read_csv(decimal=",")` in `pcss/loaders.py`.
- **energylog uses dot-decimal** numbers and timestamps as **seconds since 2010-01-01 LOCAL** (not UTC, not Unix epoch). Verified empirically by aligning the first energylog timestamp with the first DataLog timestamp.
- **Real wattage is `null`** in energylog — the BX2000M has no wattmeter. PCSS calculates power as `relativeLoad × calculatedMaxLoad / 100`, where `calculatedMaxLoad` is 1400W (declared in the energylog header).
- **PCSS cost is wrong by design.** PCSS supports only a single rate, but Coopesantos T-RE is tiered. The dashboard reports both so the discrepancy is visible. The "Cost (Coopesantos tiered)" row in the summary table is the one that matches an actual bill.
- **Tariff constants** default in `pcss/config.py` and are overridable via `config.toml` `[tariff]` (`coopesantos_low/high`, `tier_limit_kwh`, `pcss_flat`). When the quarterly Coopesantos rate changes, edit `config.toml` — no code change. (The scheduled `trig_01PgYk7Fb6HXqGjcJ5CiAbC1` agent will email when this needs to happen.)
- **High-load episode duration** counts each sample as one sampling interval, so a k-sample run is ~k×interval (not the (k−1)×interval span between first/last timestamps); this can surface short episodes the old span-based measure missed.

## Paths assumed

The PCSS agent directory defaults in `pcss/config.py`:

```
PCSS_AGENT = C:\Program Files\APC\PowerChute Serial Shutdown\agent
```

If PCSS is reinstalled elsewhere, set `[paths] pcss_agent` in `config.toml` (or pass `--agent-dir`).
