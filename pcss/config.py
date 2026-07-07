"""Configuration constants for the PCSS analyzer.

Phase 1 keeps these as module-level constants (identical values to the old
analyze_ups.py CONFIG block). Phase 2 layers a `Config` dataclass +
`load_config()` (TOML) + CLI overrides on top of these defaults.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np

# PCSS writes its logs here. Override if PCSS is reinstalled elsewhere.
PCSS_AGENT = Path(r"C:\Program Files\APC\PowerChute Serial Shutdown\agent")
DATALOG = PCSS_AGENT / "DataLog"
EVENTLOG = PCSS_AGENT / "EventLog"
ENERGYLOG_DIR = PCSS_AGENT / "energylog"

# output/ lives at the repo root (one level above this package).
OUTPUT = Path(__file__).resolve().parent.parent / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)
SIZE_HISTORY_CSV = OUTPUT / "size_history.csv"
DASHBOARD_HTML = OUTPUT / "dashboard.html"

# User-owned bill records for reconciliation: a CSV with
# columns period_start, kwh, amount_crc, next to config.toml at the repo
# root by default. The analyzer only ever reads this file, never writes it.
# A missing file simply disables the feature — bill reconciliation is opt-in
# per bill, not a required habit.
BILLS_FILE = Path(__file__).resolve().parent.parent / "bills.csv"

# User-owned dated lifecycle entries: a CSV with columns
# date, kind, label, next to config.toml at the repo root by default. The
# recognized kind "battery_replaced" marks a fit-segmentation boundary for
# battery_replace_projection; any other kind still rides through as a plain
# labeled marker on the dashboard's time panels. The analyzer only ever reads
# this file, never writes it. A missing file simply disables the feature.
ANNOTATIONS_FILE = Path(__file__).resolve().parent.parent / "annotations.csv"

# Coopesantos T-RE Residencial 2026 structural rates (CRC per kWh).
# First 200 kWh/month at LOW_RATE, anything beyond at HIGH_RATE.
COOPESANTOS_TIER_LIMIT_KWH = 200.0
COOPESANTOS_LOW_RATE = 78.17
COOPESANTOS_HIGH_RATE = 126.51
# What PCSS itself uses (single-rate mode — set to the high tier):
PCSS_FLAT_RATE = 126.51
# Day of the month the Coopesantos billing cycle starts on. The tier limit
# applies per billing period, so with a mid-month cycle the calendar-month
# grouping can drift from the bill near the tier boundary. 1 keeps the
# classic calendar-month grouping.
BILLING_CYCLE_START_DAY = 1

# Minimum distinct days of energylog evidence inside the current billing
# period before the end-of-period cost forecast will project anything at all
# (the battery_trend_min_days honesty pattern): a linear extrapolation from a
# couple of days of data is noise, not a forecast. Below this floor,
# forecast_period_cost() returns the honest "not enough of the period
# recorded yet" with no numbers.
FORECAST_MIN_DAYS = 5.0


@dataclass(frozen=True)
class TariffPeriod:
    """One [[tariff.history]] entry: the rate set Coopesantos and PCSS had in
    force from effective_from onward, until a later entry (if any) takes
    over. Field names mirror the flat [tariff] keys above."""
    effective_from: date
    coopesantos_low: float
    coopesantos_high: float
    tier_limit_kwh: float
    pcss_flat: float


# Historical tariff rate sets, oldest first (load_config() keeps this list
# date-sorted). Empty by default: with no [[tariff.history]] entries, every
# billing period is priced with the flat [tariff] keys above, exactly as
# before this feature existed.
TARIFF_HISTORY: list[TariffPeriod] = []
# Costa Rica grid CO2 intensity (Low Carbon Power 2024 dataset):
CO2_KG_PER_KWH = 0.098

# Empirical UPS runtime curve for the BX2000M-LM with current build.
# Used to estimate "if outage happens now, how long would the UPS last?"
RUNTIME_CURVE_W = np.array([0,   100, 150, 250, 500, 600, 800, 1200])
RUNTIME_CURVE_MIN = np.array([60, 40,  30,  15,  5,   3.5, 2,   0])

# Minimum number of usable discharge observations before
# calibrate_runtime_curve() will fit a measured curve at all. An observation
# needs a real capacity drop and a nearby energylog power sample, so most
# short outages contribute nothing; below this floor the honest result is
# "not enough discharge data yet", the same pattern as
# BATTERY_TREND_MIN_DAYS below.
CALIBRATION_MIN_EPISODES = 3

# Voltage envelope considered normal for 120V grids (NEC ±5%).
VOLTAGE_NORMAL_LOW = 114.0
VOLTAGE_NORMAL_HIGH = 126.0

# Sustained-high-load threshold for anomaly detection (matches PCSS umbral).
HIGH_LOAD_PCT = 80.0

# On-battery episode inference: a line-voltage sample below
# ON_BATTERY_VOLTAGE_V is a mains-loss candidate, and an episode is kept when
# the battery capacity fell by at least ON_BATTERY_CAPACITY_DROP_PCT across
# the same window (corroboration trades recall for precision — a lone 0 V
# sample with a flat capacity reads as a logging artifact).
ON_BATTERY_VOLTAGE_V = 50.0
ON_BATTERY_CAPACITY_DROP_PCT = 1.0

# Self-test detection: a battery-capacity drop of at least
# SELFTEST_DIP_PCT percentage points between consecutive samples, recovering
# to near the pre-dip level within SELFTEST_RECOVERY_SAMPLES samples, with
# line voltage staying inside the normal envelope throughout, reads as a UPS
# self-test rather than an on-battery episode (which the thresholds above
# already catch, via low line voltage). Both are heuristics tuned for the
# BX2000M-LM's factory self-test behavior; adjust if a real test's shape
# turns out to look different.
SELFTEST_DIP_PCT = 3.0
SELFTEST_RECOVERY_SAMPLES = 4

# Battery replace-by projection. The BX2000M-LM carries a
# 24 V lead-acid pack (12 cells); 25.6 V is 2.13 V per cell, the bottom of
# the float band — a pack resting below that at float no longer holds full
# charge. The projection stays silent ("not enough history") until the
# Battery Voltage history spans BATTERY_TREND_MIN_DAYS, because a slope over
# a few weeks is dominated by noise and temperature.
BATTERY_REPLACE_VOLTAGE_V = 25.6
BATTERY_TREND_MIN_DAYS = 60.0

# Baseline-deviation energy alerts: a day's own hourly
# power profile is compared against the weekday or weekend baseline built
# from the full energylog history (pcss.stats.weekday_weekend_profiles), and
# flagged when the mean absolute deviation across the 24 hours — as a
# percent of the baseline's own mean power — exceeds
# BASELINE_DEVIATION_PCT. Both knobs are deliberately blunt: per-hour
# variance is noisy with only a few weeks of history, and a holiday sits in
# neither profile, so BASELINE_MIN_DAYS holds the detector silent (the
# honest "not enough history" status, nothing flagged) below that many
# distinct energylog days, the same floor pattern as
# BATTERY_TREND_MIN_DAYS above.
BASELINE_MIN_DAYS = 14
BASELINE_DEVIATION_PCT = 35.0

# DataLog default sample interval (PCSS factory default).
DATALOG_EXPECTED_INTERVAL_MIN = 20.0

# Log-staleness watchdog: the newest DataLog sample (merged live + archive)
# is compared against the wall clock once per run. The PC being off overnight
# produces no samples with nothing actually wrong, so both defaults are
# generous — half a day before a soft warning, two full days before it reads
# as a dead serial link, a stopped service, or a wedged agent rather than an
# ordinary quiet night.
STALE_WARN_HOURS = 12.0
STALE_CRIT_HOURS = 48.0

# KPI status-pill cut points for the dashboard header row. Battery charge is
# WARN below the warn threshold and ALERT below the crit threshold; estimated
# runtime works the same way in minutes.
BATTERY_CHARGE_WARN_PCT = 90.0
BATTERY_CHARGE_CRIT_PCT = 50.0
RUNTIME_WARN_MIN = 15.0
RUNTIME_CRIT_MIN = 7.0

# Dashboard color theme: "auto" (the default — a single build that follows the
# viewer's prefers-color-scheme), "dark", or "light". "dark" and "light" pin
# the initial theme exactly as before; "auto" ships both palettes and lets a
# header toggle override at view time.
DASHBOARD_THEME = "auto"
# UPS model name shown in the dashboard header.
DASHBOARD_MODEL = "APC BX2000M-LM"
# Dashboard language: "en" or "es". The PCSS installation and the electric
# bill are in Spanish; the string table lives in pcss/dashboard.py and the
# chart-side labels ride the payload so the two sides cannot drift. Number
# formatting stays en-US regardless (the CSV export is machine-standard).
DASHBOARD_LANGUAGE = "en"
# Auto-refresh interval in minutes for a dashboard left open on a monitor
# (a meta refresh matched to the scheduled-task cadence). 0 disables it —
# the page is a static snapshot by default. The permalink hash survives the
# reload, so the restored view state is kept.
DASHBOARD_REFRESH_MINUTES = 0.0

# Payload budget for multi-year archives: the cheap
# alternative to server-side decimation. 0 (the default) changes nothing at
# all — every panel ships full history, exactly as before this setting
# existed. A positive value windows only the raw per-sample frames fed to
# the dashboard (DataLog, energylog, the size-history growth series, and the
# gap/anomaly/episode overlays that ride alongside them) to the newest
# DASHBOARD_MAX_DAYS days, anchored to the newest DataLog sample — one cut
# applied in analyze_ups.py right before build_dashboard() is called. The
# console summary, --json, alerts, the archive append, and every fitted
# stats surface (the battery replace-by projection, the cost forecast, bill
# reconciliation, grid-quality trend) still see the complete history: they
# are computed before this window is applied, from the unwindowed frames.
# The archive on disk is never touched or truncated either way.
DASHBOARD_MAX_DAYS = 0.0

# Opt-in alerting: when enabled (config [alerts] enabled=true), the analyzer
# appends a line to ALERTS_LOG whenever the analyzed window has voltage
# anomalies or sustained high-load episodes. Email/notify is a documented
# extension point (no SMTP dependency by default).
ALERTS_ENABLED = False
ALERTS_LOG = OUTPUT / "alerts.log"

# Push notification channel: when true, the tray posts
# each new alerts.log line to a webhook URL as well as raising the toast.
# This key only says whether the channel is turned on; the URL itself is
# never configured here — it lives in the OS keyring (tray_status.py's
# KEYRING_SERVICE, under a dedicated entry name), so it never appears in
# this file or the repo. A webhook send still requires that keyring entry to
# exist; with this flag true and no URL configured, the tray logs a no-op
# rather than sending anything. Off by default.
WEBHOOK_ENABLED = False

# Weekly digest: opt-in on top of ALERTS_ENABLED, since
# alerts.log is its only transport — the toast and the item-23 webhook then
# deliver it for free, and this feature must not grow one of its own. Once
# per ISO week, analyze_ups.py appends one summary line alongside whatever
# event-driven anomaly line that run may also have written. LAST_DIGEST_MARKER
# records the ISO (year, week) of the last run that actually appended a
# digest line, the same directory convention as scheduled_run.ps1's own
# output/last_scheduled_run.txt, so a same-week rerun is a no-op. Off by
# default.
WEEKLY_DIGEST_ENABLED = False
LAST_DIGEST_MARKER = OUTPUT / "last_digest.txt"

# DataLog archive: PCSS keeps roughly one month of DataLog samples and
# discards older ones, so each analyzer run appends the freshly loaded rows
# to monthly CSV partitions under ARCHIVE_DIR and the pipeline merges the
# archive back in. The append is idempotent (exact duplicate rows are
# dropped), and --no-snapshot skips it like the size-history snapshot.
ARCHIVE_ENABLED = True
ARCHIVE_DIR = OUTPUT / "archive"


def tariff_rates_for(period_start: date) -> tuple[float, float, float, float, str]:
    """The Coopesantos low/high rates, tier limit, and PCSS flat rate in
    force for a billing period that starts on period_start.

    Picks the newest TARIFF_HISTORY entry whose effective_from is at or
    before period_start (TARIFF_HISTORY is kept date-sorted by
    load_config()). A period starting before the earliest entry, or an empty
    TARIFF_HISTORY, falls back to the flat [tariff] keys above. Returns
    (low, high, tier_limit_kwh, flat, label), where label is a short tag
    saying which rates applied: "current rates" for the flat-key fallback,
    or "rates from YYYY-MM-DD" for a history entry.

    If two entries share the same effective_from, this loop keeps overwriting
    `chosen` for each one in turn, so the entry that sorted LATER wins — see
    _parse_tariff_history's docstring for the full tie-break explanation.
    """
    chosen: TariffPeriod | None = None
    for entry in TARIFF_HISTORY:
        if entry.effective_from <= period_start:
            chosen = entry
        else:
            break
    if chosen is None:
        return (COOPESANTOS_LOW_RATE, COOPESANTOS_HIGH_RATE, COOPESANTOS_TIER_LIMIT_KWH,
                PCSS_FLAT_RATE, "current rates")
    return (chosen.coopesantos_low, chosen.coopesantos_high, chosen.tier_limit_kwh,
            chosen.pcss_flat, f"rates from {chosen.effective_from:%Y-%m-%d}")


def _parse_tariff_history(entries: object) -> list[TariffPeriod]:
    """Parse and validate [[tariff.history]] entries into a date-sorted list.

    Each entry must carry effective_from plus the four rate fields the flat
    [tariff] keys hold. effective_from accepts either a quoted "YYYY-MM-DD"
    string or an unquoted TOML date literal (tomllib hands that back as a
    datetime.date); an unquoted TOML date-time literal (datetime.datetime) is
    also accepted and truncated to its date. A malformed or incomplete entry
    — an unparseable date, a wrong-type or missing effective_from, a
    non-numeric rate, a missing rate field, or `history` not being a list of
    tables — is a user config error, and it is reported loudly and naming the
    entry rather than being silently skipped or left to raise an unlabeled
    built-in exception.

    Two entries are allowed to share the same effective_from (not itself a
    config error). The list returned here is sorted with Python's stable
    sort, which preserves the original relative order of equal keys, and
    tariff_rates_for() picks the newest entry at or before a period's start
    by scanning this sorted list and repeatedly overwriting its `chosen`
    candidate — so among entries tied on effective_from, whichever one
    appeared LATER in this list (equivalently, later in the config.toml
    array) wins. This is undocumented behavior worth knowing rather than a
    deliberately designed tie-break; a config with a genuine duplicate should
    still be fixed to remove one of the two entries.
    """
    if not isinstance(entries, list):
        raise ValueError(
            f"config error: [tariff.history] must be a list of tables (got "
            f"{type(entries).__name__})")
    required = ("coopesantos_low", "coopesantos_high", "tier_limit_kwh", "pcss_flat")
    parsed: list[TariffPeriod] = []
    for i, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(
                f"config error: [[tariff.history]] entry {i} must be a table "
                f"(got {type(entry).__name__})")
        if "effective_from" not in entry:
            raise ValueError(
                f"config error: [[tariff.history]] entry {i} is missing "
                f"'effective_from' (expected a date literal like 2026-04-01 or "
                f"the quoted string \"2026-04-01\")")
        raw_date = entry["effective_from"]
        if isinstance(raw_date, datetime):
            # An unquoted TOML date-time literal parses to datetime.datetime,
            # a subclass of date, so this check must come before the plain
            # date check below.
            effective_from = raw_date.date()
        elif isinstance(raw_date, date):
            effective_from = raw_date
        elif isinstance(raw_date, str):
            try:
                effective_from = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise ValueError(
                    f"config error: [[tariff.history]] entry {i} has an invalid "
                    f"effective_from {raw_date!r} (expected YYYY-MM-DD): {exc}") from exc
        else:
            # Present, but a type that can never be a date (e.g. a bare TOML
            # integer such as effective_from = 20260401 with no dashes) —
            # distinct from the key being absent altogether (handled above).
            raise ValueError(
                f"config error: [[tariff.history]] entry {i} has a wrong type "
                f"for 'effective_from': {raw_date!r} is a {type(raw_date).__name__}, "
                f"not a date (expected a date literal like 2026-04-01 or the "
                f"quoted string \"2026-04-01\")")
        missing = [f for f in required if f not in entry]
        if missing:
            raise ValueError(
                f"config error: [[tariff.history]] entry {i} (effective_from="
                f"{effective_from}) is missing required field(s): {', '.join(missing)}")
        rates: dict[str, float] = {}
        for field in required:
            raw_value = entry[field]
            try:
                rates[field] = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"config error: [[tariff.history]] entry {i} (effective_from="
                    f"{effective_from}) has a non-numeric '{field}' value "
                    f"{raw_value!r}: {exc}") from exc
        parsed.append(TariffPeriod(effective_from=effective_from, **rates))
    parsed.sort(key=lambda p: p.effective_from)
    return parsed


def load_config(path: Path | None = None, *, agent_dir: Path | None = None,
                output: Path | None = None) -> Path | None:
    """Overlay a config.toml (and CLI overrides) onto the module defaults.

    Config is intentionally module-level state: consumers read ``config.X`` at
    call time, so mutating these globals here — before the pipeline runs — is
    how a config file / CLI flags take effect, without threading a Config
    object through every function. Returns the config path that was used (or
    None). When `path` is None, falls back to ./config.toml if it exists.
    """
    global PCSS_AGENT, DATALOG, EVENTLOG, ENERGYLOG_DIR, DASHBOARD_HTML, BILLS_FILE
    global ANNOTATIONS_FILE
    global COOPESANTOS_TIER_LIMIT_KWH, COOPESANTOS_LOW_RATE, COOPESANTOS_HIGH_RATE, PCSS_FLAT_RATE
    global BILLING_CYCLE_START_DAY, TARIFF_HISTORY, FORECAST_MIN_DAYS
    global CO2_KG_PER_KWH, RUNTIME_CURVE_W, RUNTIME_CURVE_MIN
    global VOLTAGE_NORMAL_LOW, VOLTAGE_NORMAL_HIGH, HIGH_LOAD_PCT, DATALOG_EXPECTED_INTERVAL_MIN
    global STALE_WARN_HOURS, STALE_CRIT_HOURS
    global ON_BATTERY_VOLTAGE_V, ON_BATTERY_CAPACITY_DROP_PCT
    global SELFTEST_DIP_PCT, SELFTEST_RECOVERY_SAMPLES
    global BATTERY_REPLACE_VOLTAGE_V, BATTERY_TREND_MIN_DAYS, CALIBRATION_MIN_EPISODES
    global BASELINE_MIN_DAYS, BASELINE_DEVIATION_PCT
    global BATTERY_CHARGE_WARN_PCT, BATTERY_CHARGE_CRIT_PCT, RUNTIME_WARN_MIN, RUNTIME_CRIT_MIN
    global DASHBOARD_THEME, DASHBOARD_MODEL, DASHBOARD_REFRESH_MINUTES, DASHBOARD_LANGUAGE
    global DASHBOARD_MAX_DAYS
    global ALERTS_ENABLED, WEBHOOK_ENABLED, WEEKLY_DIGEST_ENABLED, ARCHIVE_ENABLED

    if path is None:
        default = Path("config.toml")
        path = default if default.exists() else None

    data: dict = {}
    if path is not None and Path(path).exists():
        with open(path, "rb") as f:
            data = tomllib.load(f)

    paths = data.get("paths", {})
    tariff = data.get("tariff", {})
    grid = data.get("grid", {})
    th = data.get("thresholds", {})
    rc = data.get("runtime_curve", {})

    agent = agent_dir or paths.get("pcss_agent")
    if agent:
        PCSS_AGENT = Path(agent)
    DATALOG = PCSS_AGENT / "DataLog"
    EVENTLOG = PCSS_AGENT / "EventLog"
    ENERGYLOG_DIR = PCSS_AGENT / "energylog"

    bills_file = paths.get("bills_file")
    if bills_file:
        BILLS_FILE = Path(bills_file)

    annotations_file = paths.get("annotations_file")
    if annotations_file:
        ANNOTATIONS_FILE = Path(annotations_file)

    COOPESANTOS_LOW_RATE = float(tariff.get("coopesantos_low", COOPESANTOS_LOW_RATE))
    COOPESANTOS_HIGH_RATE = float(tariff.get("coopesantos_high", COOPESANTOS_HIGH_RATE))
    COOPESANTOS_TIER_LIMIT_KWH = float(tariff.get("tier_limit_kwh", COOPESANTOS_TIER_LIMIT_KWH))
    PCSS_FLAT_RATE = float(tariff.get("pcss_flat", PCSS_FLAT_RATE))
    BILLING_CYCLE_START_DAY = max(1, min(31, int(
        tariff.get("billing_cycle_start_day", BILLING_CYCLE_START_DAY))))
    FORECAST_MIN_DAYS = float(tariff.get("forecast_min_days", FORECAST_MIN_DAYS))
    raw_history = tariff.get("history")
    if raw_history is not None:
        TARIFF_HISTORY = _parse_tariff_history(raw_history)
    CO2_KG_PER_KWH = float(grid.get("co2_kg_per_kwh", CO2_KG_PER_KWH))
    VOLTAGE_NORMAL_LOW = float(th.get("voltage_normal_low", VOLTAGE_NORMAL_LOW))
    VOLTAGE_NORMAL_HIGH = float(th.get("voltage_normal_high", VOLTAGE_NORMAL_HIGH))
    HIGH_LOAD_PCT = float(th.get("high_load_pct", HIGH_LOAD_PCT))
    ON_BATTERY_VOLTAGE_V = float(th.get("on_battery_voltage_v", ON_BATTERY_VOLTAGE_V))
    ON_BATTERY_CAPACITY_DROP_PCT = float(
        th.get("on_battery_capacity_drop_pct", ON_BATTERY_CAPACITY_DROP_PCT))
    SELFTEST_DIP_PCT = float(th.get("selftest_dip_pct", SELFTEST_DIP_PCT))
    SELFTEST_RECOVERY_SAMPLES = int(
        th.get("selftest_recovery_samples", SELFTEST_RECOVERY_SAMPLES))
    BATTERY_REPLACE_VOLTAGE_V = float(
        th.get("battery_replace_voltage_v", BATTERY_REPLACE_VOLTAGE_V))
    BATTERY_TREND_MIN_DAYS = float(th.get("battery_trend_min_days", BATTERY_TREND_MIN_DAYS))
    BASELINE_MIN_DAYS = int(th.get("baseline_min_days", BASELINE_MIN_DAYS))
    BASELINE_DEVIATION_PCT = float(th.get("baseline_deviation_pct", BASELINE_DEVIATION_PCT))
    DATALOG_EXPECTED_INTERVAL_MIN = float(th.get("datalog_expected_interval_min", DATALOG_EXPECTED_INTERVAL_MIN))
    STALE_WARN_HOURS = float(th.get("stale_warn_hours", STALE_WARN_HOURS))
    STALE_CRIT_HOURS = float(th.get("stale_crit_hours", STALE_CRIT_HOURS))
    BATTERY_CHARGE_WARN_PCT = float(th.get("battery_charge_warn_pct", BATTERY_CHARGE_WARN_PCT))
    BATTERY_CHARGE_CRIT_PCT = float(th.get("battery_charge_crit_pct", BATTERY_CHARGE_CRIT_PCT))
    RUNTIME_WARN_MIN = float(th.get("runtime_warn_min", RUNTIME_WARN_MIN))
    RUNTIME_CRIT_MIN = float(th.get("runtime_crit_min", RUNTIME_CRIT_MIN))

    dash = data.get("dashboard", {})
    theme = str(dash.get("theme", DASHBOARD_THEME)).lower()
    if theme in ("auto", "dark", "light"):
        DASHBOARD_THEME = theme
    DASHBOARD_MODEL = str(dash.get("model", DASHBOARD_MODEL))
    DASHBOARD_REFRESH_MINUTES = max(0.0, float(
        dash.get("refresh_minutes", DASHBOARD_REFRESH_MINUTES)))
    lang = str(dash.get("language", DASHBOARD_LANGUAGE)).lower()
    if lang in ("en", "es"):
        DASHBOARD_LANGUAGE = lang
    DASHBOARD_MAX_DAYS = max(0.0, float(dash.get("max_days", DASHBOARD_MAX_DAYS)))

    if rc.get("watts") and rc.get("minutes"):
        RUNTIME_CURVE_W = np.array(rc["watts"], dtype=float)
        RUNTIME_CURVE_MIN = np.array(rc["minutes"], dtype=float)
    CALIBRATION_MIN_EPISODES = int(rc.get("calibration_min_episodes", CALIBRATION_MIN_EPISODES))

    alerts = data.get("alerts", {})
    ALERTS_ENABLED = bool(alerts.get("enabled", ALERTS_ENABLED))
    WEBHOOK_ENABLED = bool(alerts.get("webhook_enabled", WEBHOOK_ENABLED))
    WEEKLY_DIGEST_ENABLED = bool(alerts.get("weekly_digest", WEEKLY_DIGEST_ENABLED))
    ARCHIVE_ENABLED = bool(data.get("archive", {}).get("enabled", ARCHIVE_ENABLED))

    DASHBOARD_HTML = Path(output) if output else (OUTPUT / "dashboard.html")
    return path
