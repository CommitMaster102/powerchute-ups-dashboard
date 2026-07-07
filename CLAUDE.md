# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

PowerChute Serial Shutdown (PCSS) log analyzer and status tray for the APC BX2000M-LM UPS on Windows. PCSS writes logs under `C:\Program Files\APC\PowerChute Serial Shutdown\agent\`; this repository converts them into a dashboard and a system-tray battery icon. See `README.md` for background: why both PCSS-flat and Coopesantos-tiered cost are reported, and how log growth is measured.

Two entry points share the **`pcss/` package**: `analyze_ups.py` (a CLI that produces the HTML dashboard) and `tray_status.py` (the tray icon). They also share the `output/` directory.

The analyzer (`analyze_ups.py` + `pcss/`) is pure Python and runs on any OS against a configured/exported log directory; only `tray_status.py` is Windows-only (Windows Credential Manager + tray backend). The default `PCSS_AGENT` path is the Windows install location; when it does not exist, `analyze_ups.py` prints how to set `--agent-dir`/`[paths] pcss_agent` instead of failing. CI runs on Windows only.

## Commands

First-time setup (no venv is committed):
```
python -m venv .venv
.venv\Scripts\pip install -e .[dev]      # runtime + dev (playwright, pytest, ruff, mypy)
.venv\Scripts\python -m playwright install chromium   # for the E2E tests
```
(`pip install -r requirements.txt` installs runtime deps only — no test tooling.)

| Task | Command |
|---|---|
| Run the analyzer (console summary + `output/dashboard.html`, opens browser) | `.venv\Scripts\python.exe analyze_ups.py` — or double-click `run_analyzer.bat` |
| Analyzer flags | `--no-browser`, `-o/--output PATH`, `--since/--until YYYY-MM-DD`, `-q/--quiet`, `--no-snapshot`, `--config PATH`, `--agent-dir PATH`, `--json PATH`, `-v/--verbose`, `--version` (see `--help`) |
| Run the tray icon (silent, no console) | `run_tray.bat` (launches `pythonw.exe tray_status.py`) |
| Register the daily scheduled run (Windows, per-user) | `powershell -ExecutionPolicy Bypass -File register_scheduled_task.ps1 [-RunTime HH:mm] [-Force]` — creates a Task Scheduler job that runs `scheduled_run.ps1` (guarded: single mutex, skip-if-running, once-a-day marker `output/last_scheduled_run.txt`; logs to `output/scheduled_run.log`) |
| All unit tests (fast, no browser) | `.venv\Scripts\python.exe -m pytest tests -m "not e2e"` |
| Whole suite, parallel (seconds) | `.venv\Scripts\python.exe -m pytest tests -n auto --dist worksteal --reruns 2 --reruns-delay 1` |
| All E2E browser suites | `.venv\Scripts\python.exe -m pytest tests -m e2e -n auto --dist worksteal` — or `tests\run_e2e.py` |
| A single E2E suite / panel | `.venv\Scripts\python.exe -m pytest tests\e2e_zoom.py` · `... "tests\e2e_render.py::test_panel_renders_svg[lv]"` |
| One math test | `.venv\Scripts\python.exe -m pytest tests\test_math.py -k tiered` |
| Lint / types | `.venv\Scripts\python.exe -m ruff check .` · `.venv\Scripts\python.exe -m mypy pcss analyze_ups.py tray_status.py` |

Tests are **pytest** under `tests/`. `tests/conftest.py` is hermetic: it synthesizes a small DataLog+energylog (with one sampling gap and two voltage anomalies), runs the real pipeline to a temp `dashboard.html` with an explicit `--config` (so a developer's local `config.toml` cannot leak in), and serves it to one shared headless Chromium — so E2E does **not** need real PCSS logs (set `STATEOFUPS_E2E_REAL=1` to test the generated `output/dashboard.html` instead — `output/` is gitignored, so it must exist locally). **Playwright (+chromium) is required for E2E but is test-only** (in the `dev` extra, not `requirements.txt`).

**E2E notes:** `tests/harness.py` holds the panel constants (`PANELS`, `SYNC_PANELS`) and the Playwright helpers (`wait_ready`, `hover_panel`, `drag_panel`, `panel_box` — which scrolls the target into view first, because `page.mouse` events only land inside the viewport). The suites are `e2e_render / e2e_tooltip / e2e_zoom / e2e_sync / e2e_permalink / e2e_anomjump / e2e_touch / e2e_export / e2e_lightbox / e2e_reveal / e2e_theme / e2e_offline`, parametrized per panel where it helps `pytest-xdist -n auto` spread work across cores. The touch suite opens its own `has_touch` context and drives multi-finger gestures through CDP `Input.dispatchTouchEvent`. The `dash` fixture **resets the page in place before every test** via `__chartsDebug.resetAll()` (full time window, nothing pinned/hidden, lightbox closed — no full reload), so each item is order-independent across xdist workers (which share one page per worker). `--reruns` (pytest-rerunfailures) covers the rare timing flake. `tests/harness.py:PANELS` must stay in sync with the payload keys built in `pcss/dashboard.py` (the session fixture asserts on mismatch). The reveal/theme/offline suites open their own pages from the session browser (`_browser`) — the theme suite against a single `auto`-themed build (`auto_dashboard_path`) whose `prefers-color-scheme` is emulated with Playwright's `emulate_media(color_scheme=...)` and whose header toggle is driven live (roadmap item 30 replaced the old second light build / `light_dashboard_path` fixture). CI (`.github/workflows/ci.yml`) always runs lint+mypy+unit on code changes; the E2E job is **opt-in** — it runs only on a PR labeled `e2e` or a manual `workflow_dispatch` (expensive, so off by default). The lint/unit job installs only `.[lint,test]` (no Playwright); the E2E job installs `.[test,e2e]`.

## Architecture

### `pcss/` package — the analyzer

`analyze_ups.py` is a ~340-line CLI orchestrator: `parse_args()` → `config.load_config()` → load logs → compute → `build_dashboard()` → write/open HTML (plus an optional `--json` summary and opt-in alerts). The modules are:

- **`pcss/config.py`** — defaults + `load_config(path, agent_dir, output)` which overlays a `config.toml` and CLI overrides onto module-level constants. **Config is module-level state**: consumers read `config.X` at call time, so `load_config()` mutating these before the pipeline runs is how overrides take effect (no Config object threaded through every function). `config.example.toml` documents the keys.
- **`pcss/common.py`** — shared helpers (`parse_pcss_number`, `ts_2010_to_dt`, `fmt_bytes`, `fmt_crc`, `EPOCH_2010`).
- **`pcss/loaders.py`** — `load_datalog` (vectorized via `read_csv(decimal=",")`; surfaces a count of skipped malformed rows), `load_energylog`, `record_size_snapshot`, `history_summary`, and the DataLog archive (`append_datalog_archive` / `load_datalog_archive` / `merge_datalog_frames`: monthly CSV partitions under `output/archive/`, whole-row dedup so re-appends are no-ops, schema drift tolerated; `--no-snapshot` skips the append, `[archive] enabled=false` disables it). The pipeline analyzes the merged live+archive frame; disk-growth projections still use the live file only.
- **`pcss/stats.py`** — stats, anomaly/gap/high-load detection, on-battery episode inference (`detect_on_battery_episodes`: low line voltage corroborated by a capacity drop), battery replace-by projection (`battery_replace_projection`: rolling-median fit, silent under `battery_trend_min_days`), energy/cost/CO2 with billing-period grouping (`[tariff] billing_cycle_start_day`; tier limit per period, partial periods flagged), runtime interp, cross-validation.
- **`pcss/eventlog.py`** — binary EventLog parsing: `load_eventlog` decodes the Java ObjectOutputStream generically via javaobj-py3 and returns `(frame, status)` — status "ok" / "missing" / "no-parser" / "empty" / "parse-error: ..." so the analyzer never fails on it. `load_event_names` reads the id→name resource bundles from the installed PCSS jars (falls back to a built-in table); `on_battery_spans` pairs On Battery (`3.5.1.5.4.1`) with No Longer On Battery (`3.5.1.5.4.2`) into authoritative outage spans that replace the item-6 inference in the dashboard when available; `append_event_archive`/`load_event_archive`/`merge_event_frames` persist events to `output/archive/events.csv`. Tests run against `tests/fixtures/EventLog`, a real capture with no personal data; assertions use epoch-ms deltas so they are timezone-independent (CI runs UTC).
- **`pcss/dashboard.py`** — `build_dashboard(...) -> str`: assembles the JSON payload (per-panel series, gap spans, KPI cards, health pill) and the design shell HTML, and returns the finished page. One `_panel_*()` builder per chart card. `PALETTES` holds the dark and light palettes; both ship in the payload (`palettes`) and the panel builders emit palette-neutral color **role** names (the palette keys, e.g. `"blue"`), which `charts.js` resolves to concrete hex from the active palette at draw time (roadmap item 30). Page chrome rides CSS custom properties (both theme blocks, selected by `prefers-color-scheme` / `data-theme`); server-rendered semantic accents (KPI cards, health pill, summary rows) use `var(--role)`.
- **`pcss/charts.js`** — the chart engine (below).

Three data sources, each in a different format and requiring different parsing:
- **DataLog** (TSV, ~20-min samples): Spanish-locale numbers (`1.234,56` → `1234.56`) parsed by `read_csv(decimal=",")`.
- **energylog/*.log** (`;`-delimited, 5-min): dot-decimal; `real_w` is always `null` (no wattmeter — power = `relativeLoad × calculatedMaxLoad/100`, max load 1400W from the header). Each row carries its file's `interval_sec` so kWh stays correct if PCSS is reconfigured. Timestamps are **seconds since 2010-01-01 LOCAL time** — `ts_2010_to_dt()`, verified empirically.
- **EventLog** (binary Java-serialized): parsed by `pcss/eventlog.py` (see above); the file size still feeds the growth tracking.

`output/size_history.csv` is append-only and is retained beyond PCSS's own log rotation. Do not truncate it.

### Dashboard chart engine (`pcss/charts.js`)

Dark card-grid page (ported from the Claude Design project "PowerChute UPS Dashboard Redesign"): header + health pill, a five-card KPI row with sparklines, five titled sections on a 12-column grid, HTML reference tables, and a footer. Charts are **dependency-free inline SVG** rendered client-side by `pcss/charts.js` — Plotly is gone and the page makes zero network requests. The contract:

- **Python computes, JS renders.** `build_dashboard()` embeds one JSON payload through a single `__DASH_DATA__` token in `charts.js`. Panel keys: `lv ul pw hm bv bc rt kw daily cmp wk growth proj cad`; each card is a `#panel-<key>` container. A `None` panel renders a client-side empty-state note. The payload also carries `episodes` (on-battery spans, amber `ep-strip` above the red gap strip), `strings` (localized chart labels — `[dashboard] language = "en" | "es"`, table in `pcss/dashboard.py`), heatmap `dayKeys`, and `meta.default_preset_days` (30-day preset on load once history exceeds 45 days).
- **Timezone contract:** timestamps cross the boundary as **epoch-ms integers with the log's naive local wall-clock encoded as if UTC**, and charts.js formats labels with `getUTC*` only — labels always match the log regardless of browser timezone. `tests/test_chart_payload.py` pins this.
- **Interactions:** hover crosshair + tooltip (click pins, Esc unpins); the `sync: true` flag marks the crosshair-mirror group ONLY — zoom/pan state is strictly per panel; hovering a sync panel also outlines the matching day row + hour cell in the heatmap (`highlightHeatmap`). On the hovered chart: drag ALWAYS zoom-selects (never a mode-dependent pan), `Shift`+drag pans, wheel zooms around the cursor, double-click / `reset` pill / `0` restores. Touch: a drag is claimed only on horizontal intent (vertical swipes scroll), two fingers pinch-zoom around their midpoint, a tap pins the tooltip, and `@media (pointer: coarse)` keeps the card tools visible. Chart boxes are keyboard-focusable (`tabindex`, `role="img"`, aria-label with a Python-generated data summary); focus targets the keyboard shortcuts without hover. The `All·30d·7d·24h` preset pills by the Power Quality header are the one global control: they set every time panel's own window, each anchored to its own newest sample (`applyPreset`); `__chartsDebug.setWindow` is the equivalent test hook. The active preset or per-panel zooms live in the URL hash (`updateHash`/`restoreFromHash`, base-36 ms via `history.replaceState`; malformed hashes fall back silently). The Line Voltage card gets a `tool-anom` flag button when anomalies exist (`jumpAnomaly` cycles clustered anomalies at ±6 h padding). Legends toggle series; dense series are min/max-decimated to ~2 points per pixel and re-decimated per window change; DataLog gaps render as slim strips on the x-axis (class `gap-strip`), on-battery episodes as amber `ep-strip`. Each card's hover menu exports PNG (SVG→canvas) and CSV (always en-US machine-standard, regardless of language), and expands into a lightbox (a second view of the same panel — gestures there act on that panel only). The header `⎙ pdf` button prints the page (`@media print` stylesheet); `[dashboard] refresh_minutes` adds a meta refresh. Charts draw in once at load (clip-path sweep, skipped under `prefers-reduced-motion` / `?noanim=1`).
- **Test surface:** `window.__chartsDebug` (`ready`, `panelKeys`, `xwin`/`fullXwin`/`setWindow`, `zoom`, `isZoomed`, `hover`, `pinned`, `hidden`, `lightbox`, `anomalyClusters`, `jumpAnomaly`, `resetAll`) — the E2E suites and the between-test reset drive this. `resetAll` also clears the permalink hash and the anomaly-jump cursor.

