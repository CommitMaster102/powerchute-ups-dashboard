"""PowerChute Serial Shutdown UPS log analyzer — CLI entry point.

Thin orchestrator over the `pcss` package: load the three PCSS logs, compute
stats / anomalies / energy / cross-validation, snapshot file sizes, build the
Plotly dashboard, and write/open the HTML.

Run with no arguments to reproduce the classic behavior (console summary +
output/dashboard.html, opens browser). See `--help` for flags.
"""
from __future__ import annotations

import argparse
import json
import webbrowser
from pathlib import Path

import pandas as pd

from pcss import config
from pcss.animation import _inject_controls_into_html
from pcss.animation import _replay_metadata as _replay_metadata  # noqa: F401  re-export for tests
from pcss.common import fmt_bytes
from pcss.dashboard import build_dashboard
from pcss.loaders import (
    history_summary,
    load_datalog,
    load_energylog,
    record_size_snapshot,
)
from pcss.stats import (
    compute_energy_summary,
    compute_stats_summary,
    cross_validate_load,
    datalog_stats,
    detect_gaps,
    detect_high_load_episodes,
    detect_voltage_anomalies,
    estimate_runtime,
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


def _date_filter(df: pd.DataFrame, since: pd.Timestamp | None, until: pd.Timestamp | None) -> pd.DataFrame:
    if df.empty or "ts" not in df.columns:
        return df
    if since is not None:
        df = df[df["ts"] >= since]
    if until is not None:
        df = df[df["ts"] <= until]
    return df.reset_index(drop=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    quiet = args.quiet
    used_cfg = config.load_config(args.config, agent_dir=args.agent_dir, output=args.output)

    def say(*a, **k):
        if not quiet:
            print(*a, **k)

    def section(title: str):
        if not quiet:
            banner(title)

    if args.verbose:
        say(f"  Config: {used_cfg or 'built-in defaults'}")
        say(f"  Agent dir: {config.PCSS_AGENT}")

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

    datalog_df = _date_filter(load_datalog(), since_ts, until_ts)
    dl_stats = datalog_stats(datalog_df, sizes["DataLog"])

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

    energy_df, energy_metas = load_energylog()
    energy_df = _date_filter(energy_df, since_ts, until_ts)
    energy_summary = compute_energy_summary(energy_df) if not energy_df.empty else {}

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
            say("  Monthly breakdown:")
            monthly = energy_summary["monthly"]
            for month, kwh, cost_pcss, cost_tiered, co2_kg in monthly[
                ["month", "kwh", "cost_pcss", "cost_tiered", "co2_kg"]
            ].itertuples(index=False, name=None):
                say(f"    {month}: {kwh:>9.4f} kWh   "
                    f"PCSS=CRC {cost_pcss:>10,.2f}   "
                    f"Tiered=CRC {cost_tiered:>10,.2f}   "
                    f"CO2={co2_kg:>7.4f} kg")

    section("ANOMALIES & EVENTS")
    voltage_anomalies = detect_voltage_anomalies(datalog_df)
    say(f"  Voltage out of {config.VOLTAGE_NORMAL_LOW}-{config.VOLTAGE_NORMAL_HIGH}V envelope: "
        f"{len(voltage_anomalies)} samples")
    if not voltage_anomalies.empty:
        for ts, volts in voltage_anomalies[["ts", "Line Voltage"]].head(5).itertuples(index=False, name=None):
            say(f"    {ts}  {volts} V")
        if len(voltage_anomalies) > 5:
            say(f"    ... ({len(voltage_anomalies)-5} more)")

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

    stats_table = compute_stats_summary(datalog_df)

    section("PER-METRIC STATISTICS (DataLog)")
    if stats_table.empty:
        say("  No usable numeric columns.")
    else:
        say(stats_table.to_string(index=False))

    if args.json:
        _write_json_summary(args.json, sizes, dl_stats, hist_stats, energy_summary,
                            voltage_anomalies, high_load, gaps, crossval)
        say(f"  Wrote JSON summary to {args.json}")

    section("DASHBOARD")
    fig, animations = build_dashboard(
        datalog_df, energy_df, hist, dl_stats, hist_stats, sizes, energy_summary,
        stats_table, gaps, voltage_anomalies, high_load, crossval,
    )
    html = fig.to_html(include_plotlyjs="cdn", full_html=True)
    html = _inject_controls_into_html(html, animations)
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


def _write_json_summary(path: Path, sizes, dl_stats, hist_stats, energy_summary,
                        voltage_anomalies, high_load, gaps, crossval) -> None:
    """Structured machine-readable summary for external tooling (--json)."""
    summary = {
        "sizes_bytes": sizes,
        "total_bytes": sum(sizes.values()),
        "datalog": {k: dl_stats.get(k) for k in
                    ("n_entries", "first", "last", "median_interval_sec", "span_days",
                     "daily_bytes", "yearly_bytes")} if dl_stats else {},
        "growth": {k: hist_stats.get(k) for k in
                   ("snapshots", "bytes_per_day", "first_ts", "last_ts")} if hist_stats else {},
        "energy": {k: energy_summary.get(k) for k in
                   ("total_kwh", "total_cost_pcss", "total_cost_tiered", "total_co2_kg",
                    "n_samples", "first", "last", "interval_sec")} if energy_summary else {},
        "anomalies": {
            "voltage_out_of_envelope": int(len(voltage_anomalies)),
            "high_load_episodes": int(len(high_load)),
            "datalog_gaps": int(len(gaps)),
        },
        "cross_validation": crossval or {},
    }
    Path(path).write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
