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
| Whole suite, parallel (< 32 s) | `.venv\Scripts\python.exe -m pytest tests -n auto --dist worksteal --reruns 2 --reruns-delay 1` |
| All E2E browser suites | `.venv\Scripts\python.exe -m pytest tests -m e2e -n auto --dist worksteal` — or `tests\run_e2e.py` |
| A single E2E suite / group | `.venv\Scripts\python.exe -m pytest tests\e2e_pause_freeze.py` · `... "tests\e2e_isolation.py::test_isolation[lv]"` (each `e2e_*.py` also runs standalone: `python tests\e2e_pause_freeze.py`) |
| One math test | `.venv\Scripts\python.exe -m pytest tests\test_math.py -k tiered` |
| Lint / types | `.venv\Scripts\python.exe -m ruff check .` · `.venv\Scripts\python.exe -m mypy pcss analyze_ups.py tray_status.py` |

Tests are **pytest** under `tests/`. `tests/conftest.py` is hermetic: it synthesizes a small DataLog+energylog, runs the real pipeline to a temp `dashboard.html`, and serves it to one shared headless Chromium — so E2E does **not** need real PCSS logs (set `STATEOFUPS_E2E_REAL=1` to test the generated `output/dashboard.html` instead — `output/` is gitignored, so it must exist locally). **Playwright (+chromium) is required for E2E but is test-only** (in the `dev` extra, not `requirements.txt`).

**E2E notes:** `tests/harness.py` holds `TestRunner` + the per-suite assertions; each `tests/e2e_*.py` exposes a `run(runner, anim_data)` (reused by the standalone `__main__`) **and** a pytest test parametrized **per animation group** (`@pytest.mark.parametrize("group", ALL_GROUPS / CUMULATIVE_GROUPS)` from `harness.py`) so `pytest-xdist -n auto` spreads ~43 short browser checks across cores rather than looping 8 groups serially in one test (214 s → < 32 s). The `runner` fixture **reloads the page before every test**, so each item starts pristine and is order-independent across xdist workers (which share one page per worker); `fresh_runner` is now just an alias. `--reruns` (pytest-rerunfailures) covers the rare timing flake. `tests/harness.py:EXPECTED_GROUPS` (and `ALL_GROUPS`/`CUMULATIVE_GROUPS`) must stay in sync with the `_register_animation()` speeds/groups in `pcss/dashboard.py` (the fixture asserts on mismatch). CI (`.github/workflows/ci.yml`) always runs lint+mypy+unit on code changes; the E2E job is **opt-in** — it runs only on a PR labeled `e2e` or a manual `workflow_dispatch` (expensive, so off by default). The lint/unit job installs only `.[lint,test]` (no Playwright); the E2E job installs `.[test,e2e]`. Timing tolerances live in `harness.py` (`JITTER_MS`, `RPC_OVERHEAD_MS`); play-completion waits on the engine's real `'ended'` state, and the pause/state-machine suites drive play→pause inside a single `page.evaluate` so CDP latency can't race a short (~2.4 s) animation. Tests that need a pristine page use the `fresh_runner` fixture (reloads first); others use `runner`.

## Architecture

### `pcss/` package — the analyzer

`analyze_ups.py` is a ~320-line CLI orchestrator: `parse_args()` → `config.load_config()` → load logs → compute → `build_dashboard()` → write/open HTML (plus an optional `--json` summary and opt-in alerts). The modules are:

- **`pcss/config.py`** — defaults + `load_config(path, agent_dir, output)` which overlays a `config.toml` and CLI overrides onto module-level constants. **Config is module-level state**: consumers read `config.X` at call time, so `load_config()` mutating these before the pipeline runs is how overrides take effect (no Config object threaded through every function). `config.example.toml` documents the keys.
- **`pcss/common.py`** — shared helpers (`parse_pcss_number`, `ts_2010_to_dt`, `fmt_bytes`, `fmt_crc`, `EPOCH_2010`).
- **`pcss/loaders.py`** — `load_datalog` (vectorized via `read_csv(decimal=",")`; surfaces a count of skipped malformed rows), `load_energylog`, `record_size_snapshot`, `history_summary`.
- **`pcss/stats.py`** — stats, anomaly/gap/high-load detection, energy/cost/CO2, runtime interp, cross-validation.
- **`pcss/dashboard.py`** — `build_dashboard` (the 7×2 grid) + `add_timeseries` + a static battery-voltage degradation trend.
- **`pcss/animation.py`** + **`pcss/animation.js`** — the animation system (below).