### `tray_status.py` — the tray icon

A pystray loop that polls the PCSS web UI (`https://localhost:6547`). **TLS is pinned, not disabled**: `PCSSClient` saves the server's self-signed cert to `output/pcss_cert.pem` on first connection, verifies against it, and re-pins once on `SSLError` (`_request` wraps every call). The **password is stored in the OS keyring** (Windows Credential Manager, service `stateOfUPS-PCSS`); a plaintext password in `credentials.txt` is migrated into the keyring on first run and the file line is blanked. `PCSSClient` handles the Java form-token login (`j_security_check`), re-logs in on `SessionExpired`, and polls less often on `UserAlreadyConnected` (another session holds the login). `parse_status()` extracts the battery percentage from Spanish-locale HTML; `render_icon()` draws the icon. `AlertWatcher` tails `output/alerts.log` (starts at EOF so history never re-notifies, resets on truncation, 30-minute cooldown) and raises a toast via `icon.notify()` for new analyzer alerts. Single-instance mutex; file-only logging.

`credentials.txt` holds username/url/poll (gitignored). Delete `output/pcss_cert.pem` to force a re-pin (e.g. after a PCSS reinstall).

## Config — where to edit

`config.toml` (auto-loaded from repo root if present; or `--config PATH`) overrides the defaults in `pcss/config.py` — see `config.example.toml`:
- `[paths] pcss_agent` — or `--agent-dir`.
- `[tariff]` Coopesantos low/high + tier limit, PCSS flat rate (CRC/kWh) — update when the quarterly rate changes — plus `billing_cycle_start_day` (1 = calendar months; any other day groups by billing period and applies the tier limit per period).
- `[grid] co2_kg_per_kwh`, `[thresholds]` voltage envelope / `high_load_pct` / `datalog_expected_interval_min` / KPI pill cut points (`battery_charge_warn_pct`, `battery_charge_crit_pct`, `runtime_warn_min`, `runtime_crit_min`) / on-battery detection (`on_battery_voltage_v`, `on_battery_capacity_drop_pct`) / battery replace-by (`battery_replace_voltage_v`, `battery_trend_min_days`), `[runtime_curve]` watts→minutes.
- `[dashboard] theme = "auto" | "dark" | "light"` (auto is the default — one build ships both palettes as CSS custom properties, follows `prefers-color-scheme`, and a header toggle cycles auto/dark/light with the override encoded in the permalink hash; dark/light pin the initial theme), `model` (the header's UPS model name), `language = "en" | "es"`, and `refresh_minutes` (0 = static page; >0 emits a meta refresh).
- `[alerts] enabled` — opt-in append to `output/alerts.log` on anomalies/high-load/on-battery; the tray's `AlertWatcher` toasts new lines (starts at EOF, 30-min cooldown).
- `[archive] enabled` — DataLog archive under `output/archive/` (on by default; `--no-snapshot` skips the append). Do not truncate the archive: like `size_history.csv`, it outlives PCSS's own rotation.

`tray_status.py` URL/poll/username live in `credentials.txt`; the password in the keyring.
