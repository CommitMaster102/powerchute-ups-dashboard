"""PowerChute Serial Shutdown UPS log analyzer — CLI entry point.

Thin orchestrator over the `pcss` package: load the three PCSS logs, compute
stats / anomalies / energy / cross-validation, snapshot file sizes, build the
SVG dashboard page, and write/open the HTML.

Run with no arguments to reproduce the classic behavior (console summary +
output/dashboard.html, opens browser). See `--help` for flags.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path

import pandas as pd

from pcss import config
from pcss.common import fmt_age_hours, fmt_bytes
from pcss.dashboard import _count, build_dashboard
from pcss.eventlog import (
    append_event_archive,
    load_event_archive,
    load_eventlog,
    merge_event_frames,
    on_battery_spans,
)
from pcss.loaders import (
    append_datalog_archive,
    history_summary,
    load_annotations,
    load_bills,
    load_datalog,
    load_datalog_archive,
    load_energylog,
    merge_datalog_frames,
    record_size_snapshot,
)
from pcss.stats import (
    assess_staleness,
    battery_replace_projection,
    calibrate_runtime_curve,
    compute_energy_summary,
    compute_stats_summary,
    cross_validate_load,
    datalog_stats,
    detect_baseline_deviations,
    detect_gaps,
    detect_high_load_episodes,
    detect_on_battery_episodes,
    detect_self_tests,
    detect_voltage_anomalies,
    estimate_runtime,
    forecast_period_cost,
    grid_quality_trend,
    reconcile_bills,
    self_test_sag_trend,
)

__version__ = "1.0.0"


# ======================================================================
# CLI
# ======================================================================
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="analyze_ups.py",
        description="Analyze PowerChute Serial Shutdown logs and build the dashboard.",
    )
    p.add_argument("-o", "--output", type=Path, default=None,
                   help="dashboard HTML path (default: output/dashboard.html)")
    p.add_argument("--no-browser", action="store_true",
                   help="don't open the dashboard in a browser after writing")
    p.add_argument("--since", metavar="YYYY-MM-DD", default=None,
                   help="only analyze samples on/after this date")
    p.add_argument("--until", metavar="YYYY-MM-DD", default=None,
                   help="only analyze samples on/before this date")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="suppress the console summary (still writes the dashboard)")
    p.add_argument("--no-snapshot", action="store_true",
                   help="don't append a row to size_history.csv (read-only run)")
    p.add_argument("--config", type=Path, default=None,
                   help="path to config.toml (default: ./config.toml if present)")
    p.add_argument("--agent-dir", type=Path, default=None,
                   help="override the PCSS agent directory")
    p.add_argument("--json", type=Path, default=None, metavar="PATH",
                   help="also write the summary as JSON to PATH")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="verbose logging")
    p.add_argument("--version", action="version", version=f"stateofups {__version__}")
    return p.parse_args(argv)


# ======================================================================
# Console reporter
# ======================================================================
def banner(s: str):
    print()
    print("=" * 72)
    print(s)
    print("=" * 72)


def _calibration_console_lines(calibration: dict | None) -> list[str]:
    """The RUNTIME-CURVE CALIBRATION console lines (roadmap item 16).

    Reports the count of usable discharges behind the fit and either the
    recovered drain-rate constant k with the observed load span it was fitted
    across, or the honest below-floor status — the same shape the battery and
    forecast sections already use. Kept a pure function of the calibration
    dict (``pcss.stats.calibrate_runtime_curve``'s result) so both the wording
    and the below-floor case stay unit-testable without a full pipeline run.
    """
    cal = calibration or {}
    if cal.get("status") == "calibrated":
        lines = [f"  Fitted from {cal['n_episodes']} usable discharge(s): "
                 f"k = {cal['k']:.5f} %/min/W"]
        w_lo, w_hi = cal.get("watts_observed_min"), cal.get("watts_observed_max")
        if w_lo is not None and w_hi is not None:
            if abs(w_hi - w_lo) < 1:
                lines.append(f"  Observed load: {w_lo:.0f} W (single operating point)")
            else:
                lines.append(f"  Observed load range: {w_lo:.0f}-{w_hi:.0f} W")
        return lines
    need = cal.get("min_episodes", config.CALIBRATION_MIN_EPISODES)
    return [f"  Not enough discharge data yet "
            f"({cal.get('n_episodes', 0)}/{need:.0f} usable discharges needed)."]


def _calibration_for_json(calibration: dict | None) -> dict:
    """The runtime-curve calibration (roadmap item 16) as machine-clean values
    for --json. A missing or non-finite numeric (NaN) becomes null rather than
    a non-standard JSON token, the same NaN-safe convention the grid-quality
    serializer follows."""
    cal = calibration or {}

    def clean(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return v

    return {
        "status": cal.get("status"),
        "n_episodes": cal.get("n_episodes"),
        "min_episodes": cal.get("min_episodes"),
        "k": clean(cal.get("k")),
        "watts_observed_min": clean(cal.get("watts_observed_min")),
        "watts_observed_max": clean(cal.get("watts_observed_max")),
    }


def _date_filter(df: pd.DataFrame, since: pd.Timestamp | None, until: pd.Timestamp | None) -> pd.DataFrame:
    if df.empty or "ts" not in df.columns:
        return df
    if since is not None:
        df = df[df["ts"] >= since]
    if until is not None:
        df = df[df["ts"] <= until]
    return df.reset_index(drop=True)


def _dashboard_window(datalog_df: pd.DataFrame, max_days: float) -> pd.Timestamp | None:
    """The cut point for the dashboard-only payload budget (roadmap item
    25): the newest max_days days, anchored to the newest DataLog sample
    rather than the wall clock. Returns the cutoff timestamp to trim the
    dashboard's raw frames to, or None when nothing should be trimmed —
    max_days is 0 or negative (the default, a deliberate no-op), there is
    no DataLog data to anchor on, or max_days is already at least as large
    as the recorded span, so trimming to it would remove nothing. Either
    way, None means the dashboard sees exactly the same frames it always
    has: this is what keeps max_days = 0 byte-identical."""
    if max_days <= 0 or datalog_df.empty:
        return None
    anchor = datalog_df["ts"].iloc[-1]
    cutoff = anchor - pd.Timedelta(days=max_days)
    if not (datalog_df["ts"] < cutoff).any():
        return None
    return cutoff


def _window_df(df: pd.DataFrame | None, col: str, cutoff: pd.Timestamp,
               end_col: str | None = None) -> pd.DataFrame | None:
    """Filter one dashboard-input frame to rows at or after cutoff.

    By default a row is kept when its own col timestamp is at/after cutoff.
    When end_col names a second timestamp column — a span's end, for gaps,
    high-load episodes, and on-battery episodes — a row is kept when THAT
    column is at/after cutoff instead, so an entry that began before the
    cutoff but extends into the window keeps its still-visible tail rather
    than being dropped whole (roadmap item 25 review finding 4: filtering a
    span on its start alone silently discards the visible portion of one
    straddling the cutoff). end_col is only used when it is actually present
    on df; otherwise this falls back to col, same as before end_col existed.

    A frame that is None, empty, or missing the relevant column passes
    through unchanged, so a caller can apply this uniformly without
    special-casing frames that happen to have nothing to trim."""
    if df is None or df.empty:
        return df
    key = end_col if end_col is not None and end_col in df.columns else col
    if key not in df.columns:
        return df
    return df[df[key] >= cutoff].reset_index(drop=True)


def _wall_clock_now() -> pd.Timestamp:
    """The single wall-clock read the pipeline makes, for the staleness
    watchdog (roadmap item 31) and the weekly digest gate (item 32).

    Honors the test-only STATEOFUPS_NOW environment variable, an ISO timestamp
    the hermetic E2E fixture (tests/conftest.py) sets so its fixed-date
    synthetic logs read as fresh rather than drifting into
    staleness-critical as real time passes. The variable is unset in normal
    operation, so the analyzer reads the real wall clock; a malformed value
    falls back to it too.
    """
    override = os.environ.get("STATEOFUPS_NOW")
    if override:
        try:
            return pd.Timestamp(override)
        except (ValueError, TypeError):
            pass
    return pd.Timestamp.now()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    quiet = args.quiet
    try:
        used_cfg = config.load_config(args.config, agent_dir=args.agent_dir, output=args.output)
    except ValueError as e:
        # A malformed [[tariff.history]] entry (roadmap item 17) raises a loud,
        # already-worded "config error: ..." ValueError from _parse_tariff_history.
        # Surface just that message and exit nonzero, rather than dumping a
        # traceback at a user who only mistyped a date in config.toml.
        print(str(e), file=sys.stderr)
        return 2

    def say(*a, **k):
        if not quiet:
            print(*a, **k)

    def section(title: str):
        if not quiet:
            banner(title)

    if args.verbose:
        say(f"  Config: {used_cfg or 'built-in defaults'}")
        say(f"  Agent dir: {config.PCSS_AGENT}")

    # The default agent path is Windows-only (that's where PCSS runs). On any
    # platform, if it isn't there, tell the user how to point at exported logs
    # instead of silently producing an empty report. Printed to stderr so it
    # never pollutes stdout / --json, and shown even under --quiet.
    if not config.PCSS_AGENT.exists():
        print(
            f"[warn] PCSS agent directory not found: {config.PCSS_AGENT}\n"
            "       PowerChute Serial Shutdown runs on Windows. Point the analyzer\n"
            "       at a directory of exported logs with:  --agent-dir PATH\n"
            "       or a config.toml [paths] pcss_agent = '...' (see config.example.toml).\n"
            "       Continuing; the report will be empty unless logs are reachable.",
            file=sys.stderr,
        )

    since_ts = pd.to_datetime(args.since) if args.since else None
    until_ts = (pd.to_datetime(args.until) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)) if args.until else None

    sizes = {
        "DataLog": config.DATALOG.stat().st_size if config.DATALOG.exists() else 0,
        "EventLog (binary)": config.EVENTLOG.stat().st_size if config.EVENTLOG.exists() else 0,
        "energylog/": sum(f.stat().st_size for f in config.ENERGYLOG_DIR.glob("*")) if config.ENERGYLOG_DIR.exists() else 0,
    }

    section("PCSS LOG FILES — CURRENT SIZES")
    for name, sz in sizes.items():
        say(f"  {name:25s} {fmt_bytes(sz):>10s}  ({sz:,} bytes)")
    say(f"  {'TOTAL':25s} {fmt_bytes(sum(sizes.values())):>10s}")

    if args.no_snapshot:
        hist = (pd.read_csv(config.SIZE_HISTORY_CSV, parse_dates=["timestamp"])
                if config.SIZE_HISTORY_CSV.exists() else pd.DataFrame())
    else:
        hist = record_size_snapshot(sizes)
    hist_stats = history_summary(hist)

    section("SIZE-HISTORY SNAPSHOTS")
    say(f"  History file: {config.SIZE_HISTORY_CSV}")
    say(f"  Snapshots so far: {len(hist)}")
    if hist_stats:
        say(f"  First snapshot : {hist_stats['first_ts']}")
        say(f"  Last snapshot  : {hist_stats['last_ts']}")
        say(f"  Total grew     : {fmt_bytes(hist_stats['delta_bytes'])} "
            f"({hist_stats['first_total']:,} -> {hist_stats['last_total']:,} bytes)")
        say(f"  Rate           : {fmt_bytes(hist_stats['bytes_per_minute'])}/min, "
            f"{fmt_bytes(hist_stats['bytes_per_hour'])}/hour, "
            f"{fmt_bytes(hist_stats['bytes_per_day'])}/day")
    else:
        say("  Run again later to see growth between snapshots.")

    # Disk-growth projections must use the full on-disk log: sizes["DataLog"]
    # is the whole-file size, so datalog_stats() needs the unfiltered frame —
    # otherwise file_size / (filtered span) over-reports the growth rate by
    # roughly full_span/filtered_span. Everything downstream (anomalies, gaps,
    # energy, cross-validation, dashboard series) uses the filtered frame.
    raw_datalog_df = load_datalog()

    # PCSS discards DataLog samples after roughly a month; the archive keeps
    # them. Append the freshly loaded rows (idempotent; skipped on read-only
    # runs like the snapshot), then merge the archive back in so the analyzed
    # frame spans the whole recorded life of the UPS, not one month.
    archive_added = 0
    archive_df = pd.DataFrame()
    if config.ARCHIVE_ENABLED:
        if not args.no_snapshot:
            archive_added = append_datalog_archive(raw_datalog_df)
        archive_df = load_datalog_archive()
    merged_datalog_df = merge_datalog_frames(raw_datalog_df, archive_df)

    # The binary EventLog decodes to authoritative event markers (outages,
    # self-tests, monitoring stops). It stays strictly optional: any parse
    # trouble becomes a status string, never a failed run. Parsed events are
    # archived like the DataLog rows so they outlive PCSS's own rotation.
    events_df, ev_status = load_eventlog()
    if config.ARCHIVE_ENABLED:
        if not args.no_snapshot and not events_df.empty:
            append_event_archive(events_df)
        events_df = merge_event_frames(events_df, load_event_archive())
    events_df = _date_filter(events_df, since_ts, until_ts)
    ev_spans = on_battery_spans(events_df)

    datalog_df = _date_filter(merged_datalog_df, since_ts, until_ts)
    dl_stats = datalog_stats(raw_datalog_df, sizes["DataLog"])

    # The staleness watchdog reads the wall clock exactly once, here; the
    # console line below, the dashboard health pill, and the alerts trigger
    # all consume this same result instead of each calling datetime.now().
    # "Newest sample" is the merged (live + archive), date-filtered frame —
    # in practice the live DataLog's own newest sample, since the archive
    # only ever holds older months.
    now = _wall_clock_now()
    staleness = assess_staleness(datalog_df["ts"].iloc[-1], now) if not datalog_df.empty else None

    section("DATALOG SUMMARY")
    if datalog_df.empty:
        say("  No DataLog rows yet.")
    else:
        say(f"  First sample   : {dl_stats['first']}")
        say(f"  Last sample    : {dl_stats['last']}")
        say(f"  Entries        : {dl_stats['n_entries']}")
        say(f"  Median interval: {dl_stats['median_interval_sec']:.0f} s "
            f"(~{dl_stats['median_interval_sec']/60:.1f} min)")
        say(f"  Bytes/entry    : {dl_stats['bytes_per_entry']:.1f}")
        say("")
        say(f"  Projected disk usage (over {dl_stats['span_days']:.2f} days of data):")
        say(f"    Per minute  : {fmt_bytes(dl_stats['minute_bytes'])}")
        say(f"    Per day     : {fmt_bytes(dl_stats['daily_bytes'])}")
        say(f"    Per month   : {fmt_bytes(dl_stats['monthly_bytes'])}")
        say(f"    Per year    : {fmt_bytes(dl_stats['yearly_bytes'])}")
        if staleness is not None and staleness["level"] != "fresh":
            say("")
            say(f"  [!] Stale data feed ({staleness['level']}): no new samples in "
                f"{fmt_age_hours(staleness['age_hours'])} "
                f"(warn >= {config.STALE_WARN_HOURS:g} h, crit >= {config.STALE_CRIT_HOURS:g} h)")
    if not archive_df.empty:
        say("")
        say(f"  Archive        : {len(archive_df)} rows, "
            f"{archive_df['ts'].iloc[0]} -> {archive_df['ts'].iloc[-1]} "
            f"(+{archive_added} new this run)")

    energy_df, energy_metas = load_energylog()
    energy_df = _date_filter(energy_df, since_ts, until_ts)
    energy_summary = compute_energy_summary(energy_df) if not energy_df.empty else {}
    forecast = forecast_period_cost(energy_summary)

    section("ENERGY LOG SUMMARY")
    if energy_df.empty:
        say("  energylog/ empty or unparseable.")
    else:
        for m in energy_metas:
            say(f"  {m.month}: {m.n_samples:>5d} samples, interval={m.interval_sec}s, max_load={m.max_load_w:.0f}W")
        say("")
        say(f"  Total samples       : {energy_summary['n_samples']}")
        say(f"  Span                : {energy_summary['first']} -> {energy_summary['last']}")
        say(f"  Total energy        : {energy_summary['total_kwh']:.4f} kWh")
        say(f"  Cost @ PCSS flat    : CRC {energy_summary['total_cost_pcss']:,.2f}")
        say(f"  Cost @ Coop. tiered : CRC {energy_summary['total_cost_tiered']:,.2f}")
        say(f"  CO2 emitted         : {energy_summary['total_co2_kg']:.4f} kg")
        if energy_summary["monthly"] is not None and not energy_summary["monthly"].empty:
            say("")
            label = ("Monthly breakdown:" if config.BILLING_CYCLE_START_DAY <= 1 else
                     f"Billing-period breakdown (cycle starts day {config.BILLING_CYCLE_START_DAY}):")
            say(f"  {label}")
            monthly = energy_summary["monthly"]
            history_active = energy_summary.get("tariff_history_active", False)
            cols = ["month", "kwh", "cost_pcss", "cost_tiered", "co2_kg", "partial"]
            if history_active:
                cols.append("rate_tag")
            for row in monthly[cols].itertuples(index=False, name=None):
                month, kwh, cost_pcss, cost_tiered, co2_kg, partial = row[:6]
                mark = " (partial)" if partial else ""
                # A rate boundary mid-history would otherwise look like a
                # consumption change, so say which rates priced this period.
                tag = f" [{row[6]}]" if history_active else ""
                say(f"    {month}: {kwh:>9.4f} kWh   "
                    f"PCSS=CRC {cost_pcss:>10,.2f}   "
                    f"Tiered=CRC {cost_tiered:>10,.2f}   "
                    f"CO2={co2_kg:>7.4f} kg{mark}{tag}")
        say("")
        if forecast["status"] == "projected":
            tier_bit = ""
            if forecast["already_crossed"]:
                tier_bit = "  [tier limit already exceeded this period]"
            elif forecast["tier_cross_date"] is not None:
                tier_bit = f"  [tier crosses ~{forecast['tier_cross_date']:%Y-%m-%d}]"
            # Same bracket-tag convention as the monthly breakdown above: only
            # named when tariff history is actually in play, so an ordinary
            # run (no [[tariff.history]] configured) does not clutter every
            # forecast line with a trivial "[current rates]".
            forecast_history_active = energy_summary.get("tariff_history_active", False)
            rate_bit = f"  [{forecast['rate_tag']}]" if forecast_history_active else ""
            say(f"  Forecast (projected, at the current pace): "
                f"{forecast['projected_kwh']:.2f} kWh by {forecast['period_end']:%Y-%m-%d}   "
                f"PCSS=CRC {forecast['projected_cost_pcss']:,.2f}   "
                f"Tiered=CRC {forecast['projected_cost_tiered']:,.2f}{tier_bit}{rate_bit}")
        else:
            say(f"  Forecast: not enough of the period recorded yet "
                f"({forecast['evidence_days']}/{forecast['min_days']:.0f} days) — no projection.")

    # Bill reconciliation (roadmap item 29): a user-owned bills.csv is opt-in
    # and, missing, disables the feature with no warning at all. A malformed
    # row or a period_start that does not align to a billing-period boundary
    # is reported here unconditionally (like the DataLog malformed-row
    # warning above) so a data problem is visible even under --quiet; the
    # reconciled-periods section itself, the dashboard table, and the --json
    # key all stay absent unless at least one bill actually reconciles.
    bills_df, bill_load_warnings = load_bills()
    for msg in bill_load_warnings:
        print(f"  [warn] {msg}")
    reconciled_bills, bill_align_warnings = reconcile_bills(bills_df, energy_summary)
    for msg in bill_align_warnings:
        print(f"  [warn] {msg}")
    if not reconciled_bills.empty:
        section("BILL RECONCILIATION")
        for row in reconciled_bills.itertuples(index=False):
            say(f"  {row.period}: UPS {row.ups_kwh:.4f} kWh / billed {row.billed_kwh:.4f} kWh "
                f"({row.share_pct:.1f}% UPS-metered share of the billed consumption)")
            say(f"    UPS cost (tiered) CRC {row.ups_cost_tiered:,.2f}   "
                f"billed CRC {row.billed_amount_crc:,.2f}   "
                f"implied rate CRC {row.implied_rate_crc_per_kwh:,.2f}/kWh   "
                f"vs tariff CRC {row.tariff_low:g}/{row.tariff_high:g} ({row.rate_tag})")

    # Battery lifecycle annotations (roadmap item 26): a user-owned
    # annotations.csv is opt-in and, missing, disables the feature with no
    # warning at all. A malformed row is reported here unconditionally (the
    # same pattern as the bills.csv warnings above), and the recognized
    # "battery_replaced" kind segments the replace-by projection below.
    annotations_df, annotation_warnings = load_annotations()
    for msg in annotation_warnings:
        print(f"  [warn] {msg}")

    # Runtime-curve calibration (roadmap item 16): fits a measured
    # capacity-percent-per-minute-per-watt model from observed on-battery
    # discharges, to confirm or correct the hand-estimated [runtime_curve]
    # table. Uses the authoritative EventLog spans (ev_spans), not the
    # DataLog-inferred fallback below, so durations are exact; the same
    # battery-replacement boundary as the replace-by projection keeps a
    # replaced battery's discharges out of the fit.
    calibration = calibrate_runtime_curve(ev_spans, datalog_df, energy_df,
                                          annotations=annotations_df)

    section("ANOMALIES & EVENTS")
    voltage_anomalies = detect_voltage_anomalies(datalog_df)
    say(f"  Voltage out of {config.VOLTAGE_NORMAL_LOW}-{config.VOLTAGE_NORMAL_HIGH}V envelope: "
        f"{len(voltage_anomalies)} samples")
    if not voltage_anomalies.empty:
        for ts, volts in voltage_anomalies[["ts", "Line Voltage"]].head(5).itertuples(index=False, name=None):
            say(f"    {ts}  {volts} V")
        if len(voltage_anomalies) > 5:
            say(f"    ... ({len(voltage_anomalies)-5} more)")

    on_battery = detect_on_battery_episodes(datalog_df)
    say(f"  On-battery episodes (visible at the {config.DATALOG_EXPECTED_INTERVAL_MIN:.0f}-min "
        f"cadence; short outages between samples are missed): {len(on_battery)}")
    if not on_battery.empty:
        for start, end, dmin, minv, drop in on_battery[
            ["start", "end", "duration_min", "min_voltage", "capacity_drop_pct"]
        ].head(5).itertuples(index=False, name=None):
            drop_txt = f", capacity -{drop:.0f}%" if pd.notna(drop) else ""
            say(f"    {start} -> {end}  ~{dmin:.0f}min  min {minv:.1f} V{drop_txt}")
        if len(on_battery) > 5:
            say(f"    ... ({len(on_battery)-5} more)")

    if ev_status == "ok" and not events_df.empty:
        say(f"  EventLog (parsed): {len(events_df)} events")
        for name, n in events_df["name"].value_counts().head(6).items():
            say(f"    {n:>4d} × {name}")
        say(f"  On-battery episodes (from EventLog, authoritative): {len(ev_spans)}")
        for start, end, dmin, is_open in ev_spans[
            ["start", "end", "duration_min", "open"]
        ].head(5).itertuples(index=False, name=None):
            if is_open:
                say(f"    {start} -> (still on battery at end of log)")
            else:
                say(f"    {start} -> {end}  {dmin:.1f} min")
    else:
        say(f"  EventLog: not parsed ({ev_status}) — on-battery detection relies on"
            " the DataLog inference above")

    high_load = detect_high_load_episodes(energy_df) if not energy_df.empty else pd.DataFrame()
    say(f"  Sustained high-load episodes (>={config.HIGH_LOAD_PCT}%, >=10min): {len(high_load)}")
    if not high_load.empty:
        for start, end, dmin, ppct, pw in high_load[
            ["start", "end", "duration_min", "peak_pct", "peak_w"]
        ].head(5).itertuples(index=False, name=None):
            say(f"    {start} -> {end}  {dmin:.1f}min  peak {ppct:.0f}% / {pw:.0f}W")
        if len(high_load) > 5:
            say(f"    ... ({len(high_load)-5} more)")

    gaps = detect_gaps(datalog_df)
    say(f"  DataLog gaps (>{config.DATALOG_EXPECTED_INTERVAL_MIN*2:.0f} min): {len(gaps)}")
    if not gaps.empty:
        for frm, to, dmin in gaps[["from", "to", "duration_min"]].head(5).itertuples(index=False, name=None):
            say(f"    {frm} -> {to}  ({dmin:.1f} min)")

    # Baseline-deviation energy alerts (roadmap item 19): each complete day's
    # own hourly profile against the weekday/weekend baseline
    # (pcss.stats.weekday_weekend_profiles, shared with the wk dashboard
    # card). This flags a deviation from the recorded baseline, never a
    # fault — the wording says so explicitly.
    baseline = detect_baseline_deviations(energy_df)
    if baseline["status"] == "insufficient_history":
        say(f"  Baseline-deviation check: not enough history yet "
            f"({baseline['n_days']}/{baseline['min_days']:.0f} days).")
    else:
        flagged = baseline["flagged"]
        say(f"  {_count(len(flagged), 'day deviates', 'days deviate')} from the recorded baseline "
            f"(threshold {baseline['deviation_pct']:g}%):")
        for d, day_type, pct in flagged[["date", "day_type", "deviation_pct"]].itertuples(index=False, name=None):
            say(f"    {d} ({day_type}): {pct:.0f}% deviation")

    # Event-derived outage spans are authoritative when the EventLog parsed;
    # the DataLog inference stays as the fallback (and as a cross-check).
    if not ev_spans.empty:
        episodes = ev_spans.copy()
        if episodes["end"].isna().any() and not events_df.empty:
            episodes["end"] = episodes["end"].fillna(events_df["ts"].iloc[-1])
    else:
        episodes = on_battery

    # Guarded like the weekly-digest call site below: the alerts.log append can
    # fail (disk full, an AV lock on the file), and that must never crash a run
    # whose analysis already completed — the dashboard has not been written
    # yet at this point, so an unguarded raise here would lose the page too.
    try:
        alert_path = _maybe_write_alerts(voltage_anomalies, high_load, episodes, staleness,
                                         baseline, now)
    except Exception as e:
        alert_path = None
        print(
            f"[warn] alert append failed and was skipped this run: {type(e).__name__}: {e}\n"
            "       The analyzer run continues normally and still writes the dashboard.",
            file=sys.stderr,
        )
    if alert_path:
        say(f"  [alert] appended to {alert_path}")

    # Grid-quality trend (roadmap item 28): the envelope violations and
    # interruption episodes above, aggregated per calendar month with a rate
    # per recorded day so gap-heavy months read honestly. `episodes` is the
    # resolved authoritative-or-inferred frame from just above, so the
    # interruption counts share the dashboard episode strips' precedence.
    grid_quality = grid_quality_trend(datalog_df, gaps, episodes)

    section("GRID QUALITY TREND")
    if grid_quality.empty:
        say("  No data to trend grid quality yet.")
    else:
        say(f"  Events visible at the {config.DATALOG_EXPECTED_INTERVAL_MIN:g}-min "
            "sampling cadence; short events between samples are missed.")
        for row in grid_quality.itertuples(index=False):
            rate_txt = (f"{row.events_per_recorded_day:.2f}/recorded day"
                        if pd.notna(row.events_per_recorded_day) else "rate n/a")
            say(f"    {row.month}: sags={row.sag_count}  swells={row.swell_count}  "
                f"interruptions={row.interruption_count}  "
                f"({row.recorded_days:.1f} recorded days, {rate_txt})")
            if pd.notna(row.worst_event_ts):
                say(f"      worst: {row.worst_event_v:.1f} V "
                    f"({row.worst_event_direction}) at {row.worst_event_ts}")

    section("CROSS-VALIDATION (DataLog vs energylog)")
    crossval = cross_validate_load(datalog_df, energy_df) if not energy_df.empty else {}
    if crossval:
        say(f"  Paired samples       : {crossval['n_pairs']}")
        say(f"  DataLog mean load    : {crossval['datalog_mean_pct']:.2f}%")
        say(f"  energylog mean load  : {crossval['energylog_mean_pct']:.2f}%")
        say(f"  Mean abs error       : {crossval['mean_abs_error_pct']:.2f}%")
        say(f"  Max abs error        : {crossval['max_abs_error_pct']:.2f}%")
    else:
        say("  Not enough data to cross-validate.")

    section("RUNTIME ESTIMATE")
    if not energy_df.empty and energy_df["power_w"].notna().any():
        latest_w = float(energy_df["power_w"].dropna().iloc[-1])
        latest_rt = estimate_runtime(latest_w)
        say(f"  Latest power reading : {latest_w:.0f} W")
        say(f"  Estimated runtime    : {latest_rt:.1f} min if outage happens now")
        for label, w in [("Idle", 150), ("Moderate", 250), ("Gaming", 500), ("Peak", 600)]:
            say(f"  At {label:<10s} ({w} W): {estimate_runtime(w):.1f} min")
    else:
        say("  No power data yet.")

    # Runtime-curve calibration (roadmap item 16): the same structured result
    # that feeds the rt panel's measured overlay, reported here so a headless
    # or scheduled run surfaces the fit (or its honest below-floor status)
    # without opening the dashboard. calibration was computed once earlier from
    # the authoritative EventLog spans.
    section("RUNTIME-CURVE CALIBRATION")
    for _line in _calibration_console_lines(calibration):
        say(_line)

    # Self-test detection (roadmap item 18): event-based when the PCSS
    # self-test event id is known (still unobserved as of this writing; see
    # pcss.eventlog.SELF_TEST_EVENT_IDS), shape-based otherwise. Feeds two
    # things: the sag-under-load trend below, and an explicit mask on the
    # replace-by projection's fit, so the sawtooth these tests carve into
    # Battery Voltage never gets mistaken for real degradation.
    self_tests = detect_self_tests(datalog_df, events=events_df)
    sag_trend = self_test_sag_trend(self_tests)

    section("BATTERY SELF-TESTS")
    say(f"  Detected: {len(self_tests)}")
    if sag_trend["status"] == "trended":
        say(f"  Sag trend (voltage sag under test load): {sag_trend['slope_v_per_day']:+.4f} V/day "
            f"(median {sag_trend['median_sag_v']:.2f} V over {sag_trend['n_with_sag']} tests)")
    elif sag_trend["median_sag_v"] is not None:
        say(f"  Median sag so far: {sag_trend['median_sag_v']:.2f} V "
            f"(not enough test history to trend a rate; "
            f"{config.BATTERY_TREND_MIN_DAYS:.0f}+ days needed)")
    else:
        say("  No usable voltage-sag data yet.")

    battery = battery_replace_projection(datalog_df, annotations=annotations_df,
                                        self_tests=self_tests)

    section("BATTERY REPLACE-BY PROJECTION")
    if battery.get("battery_installed_on") is not None:
        say(f"  Battery installed: {battery['battery_installed_on']} "
            f"(age {battery['battery_age_days']:.0f} days)")
    if battery["status"] == "projected":
        say(f"  Trend (rolling-median fit): {battery['slope_v_per_day']:+.4f} V/day")
        say(f"  Crosses {battery['threshold_v']:g} V around {battery['replace_date']:%Y-%m-%d} "
            f"(~{battery['days_to_replace']:.0f} days)")
    elif battery["status"] == "stable":
        say(f"  Trend (rolling-median fit): {battery['slope_v_per_day']:+.4f} V/day — stable, "
            "no projection needed.")
    else:
        say(f"  Not enough history ({config.BATTERY_TREND_MIN_DAYS:.0f}+ days needed); "
            "the archive accumulates it over time.")

    # Weekly digest (roadmap item 32): a cadence-driven summary alongside the
    # event-driven alerts above, gated to once per ISO week. Runs after every
    # number it summarizes has been computed, using the same injected wall
    # clock (item 31) the staleness watchdog already read.
    #
    # Guarded: the alerts.log append or the marker write can fail (disk full,
    # an AV lock on the file), and that must never crash a run whose analysis
    # already completed successfully. If only the append failed, the marker
    # is untouched and the digest is retried next run; if the append
    # succeeded but the marker write then failed, the digest fires again next
    # run too — a rare, acceptable duplicate line in alerts.log, not a
    # crash.
    try:
        digest_path = _maybe_write_weekly_digest(energy_summary, forecast, voltage_anomalies,
                                                 episodes, battery, now, staleness=staleness)
    except Exception as e:
        digest_path = None
        print(
            f"[warn] weekly digest failed and was skipped this run: {type(e).__name__}: {e}\n"
            "       The analyzer run continues normally; see the comment above "
            "_maybe_write_weekly_digest's call site for the retry/duplicate behavior.",
            file=sys.stderr,
        )
    if digest_path:
        say(f"  [alert] weekly digest appended to {digest_path}")

    stats_table = compute_stats_summary(datalog_df)

    section("PER-METRIC STATISTICS (DataLog)")
    if stats_table.empty:
        say("  No usable numeric columns.")
    else:
        say(stats_table.to_string(index=False))

    if args.json:
        _write_json_summary(args.json, sizes, dl_stats, hist_stats, energy_summary,
                            voltage_anomalies, high_load, on_battery, gaps, crossval,
                            archive_df, archive_added, battery,
                            events_df, ev_status, ev_spans, forecast, reconciled_bills,
                            grid_quality, calibration)
        say(f"  Wrote JSON summary to {args.json}")

    events_summary = None
    if ev_status == "ok" and not events_df.empty:
        events_summary = {
            "n": int(len(events_df)),
            "on_battery": int(len(ev_spans)),
            "last_name": str(events_df["name"].iloc[-1]),
            "last_ts": events_df["ts"].iloc[-1],
        }

    # Payload budget for multi-year archives (roadmap item 25): everything
    # above this point — console output, --json, alerts, the archive append,
    # and every fitted stats surface (battery, forecast, reconciled_bills,
    # grid_quality, ...) — has already run against the full history. From
    # here on, only what the dashboard visualizes may be narrowed, and only
    # the raw per-sample frames that actually scale with the archive's size:
    # DataLog, energylog, the size-history growth series, the self-test
    # markers, and the gap/anomaly/episode overlays that ride alongside them
    # on the same time panels. dash_cutoff is None (the default) whenever
    # nothing should change, so every dash_* frame below is simply the
    # original frame — this is what keeps max_days = 0 byte-identical.
    #
    # energy_summary itself is also re-derived from the windowed energy frame
    # rather than reused as-is: every energy-derived dashboard surface (the
    # kw/daily/cmp panels, the Energy & Cost section note, the Reference
    # summary rows, the Billing Periods table) is built from this one dict,
    # so recomputing it here is what makes those surfaces windowed too,
    # matching the footer's "showing the last N days" claim page-wide.
    # compute_energy_summary's own partial-period labeling then applies
    # naturally to a period the window has truncated the start of (for
    # example the previous billing period on the cmp panel) — no extra code
    # needed for that to read honestly.
    dash_cutoff = _dashboard_window(datalog_df, config.DASHBOARD_MAX_DAYS)
    if dash_cutoff is None:
        dash_datalog_df, dash_energy_df, dash_hist = datalog_df, energy_df, hist
        dash_gaps, dash_voltage_anomalies = gaps, voltage_anomalies
        dash_high_load, dash_episodes = high_load, episodes
        dash_energy_summary, dash_self_tests = energy_summary, self_tests
        dash_events = events_df
        dashboard_window_days = None
    else:
        dash_datalog_df = _window_df(datalog_df, "ts", dash_cutoff)
        dash_energy_df = _window_df(energy_df, "ts", dash_cutoff)
        dash_hist = _window_df(hist, "timestamp", dash_cutoff)
        # The event timeline (roadmap item 20) is a per-sample surface too, so
        # it follows the same window as the DataLog/energylog frames.
        dash_events = _window_df(events_df, "ts", dash_cutoff)
        # gaps/high-load episodes/on-battery episodes are spans: keep an
        # entry whose end reaches into the window even if it started before
        # the cutoff, so the still-visible portion renders instead of the
        # whole entry being dropped (roadmap item 25 review finding 4).
        dash_gaps = _window_df(gaps, "from", dash_cutoff, end_col="to")
        dash_voltage_anomalies = _window_df(voltage_anomalies, "ts", dash_cutoff)
        dash_high_load = _window_df(high_load, "start", dash_cutoff, end_col="end")
        dash_episodes = _window_df(episodes, "start", dash_cutoff, end_col="end")
        dash_self_tests = _window_df(self_tests, "ts", dash_cutoff)
        dash_energy_summary = (compute_energy_summary(dash_energy_df)
                               if dash_energy_df is not None and not dash_energy_df.empty
                               else {})
        dashboard_window_days = config.DASHBOARD_MAX_DAYS

    section("DASHBOARD")
    html = build_dashboard(
        dash_datalog_df, dash_energy_df, dash_hist, dl_stats, hist_stats, sizes,
        dash_energy_summary,
        stats_table, dash_gaps, dash_voltage_anomalies, dash_high_load, crossval, dash_episodes,
        battery, events_summary, staleness, forecast, reconciled_bills, annotations_df,
        calibration, self_tests=dash_self_tests, baseline=baseline,
        grid_quality=grid_quality, dashboard_window_days=dashboard_window_days,
        events=dash_events,
    )
    config.DASHBOARD_HTML.write_text(html, encoding="utf-8")
    say(f"  Wrote {config.DASHBOARD_HTML}")
    if args.no_browser:
        say("  (--no-browser: not opening)")
    else:
        say("  Opening in browser...")
        try:
            webbrowser.open(config.DASHBOARD_HTML.as_uri())
        except Exception as e:
            say(f"  (couldn't auto-open: {e})")
    return 0


def _periods_for_json(monthly: pd.DataFrame) -> list[dict]:
    """The per-billing-period breakdown, tagged with which rates priced each
    period — only meaningful (and only called) once tariff history is in
    play, so a rate boundary mid-history does not look like a consumption
    change to whatever reads --json."""
    cols = ["month", "kwh", "cost_pcss", "cost_tiered", "co2_kg", "partial", "rate_tag"]
    return [
        {"month": month, "kwh": kwh, "cost_pcss": cost_pcss, "cost_tiered": cost_tiered,
         "co2_kg": co2_kg, "partial": bool(partial), "rate_tag": rate_tag}
        for month, kwh, cost_pcss, cost_tiered, co2_kg, partial, rate_tag
        in monthly[cols].itertuples(index=False, name=None)
    ]


def _bills_for_json(reconciled: pd.DataFrame) -> list[dict]:
    """The reconciled-bills table (roadmap item 29) as plain dicts for
    --json — only meaningful (and only called) once at least one bill
    reconciles, so the key is simply absent otherwise."""
    cols = ["period", "ups_kwh", "billed_kwh", "share_pct", "ups_cost_tiered",
            "billed_amount_crc", "implied_rate_crc_per_kwh",
            "tariff_low", "tariff_high", "tariff_flat", "rate_tag", "partial"]
    return [
        {"period": period, "ups_kwh": ups_kwh, "billed_kwh": billed_kwh,
         "share_pct": share_pct, "ups_cost_tiered": ups_cost_tiered,
         "billed_amount_crc": billed_amount_crc,
         "implied_rate_crc_per_kwh": implied_rate, "tariff_low": tariff_low,
         "tariff_high": tariff_high, "tariff_flat": tariff_flat, "rate_tag": rate_tag,
         "partial": bool(partial)}
        for (period, ups_kwh, billed_kwh, share_pct, ups_cost_tiered, billed_amount_crc,
             implied_rate, tariff_low, tariff_high, tariff_flat, rate_tag, partial)
        in reconciled[cols].itertuples(index=False, name=None)
    ]


def _grid_quality_for_json(gq: pd.DataFrame) -> dict:
    """The per-month grid-quality trend (roadmap item 28) for --json — only
    meaningful (and only called) once at least one month has data, so the
    key is simply absent otherwise. The cadence-honesty label rides this
    machine-readable surface too: cadence_min carries the configured
    datalog_expected_interval_min and the note says these are events visible
    at that cadence, the same caveat the console and the dashboard state. A
    NaN means "no events in that direction this month" (or "no span to rate
    against") and becomes null rather than a non-standard NaN token. Rows
    that mix a real worst-event month with an event-less one make pandas
    coerce worst_event_ts to datetime64, so an event-less month's value
    arrives as pd.NaT rather than None — `pd.isna` catches both NaN and
    NaT, unlike the narrower `isinstance(v, float) and pd.isna(v)`."""
    records = []
    for row in gq.itertuples(index=False):
        d = row._asdict()
        for k, v in d.items():
            if pd.isna(v):
                d[k] = None
        records.append(d)
    return {
        "cadence_min": config.DATALOG_EXPECTED_INTERVAL_MIN,
        "note": ("events visible at the sampling cadence; "
                 "short events between samples are missed"),
        "months": records,
    }


def _write_json_summary(path: Path, sizes, dl_stats, hist_stats, energy_summary,
                        voltage_anomalies, high_load, on_battery, gaps, crossval,
                        archive_df, archive_added, battery,
                        events_df, ev_status, ev_spans, forecast=None,
                        reconciled_bills=None, grid_quality=None, calibration=None) -> None:
    """Structured machine-readable summary for external tooling (--json)."""
    energy_json = {k: energy_summary.get(k) for k in
                   ("total_kwh", "total_cost_pcss", "total_cost_tiered", "total_co2_kg",
                    "n_samples", "first", "last", "interval_sec")} if energy_summary else {}
    if energy_summary and energy_summary.get("tariff_history_active"):
        energy_json["periods"] = _periods_for_json(energy_summary["monthly"])
    summary = {
        "sizes_bytes": sizes,
        "total_bytes": sum(sizes.values()),
        "datalog": {k: dl_stats.get(k) for k in
                    ("n_entries", "first", "last", "median_interval_sec", "span_days",
                     "daily_bytes", "yearly_bytes")} if dl_stats else {},
        "archive": {
            "rows": int(len(archive_df)),
            "first": archive_df["ts"].iloc[0] if not archive_df.empty else None,
            "last": archive_df["ts"].iloc[-1] if not archive_df.empty else None,
            "added": int(archive_added),
        },
        "growth": {k: hist_stats.get(k) for k in
                   ("snapshots", "bytes_per_day", "first_ts", "last_ts")} if hist_stats else {},
        "energy": energy_json,
        "anomalies": {
            "voltage_out_of_envelope": int(len(voltage_anomalies)),
            "high_load_episodes": int(len(high_load)),
            "on_battery_episodes": int(len(on_battery)),
            "datalog_gaps": int(len(gaps)),
        },
        "cross_validation": crossval or {},
        "battery": battery or {},
        "forecast": forecast or {},
        "calibration": _calibration_for_json(calibration),
        "events": {
            "status": ev_status,
            "n_events": int(len(events_df)),
            "first": events_df["ts"].iloc[0] if not events_df.empty else None,
            "last": events_df["ts"].iloc[-1] if not events_df.empty else None,
            "on_battery_events": int(len(ev_spans)),
        },
    }
    if reconciled_bills is not None and not reconciled_bills.empty:
        summary["bills"] = _bills_for_json(reconciled_bills)
    if grid_quality is not None and not grid_quality.empty:
        summary["grid_quality"] = _grid_quality_for_json(grid_quality)
    Path(path).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")


def _maybe_write_alerts(voltage_anomalies, high_load, on_battery, staleness=None,
                        baseline=None, now=None) -> Path | None:
    """Opt-in (config [alerts] enabled): append a timestamped line to
    alerts.log when the analyzed window has voltage anomalies, sustained
    high-load, inferred on-battery episodes, a stale data feed, or days that
    deviate from the recorded baseline (roadmap item 19,
    pcss.stats.detect_baseline_deviations — a deviation flag, never a fault
    claim). The tray process watches this file and raises a notification for
    new lines; email stays an extension point.

    The line is stamped with `now`, the pipeline's single wall-clock read
    (roadmap item 31), rather than a fresh pd.Timestamp.now() call, so the
    alert, the staleness watchdog, and the weekly digest all agree on one
    "now". A None `now` (a direct caller that does not thread it through)
    falls back to the wall clock."""
    if not config.ALERTS_ENABLED:
        return None
    n_v, n_h, n_ob = len(voltage_anomalies), len(high_load), len(on_battery)
    n_bd = 0 if baseline is None else len(baseline.get("flagged", []))
    stale_level = (staleness or {}).get("level", "fresh")
    if n_v == 0 and n_h == 0 and n_ob == 0 and n_bd == 0 and stale_level == "fresh":
        return None
    stale_bit = (f"  stale={stale_level}({staleness['age_hours']:.1f}h)"
                if stale_level != "fresh" else "")
    stamp = now if now is not None else pd.Timestamp.now()
    line = (f"{stamp:%Y-%m-%d %H:%M:%S}  "
            f"voltage_anomalies={n_v}  high_load_episodes={n_h}  "
            f"on_battery_episodes={n_ob}  baseline_deviations={n_bd}{stale_bit}\n")
    with config.ALERTS_LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    return config.ALERTS_LOG


# ======================================================================
# Weekly digest (roadmap item 32)
# ======================================================================
def _iso_year_week(ts) -> tuple[int, int]:
    """(ISO year, ISO week number) for a date/datetime/Timestamp.

    The ISO year can differ from the calendar year for a few days around
    New Year's (for example 2025-12-29 is ISO week 1 of 2026), which is
    exactly why the gate below compares this pair rather than the calendar
    year: ordinary tuple comparison of (iso_year, iso_week) already orders
    correctly across that boundary.
    """
    iso = pd.Timestamp(ts).isocalendar()
    return int(iso.year), int(iso.week)


def _format_digest_marker(today) -> str:
    """The marker text for output/last_digest.txt: "YYYY-Www", the ISO
    (year, week) that a digest was last actually appended for."""
    year, week = _iso_year_week(today)
    return f"{year:04d}-W{week:02d}"


def _parse_digest_marker(text: str | None) -> tuple[int, int] | None:
    """Parse the ISO (year, week) recorded in output/last_digest.txt.

    Returns None for a missing file (text is None), a blank file, or
    content that does not match the "YYYY-Www" shape written by
    _format_digest_marker — the caller's gate treats all three the same
    way, as "no digest recorded yet", so it fires on the next run.
    """
    if not text or not text.strip():
        return None
    first_line = text.splitlines()[0].strip()
    year_part, sep, week_part = first_line.partition("-W")
    if not sep or not year_part.isdigit() or not week_part.isdigit():
        return None
    return int(year_part), int(week_part)


def _should_fire_weekly_digest(marker_text: str | None, today) -> bool:
    """True when `today` falls in a newer ISO week than the one recorded by
    the digest marker (roadmap item 32) — the gate that turns "the analyzer
    runs every day" into "the digest fires once, on the first run of a new
    week." A missing or malformed marker fires unconditionally, the same
    "nothing recorded yet, so it's due" rule scheduled_run.ps1's own
    once-a-day marker follows; a same-week rerun is always a no-op, and a
    marker that is somehow ahead of today (clock skew) does not fire either,
    since today's week is not newer than what is already recorded.
    """
    parsed = _parse_digest_marker(marker_text)
    if parsed is None:
        return True
    return _iso_year_week(today) > parsed


def _write_weekly_digest_marker(marker_path: Path, today) -> None:
    """Write output/last_digest.txt atomically.

    The new content goes to a sibling temp file first, then an OS-level
    rename replaces the real marker in one step, so a crash mid-write (or a
    second process reading at the same moment) never observes a
    half-written marker — either the old content or the new one, never
    something in between.
    """
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = marker_path.with_name(marker_path.name + ".tmp")
    tmp_path.write_text(_format_digest_marker(today), encoding="utf-8")
    try:
        os.replace(tmp_path, marker_path)
    finally:
        # A successful replace already moved tmp_path away, so this is a
        # no-op on the happy path; a failed replace (disk full, an AV lock)
        # would otherwise leave the sibling .tmp file behind on disk forever.
        tmp_path.unlink(missing_ok=True)


def _weekly_digest_data(energy_summary: dict, forecast: dict,
                        voltage_anomalies: pd.DataFrame, episodes: pd.DataFrame,
                        battery: dict, now: pd.Timestamp,
                        staleness: dict | None = None) -> dict:
    """Reduce the pipeline's already-computed summaries to the plain values
    the weekly digest line is worded from (roadmap item 32). No statistic is
    computed here — everything is picked straight out of energy_summary
    (compute_energy_summary), forecast (forecast_period_cost), the anomaly
    and episode frames already detected earlier in main(), battery
    (battery_replace_projection), and staleness (pcss.stats.assess_staleness,
    item 31's watchdog, already computed once per run); the two "_7d" counts
    are filtered to the trailing 7 days ending at `now`, which is the only
    new work this function does.
    """
    data: dict = {
        "period_kwh": None, "period_cost_tiered": None, "period_partial": False,
        "forecast_kwh": None, "forecast_cost_tiered": None, "forecast_period_end": None,
        "voltage_anomalies_7d": 0, "episodes_7d": 0,
        "battery_status": None, "battery_replace_date": None,
        "biggest_day_date": None, "biggest_day_kwh": None,
        "stale_level": (staleness or {}).get("level", "fresh"),
        "stale_age_hours": (staleness or {}).get("age_hours", 0.0),
    }

    monthly = energy_summary.get("monthly") if energy_summary else None
    if monthly is not None and not monthly.empty:
        last = monthly.iloc[-1]
        data["period_kwh"] = float(last["kwh"])
        data["period_cost_tiered"] = float(last["cost_tiered"])
        data["period_partial"] = bool(last["partial"])

    if forecast and forecast.get("status") == "projected":
        data["forecast_kwh"] = forecast["projected_kwh"]
        data["forecast_cost_tiered"] = forecast["projected_cost_tiered"]
        data["forecast_period_end"] = forecast["period_end"]

    week_ago = now - pd.Timedelta(days=7)
    if voltage_anomalies is not None and not voltage_anomalies.empty:
        data["voltage_anomalies_7d"] = int((voltage_anomalies["ts"] >= week_ago).sum())
    if episodes is not None and not episodes.empty:
        # An episode counts toward the trailing window when its end falls
        # inside it, the same "still-visible tail" rule _window_df already
        # applies elsewhere to a span straddling a cutoff.
        data["episodes_7d"] = int((episodes["end"] >= week_ago).sum())

    if battery:
        data["battery_status"] = battery.get("status")
        data["battery_replace_date"] = battery.get("replace_date")

    daily = energy_summary.get("daily") if energy_summary else None
    if daily is not None and not daily.empty:
        cutoff_date = week_ago.date()
        recent = daily[daily["date"] >= cutoff_date]
        if not recent.empty:
            idx = recent["kwh"].idxmax()
            data["biggest_day_date"] = recent.loc[idx, "date"]
            data["biggest_day_kwh"] = float(recent.loc[idx, "kwh"])

    return data


def _build_weekly_digest_line(data: dict, now: pd.Timestamp) -> str:
    """Assemble the one-line weekly digest (roadmap item 32) from the dict
    _weekly_digest_data produces.

    Pure string formatting: no statistic is computed here either, only
    chosen and worded. A clause whose underlying number is unavailable is
    left out entirely rather than guessed or shown as a placeholder, so the
    line never claims more than the analyzer actually knows this run.
    """
    clauses: list[str] = []

    if data.get("period_kwh") is not None:
        partial = " (partial)" if data.get("period_partial") else ""
        clauses.append(
            f"period={data['period_kwh']:.2f} kWh / "
            f"CRC {data['period_cost_tiered']:,.2f} tiered so far this period{partial}"
        )

    if data.get("forecast_kwh") is not None:
        clauses.append(
            f"forecast=~{data['forecast_kwh']:.2f} kWh / "
            f"CRC {data['forecast_cost_tiered']:,.2f} tiered "
            f"by {data['forecast_period_end']:%Y-%m-%d} (projected)"
        )

    clauses.append(f"voltage_anomalies_7d={data.get('voltage_anomalies_7d', 0)}")
    clauses.append(f"on_battery_episodes_7d={data.get('episodes_7d', 0)}")

    if data.get("battery_status") == "stable":
        clauses.append("battery=stable")
    elif data.get("battery_status") == "projected" and data.get("battery_replace_date") is not None:
        clauses.append(f"battery=replace ~{data['battery_replace_date']:%Y-%m-%d}")

    if data.get("biggest_day_date") is not None:
        clauses.append(
            f"biggest_day_7d={data['biggest_day_date']:%Y-%m-%d} "
            f"({data['biggest_day_kwh']:.2f} kWh)"
        )

    # A staleness caveat (item 31's watchdog), so a clean-week reading is not
    # mistaken for current data when the collection itself has lagged —
    # omitted entirely when the feed is fresh, the same "say nothing extra"
    # rule every other optional clause here follows.
    stale_level = data.get("stale_level", "fresh")
    if stale_level != "fresh":
        clauses.append(f"stale_data={stale_level}({data.get('stale_age_hours', 0.0):.1f}h)")

    return f"{now:%Y-%m-%d %H:%M:%S}  weekly_digest  " + "  ".join(clauses) + "\n"


def _maybe_write_weekly_digest(energy_summary: dict, forecast: dict,
                                voltage_anomalies: pd.DataFrame, episodes: pd.DataFrame,
                                battery: dict, now: pd.Timestamp,
                                staleness: dict | None = None) -> Path | None:
    """Opt-in (config [alerts] enabled AND [alerts] weekly_digest): once per
    ISO week, append one compact summary line to alerts.log alongside
    whatever event-driven anomaly line _maybe_write_alerts may also have
    written this run (roadmap item 32). alerts.log is the digest's only
    transport — the tray's AlertWatcher toast and item 23's webhook channel
    then deliver it for free, with no new transport added here. The gate is
    output/last_digest.txt (config.LAST_DIGEST_MARKER), advanced only after
    the line is actually appended, so a rerun later the same week is a
    no-op and a missing marker fires on the next eligible run.
    """
    if not (config.ALERTS_ENABLED and config.WEEKLY_DIGEST_ENABLED):
        return None
    try:
        marker_text = config.LAST_DIGEST_MARKER.read_text(encoding="utf-8")
    except OSError:
        marker_text = None
    if not _should_fire_weekly_digest(marker_text, now):
        return None
    data = _weekly_digest_data(energy_summary, forecast, voltage_anomalies, episodes,
                               battery, now, staleness=staleness)
    line = _build_weekly_digest_line(data, now)
    with config.ALERTS_LOG.open("a", encoding="utf-8") as f:
        f.write(line)
    _write_weekly_digest_marker(config.LAST_DIGEST_MARKER, now)
    return config.ALERTS_LOG


if __name__ == "__main__":
    raise SystemExit(main())