Three data sources, each in a different format and requiring different parsing:
- **DataLog** (TSV, ~20-min samples): Spanish-locale numbers (`1.234,56` → `1234.56`) parsed by `read_csv(decimal=",")`.
- **energylog/*.log** (`;`-delimited, 5-min): dot-decimal; `real_w` is always `null` (no wattmeter — power = `relativeLoad × calculatedMaxLoad/100`, max load 1400W from the header). Each row carries its file's `interval_sec` so kWh stays correct if PCSS is reconfigured. Timestamps are **seconds since 2010-01-01 LOCAL time** — `ts_2010_to_dt()`, verified empirically.
- **EventLog** (binary Java-serialized): only its file size is read.

`output/size_history.csv` is append-only and is retained beyond PCSS's own log rotation. Do not truncate it.

### Dashboard animation system

7×2 Plotly grid; per-panel Play/Pause "cumulative reveal" uses the following contract:

- **`fig.frames` is kept EMPTY.** Python never builds `go.Frame`. `_replay_metadata()` / `_runtime_metadata()` / `_heatmap_metadata()` (in `pcss/animation.py`) emit metadata dicts; `_register_animation()` collects them.
- The initial render shows full data immediately. On Play, custom JS — kept in **`pcss/animation.js`** (loaded and injected via a single `__ANIM_DATA__` token by `_build_custom_controls_html`/`_inject_controls_into_html`) — reconstructs frames in the browser by slicing live trace data against `cutoffs_ms`.
- **Timezone handling:** Plotly serializes datetimes as ISO strings *without* a timezone; JS `Date.parse` reads those as LOCAL while Python cutoffs (`pd.Timestamp.value`, always ns) are UTC. The JS forces UTC parsing to match. `tests/test_animation_slicing.py` tests this; the browser-driven equivalents are in `tests/e2e_*.py`.

### `tray_status.py` — the tray icon

A pystray loop that polls the PCSS web UI (`https://localhost:6547`). **TLS is pinned, not disabled**: `PCSSClient` saves the server's self-signed cert to `output/pcss_cert.pem` on first connection, verifies against it, and re-pins once on `SSLError` (`_request` wraps every call). The **password is stored in the OS keyring** (Windows Credential Manager, service `stateOfUPS-PCSS`); a plaintext password in `credentials.txt` is migrated into the keyring on first run and the file line is blanked. `PCSSClient` handles the Java form-token login (`j_security_check`), re-logs in on `SessionExpired`, and polls less often on `UserAlreadyConnected` (another session holds the login). `parse_status()` extracts the battery percentage from Spanish-locale HTML; `render_icon()` draws the icon. Single-instance mutex; file-only logging.

`credentials.txt` holds username/url/poll (gitignored). Delete `output/pcss_cert.pem` to force a re-pin (e.g. after a PCSS reinstall).

## Config — where to edit

`config.toml` (auto-loaded from repo root if present; or `--config PATH`) overrides the defaults in `pcss/config.py` — see `config.example.toml`:
- `[paths] pcss_agent` — or `--agent-dir`.
- `[tariff]` Coopesantos low/high + tier limit, PCSS flat rate (CRC/kWh) — update when the quarterly rate changes.
- `[grid] co2_kg_per_kwh`, `[thresholds]` voltage envelope / `high_load_pct` / `datalog_expected_interval_min`, `[runtime_curve]` watts→minutes.
- `[alerts] enabled` — opt-in append to `output/alerts.log` on anomalies/high-load.

`tray_status.py` URL/poll/username live in `credentials.txt`; the password in the keyring.
