"""PowerChute Serial Shutdown UPS log analyzer — CLI entry point.

Thin orchestrator over the `pcss` package: load the three PCSS logs, compute
stats / anomalies / energy / cross-validation, snapshot file sizes, build the
Plotly dashboard, and write/open the HTML. (Phase 2 adds the argparse CLI.)
"""
from __future__ import annotations

import webbrowser

import pandas as pd

from pcss.animation import _inject_controls_into_html
from pcss.animation import _replay_metadata as _replay_metadata  # noqa: F401  re-export for tests
from pcss.common import fmt_bytes
from pcss.config import (
    DASHBOARD_HTML,
    DATALOG,
    DATALOG_EXPECTED_INTERVAL_MIN,
    ENERGYLOG_DIR,
    EVENTLOG,
    HIGH_LOAD_PCT,
    SIZE_HISTORY_CSV,
    VOLTAGE_NORMAL_HIGH,
    VOLTAGE_NORMAL_LOW,
)
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


# ======================================================================
# Console reporter
# ======================================================================
def banner(s: str):
    print()
    print("=" * 72)
    print(s)
    print("=" * 72)


def main():
    sizes = {
        "DataLog": DATALOG.stat().st_size if DATALOG.exists() else 0,
        "EventLog (binary)": EVENTLOG.stat().st_size if EVENTLOG.exists() else 0,
        "energylog/": sum(f.stat().st_size for f in ENERGYLOG_DIR.glob("*")) if ENERGYLOG_DIR.exists() else 0,
    }

    banner("PCSS LOG FILES — CURRENT SIZES")
    for name, sz in sizes.items():
        print(f"  {name:25s} {fmt_bytes(sz):>10s}  ({sz:,} bytes)")
    print(f"  {'TOTAL':25s} {fmt_bytes(sum(sizes.values())):>10s}")

    hist = record_size_snapshot(sizes)
    hist_stats = history_summary(hist)

    banner("SIZE-HISTORY SNAPSHOTS")
    print(f"  History file: {SIZE_HISTORY_CSV}")
    print(f"  Snapshots so far: {len(hist)}")
    if hist_stats:
        print(f"  First snapshot : {hist_stats['first_ts']}")
        print(f"  Last snapshot  : {hist_stats['last_ts']}")
        print(f"  Total grew     : {fmt_bytes(hist_stats['delta_bytes'])} "
              f"({hist_stats['first_total']:,} -> {hist_stats['last_total']:,} bytes)")
        print(f"  Rate           : {fmt_bytes(hist_stats['bytes_per_minute'])}/min, "
              f"{fmt_bytes(hist_stats['bytes_per_hour'])}/hour, "
              f"{fmt_bytes(hist_stats['bytes_per_day'])}/day")
    else:
        print("  Run again later to see growth between snapshots.")

    datalog_df = load_datalog()
    dl_stats = datalog_stats(datalog_df, sizes["DataLog"])

    banner("DATALOG SUMMARY")
    if datalog_df.empty:
        print("  No DataLog rows yet.")
    else:
        print(f"  First sample   : {dl_stats['first']}")
        print(f"  Last sample    : {dl_stats['last']}")
        print(f"  Entries        : {dl_stats['n_entries']}")
        print(f"  Median interval: {dl_stats['median_interval_sec']:.0f} s "
              f"(~{dl_stats['median_interval_sec']/60:.1f} min)")
        print(f"  Bytes/entry    : {dl_stats['bytes_per_entry']:.1f}")
        print()
        print(f"  Projected disk usage (over {dl_stats['span_days']:.2f} days of data):")
        print(f"    Per minute  : {fmt_bytes(dl_stats['minute_bytes'])}")
        print(f"    Per day     : {fmt_bytes(dl_stats['daily_bytes'])}")
        print(f"    Per month   : {fmt_bytes(dl_stats['monthly_bytes'])}")
        print(f"    Per year    : {fmt_bytes(dl_stats['yearly_bytes'])}")

    energy_df, energy_metas = load_energylog()
    energy_summary = compute_energy_summary(energy_df) if not energy_df.empty else {}

    banner("ENERGY LOG SUMMARY")
    if energy_df.empty:
        print("  energylog/ empty or unparseable.")
    else:
        for m in energy_metas:
            print(f"  {m.month}: {m.n_samples:>5d} samples, interval={m.interval_sec}s, max_load={m.max_load_w:.0f}W")
        print()
        print(f"  Total samples       : {energy_summary['n_samples']}")
        print(f"  Span                : {energy_summary['first']} -> {energy_summary['last']}")
        print(f"  Total energy        : {energy_summary['total_kwh']:.4f} kWh")
        print(f"  Cost @ PCSS flat    : CRC {energy_summary['total_cost_pcss']:,.2f}")
        print(f"  Cost @ Coop. tiered : CRC {energy_summary['total_cost_tiered']:,.2f}")
        print(f"  CO2 emitted         : {energy_summary['total_co2_kg']:.4f} kg")
        if energy_summary["monthly"] is not None and not energy_summary["monthly"].empty:
            print()
            print("  Monthly breakdown:")
            for _, r in energy_summary["monthly"].iterrows():
                print(f"    {r['month']}: {r['kwh']:>9.4f} kWh   "
                      f"PCSS=CRC {r['cost_pcss']:>10,.2f}   "
                      f"Tiered=CRC {r['cost_tiered']:>10,.2f}   "
                      f"CO2={r['co2_kg']:>7.4f} kg")

    banner("ANOMALIES & EVENTS")
    voltage_anomalies = detect_voltage_anomalies(datalog_df)
    print(f"  Voltage out of {VOLTAGE_NORMAL_LOW}-{VOLTAGE_NORMAL_HIGH}V envelope: "
          f"{len(voltage_anomalies)} samples")
    if not voltage_anomalies.empty:
        for _, r in voltage_anomalies.head(5).iterrows():
            print(f"    {r['ts']}  {r['Line Voltage']} V")
        if len(voltage_anomalies) > 5:
            print(f"    ... ({len(voltage_anomalies)-5} more)")

    high_load = detect_high_load_episodes(energy_df) if not energy_df.empty else pd.DataFrame()
    print(f"  Sustained high-load episodes (>={HIGH_LOAD_PCT}%, >=10min): {len(high_load)}")
    if not high_load.empty:
        for _, r in high_load.head(5).iterrows():
            print(f"    {r['start']} -> {r['end']}  {r['duration_min']:.1f}min  peak {r['peak_pct']:.0f}% / {r['peak_w']:.0f}W")
        if len(high_load) > 5:
            print(f"    ... ({len(high_load)-5} more)")

    gaps = detect_gaps(datalog_df)
    print(f"  DataLog gaps (>{DATALOG_EXPECTED_INTERVAL_MIN*2:.0f} min): {len(gaps)}")
    if not gaps.empty:
        for _, r in gaps.head(5).iterrows():
            print(f"    {r['from']} -> {r['to']}  ({r['duration_min']:.1f} min)")

    banner("CROSS-VALIDATION (DataLog vs energylog)")
    crossval = cross_validate_load(datalog_df, energy_df) if not energy_df.empty else {}
    if crossval:
        print(f"  Paired samples       : {crossval['n_pairs']}")
        print(f"  DataLog mean load    : {crossval['datalog_mean_pct']:.2f}%")
        print(f"  energylog mean load  : {crossval['energylog_mean_pct']:.2f}%")
        print(f"  Mean abs error       : {crossval['mean_abs_error_pct']:.2f}%")
        print(f"  Max abs error        : {crossval['max_abs_error_pct']:.2f}%")
    else:
        print("  Not enough data to cross-validate.")

    banner("RUNTIME ESTIMATE")
    if not energy_df.empty and energy_df["power_w"].notna().any():
        latest_w = float(energy_df["power_w"].dropna().iloc[-1])
        latest_rt = estimate_runtime(latest_w)
        print(f"  Latest power reading : {latest_w:.0f} W")
        print(f"  Estimated runtime    : {latest_rt:.1f} min if outage happens now")
        for label, w in [("Idle", 150), ("Moderate", 250), ("Gaming", 500), ("Peak", 600)]:
            print(f"  At {label:<10s} ({w} W): {estimate_runtime(w):.1f} min")
    else:
        print("  No power data yet.")

    stats_table = compute_stats_summary(datalog_df)

    banner("PER-METRIC STATISTICS (DataLog)")
    if stats_table.empty:
        print("  No usable numeric columns.")
    else:
        print(stats_table.to_string(index=False))

    banner("DASHBOARD")
    fig, animations = build_dashboard(
        datalog_df, energy_df, hist, dl_stats, hist_stats, sizes, energy_summary,
        stats_table, gaps, voltage_anomalies, high_load, crossval,
    )
    html = fig.to_html(include_plotlyjs="cdn", full_html=True)
    html = _inject_controls_into_html(html, animations)
    DASHBOARD_HTML.write_text(html, encoding="utf-8")
    print(f"  Wrote {DASHBOARD_HTML}")
    print("  Opening in browser...")
    try:
        webbrowser.open(DASHBOARD_HTML.as_uri())
    except Exception as e:
        print(f"  (couldn't auto-open: {e})")


if __name__ == "__main__":
    main()
