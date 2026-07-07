# Roadmap — candidate features and the challenges each one brings

This file collects features that would be worth adding to the analyzer and the
dashboard, together with the technical problems each one has to solve. Items
are grouped by theme and roughly ordered by value for effort inside each
group. When a feature touches the current architecture, the relevant files and
symbols are named so the entry stays actionable later.

How this file works: active candidates live at the top; finished items move,
whole and unedited, into the "Shipped — implementation archive" section at
the bottom of this same file, each with a SHIPPED note recording where the
implementation landed and which tests pin it. Numbering is continuous and
never reused, so a reference like "item 5" stays unambiguous forever. Before
proposing or starting anything here, read the archive first — it is the
record of what already exists, where it lives, and which design decisions
were already made (future agents: do not re-propose or re-implement archived
items; extend them).

Shipped so far (details in the archive at the bottom):

| # | Item | Landed in |
|---|---|---|
| 1 | Permalink view state | `pcss/charts.js` (`updateHash`/`restoreFromHash`) |
| 2 | Anomaly jump navigation | `pcss/charts.js` (`jumpAnomaly`) |
| 3 | Heatmap linked to the crosshair | `pcss/charts.js` (`highlightHeatmap`) |
| 4 | DataLog archiving | `pcss/loaders.py` (`append_datalog_archive` …) |
| 5 | EventLog parsing | `pcss/eventlog.py` |
| 6 | On-battery episode inference | `pcss/stats.py` (`detect_on_battery_episodes`) |
| 7 | Battery replace-by projection | `pcss/stats.py` (`battery_replace_projection`) |
| 8 | Billing-cycle alignment | `pcss/stats.py` (`compute_energy_summary`) |
| 9 | Comparison views (cmp, wk) | `pcss/dashboard.py` (`_panel_cmp`/`_panel_wk`) |
| 10 | Touch support | `pcss/charts.js` (gesture layer) |
| 11 | Whole-page export | print stylesheet + `#print-btn` |
| 12 | Accessibility (first tranche) | aria summaries + focusable charts |
| 13 | Spanish localization | `_STRINGS_ES`/`_L` in `pcss/dashboard.py` |
| 14 | Notifications (toast route) | `AlertWatcher` in `tray_status.py` |
| 15 | Auto-refreshing view | `[dashboard] refresh_minutes` meta refresh |
| 31 | Log-staleness watchdog | `pcss/stats.py` (`assess_staleness`) |
| 17 | Tariff history with effective dates | `pcss/config.py` (`tariff_rates_for`), `pcss/stats.py` (`compute_energy_summary`) |
| 27 | End-of-period cost forecast | `pcss/stats.py` (`forecast_period_cost`) |
| 29 | Bill reconciliation | `pcss/loaders.py` (`load_bills`), `pcss/stats.py` (`reconcile_bills`) |
| 26 | Battery lifecycle annotations | `pcss/loaders.py` (`load_annotations`), `pcss/stats.py` (`latest_battery_replacement`, `battery_replace_projection`) |
| 16 | Runtime-curve calibration from observed discharges | `pcss/stats.py` (`calibrate_runtime_curve`) |
| 18 | Self-test detection and battery health under load | `pcss/stats.py` (`detect_self_tests`, `self_test_sag_trend`) |
| 19 | Baseline-deviation energy alerts | `pcss/stats.py` (`detect_baseline_deviations`, `weekday_weekend_profiles`) |
| 28 | Grid-quality trend | `pcss/stats.py` (`grid_quality_trend`) |
| 25 | Payload budget (`max_days` window) | `analyze_ups.py` (`_dashboard_window`, `_window_df`) |

## Dashboard and interaction

### 20. Event timeline panel

Parsed events (item 5) currently surface as amber strips and reference-table
counts. A dedicated card — one row per event category, a dot per occurrence,
time on the x axis — would make the month's story readable at a glance:
outages, low-battery warnings, communication drops, monitoring gaps.

Challenges:

- A new chart shape: categorical rows over a time axis is a fourth renderer
  in `pcss/charts.js` next to line, bar, and heatmap; it needs its own hover
  path and CSV export shape, and the panel key must join `PANELS` in
  `tests/harness.py` with the conftest assertion that the page renders it.
- The E2E fixture agent has no EventLog; either the synthetic agent copies
  `tests/fixtures/EventLog` (real, personal-data-free) or the panel must be
  exempt from the render assertion. Copying the fixture is simpler and also
  exercises the parser inside the E2E build.
- Noise: about 95 percent of recorded events are daily Monitoring and
  Communication churn from PC boots. The panel needs a default filter (power
  and battery categories on, housekeeping off) with the legend toggling the
  rest.

### 21. Keyboard sample step-through (the open remainder of item 12)

Focus a chart, press Enter, and walk sample by sample with the arrow keys —
the tooltip follows the cursor and an `aria-live` region reads out
"timestamp, value, unit" for screen readers. This is the piece of item 12
that was deliberately deferred as a real feature rather than a patch.

Challenges:

- The arrow keys already pan the window (`bindKeyboard` in
  `pcss/charts.js`), so stepping needs an explicit mode — Enter to toggle
  inspect mode on the focused panel, Escape to leave it — with a visible
  state cue so sighted keyboard users know which mode they are in.
- The cursor must walk the full data arrays, not the decimated index that
  `decimateMinMax` renders, or steps would skip samples at wide zoom.
- `aria-live` etiquette: announce on step, not on every render, and keep the
  text short; a chatty live region is worse than none.

### 22. Selectable comparison periods

The Period Comparison card fixes "current versus previous". A small selector
(previous period, same period last quarter, pick-a-period) would let the
panel answer seasonal questions once the energylog archive spans enough
months.

Challenges:

- The payload currently carries only the last two periods
  (`_panel_cmp` in `pcss/dashboard.py`); selection needs all periods in the
  payload, which is fine at hourly resolution but should be decimated
  server-side beyond a few thousand points.
- Selection is client state that belongs in the permalink hash (item 1's
  encoder is extensible — one more key alongside `z` and `p`).
- The control must fit the minimal card-header design; the preset-pill
  pattern (`_section_head`) is the established look for this kind of toggle.

### 30. Auto theme

`[dashboard] theme` picks one palette at build time — `build_dashboard`
bakes `PALETTES[theme]` into the page, which is why the E2E theme suite
builds a second, light dashboard. Moving the palette to CSS custom
properties and shipping both would let a single build follow
`prefers-color-scheme`, with a header toggle for manual override.

Challenges:

- The palette is not only CSS: the panel builders in `pcss/dashboard.py`
  embed concrete colors in the JSON payload (series and marker colors), so
  either the payload carries palette-neutral color roles resolved by
  `pcss/charts.js` at draw time, or both palettes ride the payload. That
  payload refactor is the real work.
- PNG export serializes the SVG to a canvas, and CSS variables inside
  serialized SVG do not resolve; the export path must inline the computed
  colors of the active theme or the exported image comes out wrong.
- The manual override belongs in the permalink hash (item 1's encoder is
  extensible) so a shared link carries its theme; `localStorage` would keep
  it per-machine instead — one of the two, chosen deliberately.
- `tests/e2e_theme.py` and the `light_dashboard_path` fixture simplify to a
  single build toggled live, but must be reworked in the same change.

## Alerting and automation

### 23. Webhook or email notification channel (the open remainder of item 14)

The toast reaches someone sitting at the PC. When nobody is, a push channel
does: a webhook POST (ntfy.sh, a Telegram bot, or any HTTP endpoint) is
simpler and safer to configure than SMTP and covers phones. The trigger and
cooldown logic already exist in `AlertWatcher`; this adds a second delivery
path.

Challenges:

- Ownership: the analyzer stays offline-friendly, so network delivery
  belongs in the tray process next to the toast (`tray_status.py`), sharing
  the watcher and its cooldown rather than duplicating them.
- Secrets: the webhook URL or SMTP credentials go in the OS keyring under
  the existing `KEYRING_SERVICE` pattern, never in `credentials.txt` or the
  repo; the config only says which channel is enabled.
- Delivery failure must never disturb polling — fire-and-forget with a
  short timeout and a logged error, exactly like the toast path.

### 24. Run the analyzer from the tray

A "Actualizar dashboard" menu item that runs `analyze_ups.py --no-browser
--quiet` and notifies when the new page is ready, so refreshing the
dashboard does not require a terminal or waiting for the scheduled task.

Challenges:

- Process hygiene: spawn the venv's `python.exe` (the tray already knows
  `SCRIPT_DIR`) detached, one at a time (reuse the single-flight idea from
  `scheduled_run.ps1`'s skip-if-running guard, but in-process), and report
  completion through `icon.notify`.
- The analyzer writes the archive and the size history; a tray-triggered
  run should behave like the scheduled one (full run, snapshot included)
  unless it happened recently — the once-a-day snapshot marker logic can be
  consulted rather than duplicated.
- Failure surfacing: a nonzero exit should toast the last lines of the log,
  not fail silently.

### 32. Weekly digest

Event-driven alerts (items 14 and 23) say when something happened; a digest
says that nothing did, on a schedule: kWh and cost so far this billing
period, the forecast (item 27) once it exists, anomaly and episode counts,
battery health, and the biggest day of the week. Fatigue-free by
construction, because it arrives on a cadence rather than a trigger.

Challenges:

- Scheduling ownership: the daily scheduled run already exists
  (`scheduled_run.ps1`); the digest is a gate — "first run on or after
  Monday" — with a marker file alongside `output/last_scheduled_run.txt` so
  reruns do not duplicate it.
- Delivery: a digest line appended to `alerts.log` gets toasted by
  `AlertWatcher` today, and the richer destination is item 23's webhook
  channel. This item must not grow its own transport.
- The content is summarization, not new math: everything listed already
  exists in the console summary and `--json`. The work is choosing what a
  short text message omits, and keeping the wording honest about partial
  periods and projections.

## Shipped — implementation archive

Everything below is done and in production. Entries are preserved exactly as
they stood on the active list, with a SHIPPED note recording where the
implementation landed. The first fifteen items were implemented on
2026-07-06. Item 5 (EventLog parsing) was initially deferred for lack of
binary samples, then unblocked the same day: the live EventLog on this
machine turned out to contain no personal data, decodes with a generic
grammar-level reader (javaobj-py3), and the id-to-name mapping ships inside
PCSS's own jars.

### 1. Permalink view state

SHIPPED: `updateHash` / `restoreFromHash` in `pcss/charts.js` encode the
active preset or each panel's non-default zoom window (base-36 epoch-ms) via
`history.replaceState`; restore clamps through `applyWindow` and falls back
silently. `tests/e2e_permalink.py` covers it.

Encode the current view (per-panel zoom windows, active preset, theme) in the
URL hash so a specific view can be bookmarked or reopened after a refresh.

The chart engine already funnels every window change through
`applyWindow` / `applyPreset` / `setWindow` in `pcss/charts.js`, so writing a
compact hash there and replaying it once at `init()` is a small, contained
change.

Challenges:

- The dashboard file is overwritten on every analyzer run, so a stored window
  may refer to a time range that no longer exists in the data. The restore
  path has to clamp against each panel's own domain (the logic in
  `setWindow` already does this) and fall back to the full range silently.
- The hash must stay short and human-tolerable; encoding twelve panel states
  naively produces an ugly URL. Encoding only the panels that differ from the
  default keeps it small.
- Browser history noise: every drag would push a history entry unless the code
  uses `history.replaceState` instead of assignment.

### 2. Anomaly jump navigation

SHIPPED: a flag button on the Line Voltage card (rendered only when anomalies
exist) cycles through marker clusters — anomalies within twelve hours frame as
one view, padded six hours each side (`jumpAnomaly` in `pcss/charts.js`,
`tests/e2e_anomjump.py`).

A control on the Line Voltage card (or in the header) that cycles the window
through the detected anomalies: click, and the panel zooms to a few hours
around the next out-of-envelope sample.

The anomaly timestamps are already in the payload (`markers` on the `lv`
panel), so this is a UI affordance plus a small window calculation.

Challenges:

- Sensible framing: a single-sample anomaly needs padding around it (for
  example, six hours each side), but two anomalies close together should be
  framed as one view rather than two nearly identical jumps. A simple
  clustering pass over the marker timestamps solves it.
- Discoverability without clutter: the design keeps card headers minimal. A
  small "next anomaly" arrow that only appears when anomalies exist fits the
  existing tool-button pattern (`_tools_html` in `pcss/dashboard.py`).

### 3. Heatmap linked to the crosshair

SHIPPED: `_panel_hm` carries `dayKeys` (epoch-ms midnights) and
`highlightHeatmap` in `pcss/charts.js` outlines the hovered day row and hour
cell from the sync-group crosshair (covered in `tests/e2e_sync.py`).

When the crosshair hovers a time panel, outline the matching day row (and
optionally the hour cell) in the Hourly Power Map, so a spike in Power Draw is
easy to locate in the day-by-hour view.

Challenges:

- The heatmap's y axis is a list of day labels, not a time axis; the mapping
  from a hovered timestamp to a row index needs the day labels carried in the
  payload as real dates rather than display strings (a small payload change in
  `_panel_hm`).
- The crosshair-mirror code (`hoverLineAt`) currently only touches panels in
  the `sync` group; the heatmap needs its own lightweight highlight path so
  hover stays cheap.

### 4. DataLog archiving beyond the PCSS retention window

SHIPPED: `append_datalog_archive` / `load_datalog_archive` /
`merge_datalog_frames` in `pcss/loaders.py` (monthly partitions under
`output/archive/`, whole-row dedup so re-appends are no-ops, schema drift
tolerated by concat-then-dedup); `[archive] enabled` config,
`--no-snapshot` skips the append, and the dashboard opens on the 30-day
preset once the merged history exceeds 45 days. `tests/test_archive.py`.

PCSS keeps roughly one month of DataLog samples and discards older ones. The
analyzer already preserves file-size history (`output/size_history.csv`), but
the measurements themselves are lost. Each analyzer run could append the new
DataLog rows to a local archive (partitioned by month, like
`output/archive/datalog-2026-07.csv`), and the loaders could merge the archive
with the live log so charts and statistics span the whole recorded life of the
UPS instead of one month.

This is probably the highest-value item in this file: battery degradation, the
one metric that plays out over months, currently cannot be seen end to end.

Challenges:

- Deduplication on overlap: consecutive runs see mostly the same rows. The
  timestamp is a natural key, but PCSS can log two rows in the same second and
  clock adjustments can repeat a timestamp; the merge has to be idempotent.
- Storage growth and load time: a year of 20-minute samples is small
  (roughly 26,000 rows), so CSV is fine at first, but the merge in
  `load_datalog` must not turn into the slow path of every run.
- Schema drift: PCSS column sets differ across configurations (temperature and
  humidity probes appear and disappear). The archive needs to tolerate columns
  appearing later, which pandas handles if the merge is a concatenation
  followed by deduplication rather than a strict schema join.
- Dashboard density: a year of samples makes the decimation budget in
  `charts.js` matter much more, and the default view probably wants a "last
  30 days" preset applied on load once the archive grows.

### 5. EventLog parsing

SHIPPED: `pcss/eventlog.py` decodes the stream generically with javaobj-py3
(the documented Java-serialization grammar — no APC class definitions, no
hand-maintained offsets), resolves event names from the resource bundles
inside the installed PCSS jars (so names track the installed version) with a
built-in fallback table, and pairs On Battery / No Longer On Battery events
into authoritative outage spans that replace the item-6 inference in the
dashboard when available. Any failure — missing file, missing library,
unparseable bytes — degrades to a status string and the analyzer continues.
Parsed events are archived to `output/archive/events.csv`. The committed
fixture is a real capture verified to contain no personal data (the stream
holds only class names, ids, and timestamps). `tests/test_eventlog.py`.
The original concern about needing samples from two PCSS versions was
resolved by not hand-parsing at all: the grammar-level reader plus
version-tracking bundles plus numeric labels for unknown ids make version
drift a rendering detail instead of a parser break.

The EventLog is written with Java object serialization (a binary format
produced by Java's `ObjectOutputStream`); today only its file size is read.
Parsed events would give the dashboard authoritative markers for outages,
self-tests, and shutdown commands — better than inferring them from data.

Challenges:

- Reverse engineering: the stream format is documented, but the payload
  classes are APC's own. Parsing requires either reimplementing the class
  shapes from observed bytes (fragile across PCSS versions) or using a
  generic Java-deserialization reader that dumps field trees without the
  classes. Either way, samples from at least two PCSS versions are needed to
  trust the parser.
- Safety and stability: a wrong offset must degrade to "could not parse", not
  a crash; the analyzer treats the EventLog as optional today and must keep
  doing so.
- Testing: binary fixtures have to be committed to the repository, and they
  must contain no personal data (event text can include host names).

### 6. Outage and on-battery episode detection from the data we already have

SHIPPED: `detect_on_battery_episodes` in `pcss/stats.py` (low line voltage
corroborated by a battery-capacity drop; thresholds under `[thresholds]`),
listed in the console, shaded as amber `ep-strip` strips on the time panels,
counted in the health pill, and included in the `[alerts]` trigger.
`tests/test_episodes.py`.

Short of parsing the EventLog, the analyzer can infer on-battery episodes:
line voltage at or near zero, battery capacity falling between consecutive
samples, or the runtime estimate dropping sharply. Detected episodes would be
listed in the console summary, shaded on the time panels (the gap-strip
pattern in `renderLine` generalizes to a second strip color), and counted in
the health pill.

Challenges:

- The 20-minute DataLog cadence misses most short outages entirely, and the
  5-minute energylog has no voltage column. Honest labeling matters: the
  feature detects "episodes visible at the sampling cadence", not all
  outages, and the documentation has to say so.
- Distinguishing a real 0 V sample from a logging artifact is not always
  possible from one row; requiring corroboration (capacity drop in the same
  window) trades recall for precision. The thresholds belong in
  `[thresholds]` in `config.toml`, like the existing detections.

### 7. Battery replace-by projection

SHIPPED: `battery_replace_projection` in `pcss/stats.py` — a fit on the
rolling median (immune to self-test dips) projected to
`battery_replace_voltage_v` (default 25.6 V, 2.13 V per cell, documented in
config.example.toml), silent below `battery_trend_min_days` of history. Shown
in the Battery Voltage subtitle, the health pill, the console, and `--json`.
`tests/test_battery.py`.

The Battery Voltage card already fits a linear degradation trend. Extending
the fit to answer "at the current slope, when does the resting voltage cross
the replace threshold?" gives a concrete date instead of a slope, shown in the
card subtitle and the health pill.

Challenges:

- The slope over one month of data is dominated by noise and temperature; the
  projection only becomes meaningful with the archive from item 4. Until
  then, the honest output is "not enough history" below a confidence floor.
- Choosing the threshold voltage requires a defensible number for this battery
  chemistry (per-cell float voltage times cell count), which should be a
  config key with a documented default rather than a hardcoded constant.
- Capacity self-test dips (the sawtooth in Battery Charge) must be excluded
  from the fit or they bias the slope; a robust fit (for example, fitting on
  the rolling median) is likely enough.

### 8. Billing-cycle alignment for cost

SHIPPED: `[tariff] billing_cycle_start_day` groups `compute_energy_summary`
by billing period (start day clamped in short months), applies the tier limit
per period, and labels partial periods. Day 1 reproduces the calendar-month
grouping exactly. `tests/test_billing.py`.

The cost summary currently groups energy by calendar month. Coopesantos bills
on a cycle that does not necessarily start on the first of the month, so the
tiered-cost figure can drift from the bill near the tier boundary. A
`[tariff] billing_cycle_start_day` key would let `compute_energy_summary`
group by billing period instead of calendar month.

Challenges:

- Partial periods at both ends of the recorded span need explicit handling
  (label them as partial rather than presenting a misleading tier split).
- The tier limit applies per billing period; the current
  `compute_tiered_cost` applies it per group, so the change is mostly about
  building the right groups, not the arithmetic.

### 9. Comparison views

SHIPPED: two new cards in Energy & Cost — `cmp` (cumulative kWh against
day-offset-in-period, current billing period overlaid on the previous one)
and `wk` (mean W by hour, weekday against weekend), both linear-axis line
panels (`_panel_cmp` / `_panel_wk` in `pcss/dashboard.py`).

Month-over-month energy (this month's cumulative kWh against last month's at
the same day offset) and weekday-against-weekend daily profiles. Both answer
"is this normal?" — the question the current charts leave to memory.

Challenges:

- These need their own panel builders and chart shapes (two overlaid
  cumulative lines with a shared day-offset axis; grouped bars). The engine
  supports multi-series lines already, but a "day offset" x axis is a third
  `xkind` after time and linear.
- With only the live month of DataLog, comparisons rely on the energylog's
  longer retention or on item 4.

### 10. Touch support

SHIPPED: touch drags start pending and are claimed only on horizontal intent
(vertical swipes keep scrolling), two fingers pinch-zoom around their
midpoint, a tap synthesizes the hover and pins the tooltip, and
`@media (pointer: coarse)` keeps the card tools visible. `tests/e2e_touch.py`
drives it through CDP touch events, including the discriminating
pinch-close-must-zoom-out case.

The interactions are pointer-event based, so taps already hover and pin, but
drag-to-zoom fights native scrolling on a touch screen, pinch zoom is not
implemented, and the card tools only appear on hover.

Challenges:

- Gesture arbitration: the chart boxes set `touch-action: pan-y`, so vertical
  scrolling works, but a horizontal drag must be distinguished from a
  hesitant vertical scroll; a small slope threshold before claiming the
  gesture is the usual answer.
- Pinch zoom means tracking two pointers and computing the scale around their
  midpoint — the math is simple, the state machine (what happens when one
  finger lifts?) is where the bugs live.
- Hover-only affordances (tooltips, the card menu) need a touch equivalent:
  long-press for the tooltip, and always-visible or tap-to-reveal tools on
  coarse pointers (`@media (pointer: coarse)`).

### 11. Whole-page export

SHIPPED: the pragmatic print-pipeline route — an `@media print` stylesheet
(white page, hidden hover-only controls, cards kept whole across page
breaks, colors preserved) plus a header "⎙ pdf" button that calls
`window.print()`.

A single "save as image / PDF" action for the whole dashboard, for sharing a
snapshot. The per-card PNG export exists; the page-level version is a
different problem.

Challenges:

- The page is taller than any viewport; the SVG-to-canvas approach used per
  card would need stitching, and text rendering at canvas scale differs
  subtly from the live page.
- The pragmatic route is the browser's own print pipeline with a print
  stylesheet (`@media print`: white background, no hover-only controls,
  page-break rules between sections). PDF fidelity then depends on the
  browser, which is acceptable for a personal tool.

### 12. Accessibility

SHIPPED (first tranche): every chart box carries `role="img"`,
`tabindex="0"`, and an aria-label with a Python-generated data summary
(span, latest, minimum, maximum); focusing a panel targets the keyboard
shortcuts without hover. The remaining idea — an arrow-key
step-through-samples mode for reading the tooltip without a pointer — is a
real feature on its own and continues as item 21 on the active list.

The dashboard is mouse-first. Keyboard focus for panels (the keyboard
shortcuts currently require hovering), ARIA labels on the SVG charts, and a
screen-reader-friendly rendering of each chart's data (the reference tables
already carry much of it) would make it usable without a pointer.

Challenges:

- Charts as SVG are opaque to screen readers by default; the honest fix is a
  visually hidden data summary per panel (min, max, latest, trend), generated
  in Python where the numbers already exist, rather than trying to make
  every `<path>` readable.
- Focus management: making each chart focusable (`tabindex`) so the existing
  keyboard shortcuts work without hover is straightforward; making the
  tooltip readable on focus without a pointer position requires a "step
  through samples with arrow keys" mode, which is a real feature, not a
  patch.

### 13. Spanish localization

SHIPPED: `[dashboard] language = "en" | "es"` with a single translation
table in `pcss/dashboard.py` (keyed by the English source strings, missing
keys fall back to English); the chart-side labels ride the payload under
`strings` so the two sides cannot drift, and number formatting stays en-US
everywhere so the CSV export remains machine-standard.

The PCSS installation and the electric bill are in Spanish; the dashboard is
in English. A `[dashboard] language = "en" | "es"` key with a small string
table in `pcss/dashboard.py` (and a mirrored table injected into
`charts.js` for tooltip labels) would cover it.

Challenges:

- Keeping two string tables in sync across Python and JavaScript; a single
  dictionary emitted into the payload avoids the drift.
- Number and date formatting: the charts format numbers with `en-US`
  separators today; Spanish formatting (comma decimals) must not leak into
  the CSV export, which should stay machine-standard.

### 14. Notifications

SHIPPED (toast route): `AlertWatcher` in `tray_status.py` tails
`output/alerts.log` from its end (history never re-notifies), tolerates
rotation, applies a 30-minute cooldown, and raises a tray notification for
new lines; the analyzer's alert trigger now also includes on-battery
episodes. The remote channel (webhook or email) continues as item 23 on the
active list.

`[alerts]` currently appends a line to `output/alerts.log`. A notification
that a human actually sees — a Windows toast from the scheduled run, or an
email — is the natural next step, using the same anomaly/high-load triggers
plus any new detections from item 6.

Challenges:

- Windows toasts from a scheduled, non-interactive task are unreliable; the
  dependable route is the tray process (`tray_status.py`), which is already
  a long-lived interactive program and can watch `alerts.log` for new lines.
- Email needs credentials; the keyring pattern used for the PCSS password
  extends to SMTP, but delivery failures must never break the analyzer run
  (alerting stays fire-and-forget with a logged error).
- Alert fatigue: a repeated anomaly should not notify on every run; a
  cooldown or "new since last alert" comparison against the log is needed.

### 15. Auto-refreshing view

SHIPPED (meta-refresh route): `[dashboard] refresh_minutes` (0 disables)
emits a `<meta http-equiv="refresh">` matched to the scheduled-task cadence;
the permalink hash from item 1 preserves the view state across the reload,
and `--no-snapshot` already covers frequent refresh runs. The local-HTTP
variant stays out until this proves too crude.

The dashboard is a static snapshot. For a monitor that sits open on a second
screen, the page could reload itself when a newer `dashboard.html` exists, or
the scheduled task could run more often than daily.

Challenges:

- A `file://` page cannot poll the filesystem; the simplest honest mechanism
  is a `<meta http-equiv="refresh">` interval matched to the scheduled-task
  cadence, accepting that the reload discards view state (which item 1 would
  preserve).
- Anything smarter (a tiny local HTTP server with a change signal) adds a
  long-running process and a port to the project — a real architectural step
  that should only happen if the meta-refresh version proves too crude.
- More frequent analyzer runs append more size-history snapshots; the
  once-a-day marker in `scheduled_run.ps1` and the snapshot logic would need
  a distinction between "full daily run" and "refresh run" (`--no-snapshot`
  already exists for the latter).

### 31. Log-staleness watchdog

SHIPPED: `assess_staleness` in `pcss/stats.py` compares the merged frame's
newest DataLog sample against a wall clock the orchestrator (`analyze_ups.py`)
reads exactly once (`[thresholds] stale_warn_hours` = 12, `stale_crit_hours` =
48, both generous so an evening with the PC off never trips it). Surfaced as
a console line, a `_build_health` degrade to amber/red in `pcss/dashboard.py`
with a reason naming the age (worded distinctly from the `detect_gaps`
historical-gap text), and a line through the existing `[alerts]` append path
in `analyze_ups.py`. The tray-side DataLog-mtime check stays out of scope.
`tests/test_staleness.py`.

Nothing notices when PCSS stops writing: a dead serial link, a stopped
service, or a wedged agent just means every analyzer run re-analyzes aging
data and the dashboard quietly goes stale. When the newest DataLog sample is
older than a multiple of `datalog_expected_interval_min`, the console should
say so, `_build_health` should degrade the health pill (amber, then red
beyond a second threshold), and the `[alerts]` trigger should fire so the
tray toasts it. Cheap, and it guards everything downstream of the data.

Challenges:

- False alarms: the PC being off overnight produces no samples with nothing
  wrong. The default threshold must be generous (half a day or more, under
  `[thresholds]`), and "stale now" must stay distinct from the historical
  gaps that `detect_gaps` already reports.
- Wall-clock enters the pipeline: the analyzer currently reasons only about
  log timestamps. Comparing the newest sample against "now" has to respect
  the naive-local timestamp contract (`ts_2010_to_dt`, the epoch-ms
  convention), or a timezone slip fabricates staleness.
- The tray could also check cheaply between analyzer runs (DataLog file
  mtime); if it does, it must read the same threshold from config rather
  than duplicating the number.

### 17. Tariff history with effective dates

SHIPPED: `[[tariff.history]]` array-of-tables entries in `config.toml`
(`effective_from` plus the same four rate fields the flat `[tariff]` keys
hold), parsed and date-sorted by `load_config()` into `pcss/config.py`'s
`TARIFF_HISTORY` (a loud `ValueError` naming the entry on a malformed date or
a missing rate field). `config.tariff_rates_for(period_start)` picks the
newest entry at or before a period's start date, falling back to the flat
keys — "current rates" — before the earliest entry or when the list is
empty. `compute_energy_summary` in `pcss/stats.py` uses that lookup per
billing period (the grouping and tier arithmetic from item 8 are unchanged)
and tags each period with a rate_tag ("current rates" or "rates from
YYYY-MM-DD"); with no history configured, every number and every surface is
byte-identical to before this feature existed. When history is in play, the
console monthly breakdown, the dashboard's new Billing Periods table
(`pcss/dashboard.py`, localized via `_STRINGS_ES`), and the `--json`
`energy.periods` array all say which rates priced each period.
`tests/test_billing.py`, `tests/test_pipeline.py`, `tests/test_chart_payload.py`.

Coopesantos revises the T-RE rates quarterly, but the config holds a single
rate set, so every analyzer run prices the entire history at today's rates —
past billing periods drift away from the bills they once matched. An array
of dated rate sets (for example `[[tariff.history]]` entries with
`effective_from`, low, high, tier limit) would price each billing period
with the rates that were in force.

Challenges:

- Config shape and compatibility: the flat `[tariff]` keys must keep working
  as the "current" rates so existing config files stay valid;
  `load_config()` in `pcss/config.py` gains a parsed, date-sorted list.
- The per-period grouping and tier arithmetic already exist (item 8), so
  this is a rate lookup by period start date in `compute_energy_summary`,
  not new math. The lookup must pick the newest entry at or before each
  period's start, and fall back to the flat keys before the earliest entry.
- Reporting has to say which rates priced which period (the partial-period
  labeling pattern extends naturally), otherwise a rate boundary mid-history
  looks like a consumption change.

### 27. End-of-period cost forecast

SHIPPED: `forecast_period_cost` in `pcss/stats.py`, next to
`compute_energy_summary`. The current period is the most recent one in the
per-sample frame (the same choice `_panel_cmp` already makes), so a period
with no energylog samples yet is simply absent rather than silently
forecasting an already-closed prior period. Evidence is the count of
distinct calendar days with at least one energylog sample inside that
period, not calendar days elapsed; below `[tariff] forecast_min_days`
(default 5) the result carries no numbers at all — the
`battery_trend_min_days` honesty pattern. Above the floor, the plain
per-day mean (recorded kWh over evidence days) projects to the period's end
date and is priced both flat and Coopesantos-tiered with whichever rates are
in force for the period's start date (`config.tariff_rates_for`, item 17,
not duplicated). A projected tier crossing is named by date; kWh already
recorded past the tier limit is reported as a fact (`already_crossed`)
instead of a projected date. Surfaced as a subtitle on the Period Comparison
card (`_forecast_sub` in `pcss/dashboard.py`, localized via `_STRINGS_ES`),
one console line, and a `forecast` key in the `--json` summary — every
surface words it as a projection ("projected", "at the current pace"),
never a measurement. Only the simple per-day-mean method shipped; the
day-of-week-aware variant from the profile views (item 9) stayed out of
scope, per the roadmap's own "simple version ships first" call.
`tests/test_forecast.py`.

The billing-period grouping (item 8) and the hourly profiles (item 9) make a
forecast mostly a lookup: project the current period's cumulative kWh to the
period's end date, price the result both flat and tiered, and name the date
the tier limit will be crossed. Shown as a subtitle on the Period Comparison
card, a console line, and the `--json` summary — it answers "what will this
bill be?" while there is still time to react.

Challenges:

- Early-period noise: two days into a period, a linear extrapolation is
  wild. The forecast needs a minimum-days floor (the
  `battery_trend_min_days` honesty pattern) or a blend with the previous
  period's profile until enough of the current period exists.
- Method choice: a day-of-week-aware projection from the item-9 profiles
  against a plain per-day mean; the simple version should ship first and the
  profile version only if it demonstrably beats it.
- Wording: the partial-period labeling from item 8 already marks the current
  period; the forecast must present itself as a projection, never a
  measurement, in every surface it reaches.
- Where it lands: a new function in `pcss/stats.py` next to
  `compute_energy_summary`, feeding the `_panel_cmp` subtitle in
  `pcss/dashboard.py`.

### 29. Bill reconciliation

SHIPPED: a user-owned `bills.csv` (`period_start`, `kwh`, `amount_crc`,
header row required) next to `config.toml`, path configurable via
`[paths] bills_file` (`pcss/config.py`'s `BILLS_FILE`, defaulting to
`bills.csv` in the repo root). `pcss/loaders.py`'s `load_bills` reads it
defensively — a missing file silently disables the feature with no warning
at all, a missing required column ignores the whole file, and a row with an
unparseable date or a non-numeric `kwh`/`amount_crc` is reported by line
number and skipped — the analyzer never crashes on this file and never
writes it. `pcss/stats.py`'s `reconcile_bills` joins the parsed rows against
`compute_energy_summary`'s own per-billing-period UPS kWh: a bill's
`period_start` must equal, exactly, the boundary `_billing_period_start`
computes for the configured `billing_cycle_start_day`, or the entry is
reported — naming the entry and the nearest valid boundary — and excluded
rather than silently joined to the wrong period; a period that aligns but
has no matching UPS energy data is likewise reported and excluded. Each
reconciled period reports `ups_kwh`, `billed_kwh`, `share_pct` (worded
everywhere as "the UPS-metered share of the billed consumption" — the UPS
sees only its own outlets, never a whole-house meter), `ups_cost_tiered`,
the billed amount, the bill's own implied rate (`amount_crc / kwh`), and the
tariff's own effective rates for that period via `config.tariff_rates_for`
(item 17, reused rather than duplicated). Surfaced as a console "BILL
RECONCILIATION" section, a "Bill Reconciliation" reference table on the
dashboard (`pcss/dashboard.py`, localized via `_STRINGS_ES`), and a `bills`
key in the `--json` summary — all three appear only when at least one bill
actually reconciles, so the feature is invisible to anyone who never creates
the file. `bills.example.csv` documents the shape; `bills.csv` itself is
gitignored, like `credentials.txt`. `tests/test_bills.py`.

The dashboard prices UPS-metered energy, but the bill covers the whole
house. Recording actual bills (period, kWh, amount) in a small user-owned
file would show the UPS share of household consumption per period and
validate the tariff arithmetic against a real invoice instead of assuming
it.

Challenges:

- Entry ergonomics: a `bills.csv` next to `config.toml` (or `[[bills]]`
  entries in it), tolerant of missing periods — reconciliation is opt-in per
  bill, not a required habit.
- Alignment: entered periods must snap to the bounds from
  `_billing_period_bounds` in `pcss/stats.py`; a start-day mismatch should
  be reported, never silently mis-joined.
- Honest labeling: the UPS sees only its own outlets, so "share of household
  consumption" must not read as if a household meter exists.
- Synergy with item 17: a real bill pins the effective rates for its period,
  so reconciliation data can seed or verify the tariff history.

### 26. Battery lifecycle annotations and replacement tracking

SHIPPED: a user-owned `annotations.csv` (`date`, `kind`, `label`, header row
required) next to `config.toml`, path configurable via
`[paths] annotations_file` (`pcss/config.py`'s `ANNOTATIONS_FILE`,
defaulting to `annotations.csv` in the repo root). `pcss/loaders.py`'s
`load_annotations` reads it defensively, the same pattern as `load_bills`
(item 29): a missing file silently disables the feature, a missing required
column ignores the whole file, and a row with an unparseable date or a
blank `kind` is reported by line number and skipped — the analyzer never
crashes on this file and never writes it. `kind` is freeform text; the only
one the analyzer treats specially is `battery_replaced`, resolved by the new
reusable `pcss/stats.py` helper `latest_battery_replacement` — the newest
`battery_replaced` entry at or before the data's newest sample, so a
replacement dated later than the analyzed data marks no boundary yet.
`battery_replace_projection` now accepts an `annotations` argument: when a
boundary resolves, the fit runs only on samples at or after it (a trend
fit spanning a replacement would otherwise blend two different batteries'
degradation into one meaningless slope), and the result carries
`battery_installed_on` and `battery_age_days` regardless of the projection's
own status — reported in the console `BATTERY REPLACE-BY PROJECTION`
section, the Battery Voltage card subtitle (`pcss/dashboard.py`, localized
via `_STRINGS_ES`), and `--json`. With no qualifying annotation, every
number is unchanged from before this feature existed. Every annotation
(including kinds other than `battery_replaced`) rides the dashboard payload
as a top-level `annotations` list (epoch-ms per the timezone contract) and
renders as a small dashed vertical marker with its label near the top of
every time-axis panel in `pcss/charts.js`, re-positioning under zoom/pan and
disappearing outside the window exactly like the existing point markers,
visually distinct from the red gap strips and amber episode strips.
`annotations.example.csv` documents the shape; `annotations.csv` itself is
gitignored, like `bills.csv`. `tests/test_annotations.py`,
`tests/test_battery.py`, `tests/test_chart_payload.py`,
`tests/e2e_render.py::test_annotation_marker_present`. Item 16's runtime
calibration, when it lands, is expected to reuse the same
`latest_battery_replacement` boundary rather than duplicating it.

The archive is designed to outlive PCSS's own rotation, which means it will
eventually span battery replacements — and both the replace-by projection
(item 7) and the planned runtime calibration (item 16) fit trends that
assume a single battery. A `battery_installed_on` date, and more generally a
small annotations file of dated entries (battery replaced, new appliance on
the UPS, UPS moved), would segment those fits at replacement boundaries and
draw labeled vertical markers across the time panels so the archive's
history stays interpretable years later.

Challenges:

- Fit segmentation: `battery_replace_projection` in `pcss/stats.py` must fit
  only samples after the newest replacement date and report the battery's
  age alongside the projection; the same boundary applies to item 16's
  calibration when it lands.
- Where annotations live: config is for settings, and `output/` holds
  generated files. A user-owned `annotations.csv` (or a `[[annotations]]`
  list in `config.toml`) that the analyzer reads and never writes keeps the
  entries authoritative, and like `size_history.csv` they must never be
  truncated.
- Rendering: a labeled vertical marker is a new small shape in
  `pcss/charts.js` next to the existing point markers, and it must stay
  legible next to the gap and episode strips on a busy axis.

### 16. Runtime-curve calibration from observed discharges

SHIPPED: `calibrate_runtime_curve` in `pcss/stats.py` turns observed
on-battery discharges into a fitted "capacity percent per minute at W
watts" model. It reads the authoritative EventLog spans from
`on_battery_spans` (not the DataLog-inferred fallback), so durations are
exact to the millisecond, and for each closed span computes: capacity
consumed (the last DataLog Battery Capacity sample at or before the span's
start, minus the first sample at or after its end — kept only when the
drop is at least one percentage point, since most real outages last
seconds and drain nothing measurable); duration (the span's own exact
start/end); and power (the energylog sample nearest the span's midpoint via
`merge_asof`, within a tolerance — a span with no power sample nearby is
discarded). A through-origin least-squares fit of drain rate against watts
across the surviving observations recovers `k`, and the measured
watts-to-minutes curve is `100 / (k * W)` evaluated at the configured
`[runtime_curve]` watt points (0 W excluded, where that formula is
undefined). Below `[runtime_curve] calibration_min_episodes` (default 3)
usable observations, the result reports the honest "insufficient_evidence"
and no curve is fitted — the same floor pattern as
`battery_replace_projection`'s `battery_trend_min_days`. The function
reuses `latest_battery_replacement` (item 26) so only discharges recorded
after the newest annotated battery replacement feed the fit, exactly as
that item's own note anticipated. `_panel_rt` in `pcss/dashboard.py` draws
the measured curve as a second, legend-toggleable series next to the
configured curve, and the Estimated Runtime card subtitle names the
discharge count behind it ("measured from N discharges") or the honest
floor note, localized via `_STRINGS_ES`. `analyze_ups.py` sources the spans
from the merged event archive (the same wiring the dashboard's episode
strips already use) and passes the frames into the calibration call.
Per-quarter fitting and recency weighting stayed out of scope, as the
roadmap called for below. `tests/test_calibration.py`.

The `[runtime_curve]` watts-to-minutes table is hand-estimated. Every real
outage is now a measurement: the EventLog spans (item 5) give the exact
on-battery duration to the millisecond, the DataLog gives the battery
capacity consumed, and the energylog gives the mean power draw during the
span. Accumulated in the archives, those observations support a fitted
"capacity percent per minute at W watts" model that can confirm or correct
the configured curve — shown on the Estimated Runtime card as a measured
overlay next to the configured line.

Challenges:

- Sample scarcity: four of the five outages recorded so far lasted seconds,
  which drains no measurable capacity. Only episodes long enough to straddle
  a DataLog sample are usable, so the model needs a minimum-evidence floor
  and the honest "not enough discharge data yet" output, like the
  replace-by projection's `battery_trend_min_days`.
- Load is not constant during a discharge; the energylog's 5-minute samples
  give a mean, and an episode shorter than one interval has no power sample
  at all. The join is per-span, not per-sample (`merge_asof` against the
  span midpoint is probably enough).
- Battery age and temperature shift the curve over time; either weight
  recent episodes more heavily or fit per calendar quarter once the archive
  is deep enough.
- Where it lands: a new function in `pcss/stats.py` feeding `_panel_rt` in
  `pcss/dashboard.py` (a second series plus a subtitle note), with the
  observations sourced from `output/archive/events.csv` and the DataLog
  archive.

### 18. Self-test detection and battery health under load

SHIPPED: `detect_self_tests` in `pcss/stats.py` finds UPS self-tests two
ways, in order of preference. Event-based: once
`pcss.eventlog.SELF_TEST_EVENT_IDS` names the real self-test event id (still
empty — the one-month capture behind this analyzer never caught one; the
constant's own docstring explains how the first observed test names itself
in `output/archive/events.csv` and gains the entry), every matching parsed
event anchors one record at its own timestamp, replacing the shape
heuristic entirely for that call — the same way the EventLog's own outage
spans already replace the DataLog-inferred on-battery episodes once the log
parses. Shape-based (the fallback in practice today): a battery-capacity
drop of at least `[thresholds] selftest_dip_pct` percentage points between
consecutive DataLog samples, recovering to within half that margin of the
pre-dip level within `selftest_recovery_samples` samples, with every Line
Voltage sample across the window inside the normal envelope — the voltage
requirement is what tells a self-test apart from an on-battery episode
(`detect_on_battery_episodes`), which is the same capacity-dip shape but
with line voltage collapsed. Both knobs default to 3.0 points and 4 samples
in `pcss/config.py`, documented in `config.example.toml`. Either route
measures, from the DataLog window bracketing the test, the capacity drop
and the voltage sag (the resting Battery Voltage just before the dip minus
the window's minimum) — a window with no usable voltage samples yields a
record with `sag_v` NaN rather than crashing. `self_test_sag_trend` fits
that sag against time the same way `battery_replace_projection` fits the
resting-voltage slope, reporting the median sag whenever any test has one
but staying at the honest `"insufficient_history"` below
`battery_trend_min_days` of detected-test history (reused, not a new key).
`battery_replace_projection` also gained a `self_tests` argument: every
Battery Voltage sample inside a detected test's `[dip_start, dip_end]`
window is masked out of the fit before the rolling median even runs — belt
and braces on top of that median's own damping, per the roadmap's
"excluding them explicitly". On the dashboard, `_panel_bc` in
`pcss/dashboard.py` draws one dot marker per detected test at the nearest
Battery Charge reading (the `lv` panel's anomaly-marker pattern, reused),
and the card subtitle names the count and median sag, localized via
`_STRINGS_ES`; `analyze_ups.py` prints the count and, once available, the
sag trend in a new "BATTERY SELF-TESTS" console section, right before the
now self-test-aware replace-by projection. `tests/test_selftest.py`.

The Battery Charge sawtooth comes from the UPS's periodic self-tests, and
the PCSS event bundles include self-test event ids. Detecting the tests
(from events when the id shows up, from the capacity-dip shape otherwise)
enables two things: excluding them explicitly from trend fits, and using
the voltage sag under test load as a battery-health signal that complements
the resting-voltage slope of item 7.

Challenges:

- The current one-month capture contains no self-test event, so the exact
  id is unknown. Unknown ids already render numerically and accumulate in
  `output/archive/events.csv`; the first observed test names itself, and
  `FALLBACK_NAMES` in `pcss/eventlog.py` then gains the entry.
- Correlating a test event with the DataLog dip around it is a windowed
  join at the 20-minute cadence; a test shorter than one sample interval may
  leave only the capacity dip, only the voltage sag, or neither.
- The health metric needs a defensible interpretation (voltage sag at a
  known load, trended over months), and it must state its confidence the
  same way the replace-by projection does.

### 19. Baseline-deviation energy alerts

SHIPPED: `weekday_weekend_profiles` in `pcss/stats.py` pulls the mean-W-by-hour
math out of the Weekday vs Weekend dashboard card (item 9's `_panel_wk`) into
one shared helper, so the card and the new detector below read the same
definition of "what a normal day looks like" and cannot drift apart.
`detect_baseline_deviations(energy_df, deviation_pct=None, min_days=None)`
compares each complete day in the energylog — every day except the trailing
partial one (the most recent calendar date, which has not finished
accumulating samples yet) and any day whose sample count falls far short of
its peers (a mid-history sampling gap) — against whichever baseline profile
matches its type (weekday or weekend), built from the full history including
the day itself. The deviation metric is the mean absolute difference between
the day's own hourly profile and the baseline profile across their shared
hours, expressed as a percent of the baseline's own mean power — the blunter
mean-absolute-deviation option the roadmap offered, not a per-hour z-score. A
day is flagged once that percent exceeds `[thresholds] baseline_deviation_pct`
(35.0 by default); below `baseline_min_days` (14) distinct energylog days of
history, the honest result is `"insufficient_history"` with nothing flagged,
the same floor pattern `battery_replace_projection` and
`forecast_period_cost` already use. Both keys default in `pcss/config.py`,
documented in `config.example.toml` as deliberately blunt. On the dashboard,
`_panel_daily` in `pcss/dashboard.py` gives a flagged day's bar the amber
accent color (the same per-bar `color` override `_panel_cad` already uses)
and carries a `markers` list (bar index, the bar's kWh, and a
deviation-percent label) that `renderBar` in `pcss/charts.js` draws as a
dot glyph above the flagged bar; the Daily Energy card subtitle names the
count once at least one day is flagged, localized via `_STRINGS_ES`, and
stays silent otherwise (whether the history is clean or still below the
floor). `analyze_ups.py` prints a new console block right after the
DataLog-gaps line, naming each flagged day with its deviation percent or the
honest not-enough-history line, and `_maybe_write_alerts` gained a
`baseline` argument: a `baseline_deviations=N` field rides the alert line
and the trigger fires on a nonzero flagged count alone, same as the existing
anomaly counts. Every surface says "deviates from the recorded baseline",
never a fault claim. `tests/test_baseline.py` (29 tests) plus a dedicated
`tests/e2e_baseline.py` browser check that the marker glyph really renders.

The weekday and weekend hourly profiles (item 9's `wk` panel) define what a
normal day looks like. Comparing each new day against its profile — mean
absolute deviation, or a per-hour z-score — turns "is this normal?" into a
detection: a stuck-on appliance, a new always-on load, or a failing PSU
shows up as a flagged day instead of a chart the user must remember to read.

Challenges:

- Small history first: with a few weeks of energylog, per-hour variance is
  noisy, and holidays sit in neither profile. The detector needs a minimum
  history floor and a deliberately blunt threshold in `[thresholds]`.
- Where the flag surfaces: a marker on the Daily Energy bars, a line in the
  console summary, and the `[alerts]` trigger — all three exist as patterns
  (`markers`, the anomalies section, `_maybe_write_alerts`).
- Honest labeling again: this flags deviation from the recorded baseline,
  not faults; the wording must not overclaim.

### 28. Grid-quality trend

SHIPPED: `grid_quality_trend` in `pcss/stats.py` classifies the
`detect_voltage_anomalies` envelope violations by direction — a sample below
the `[thresholds]` low envelope bound is a sag, one above the high bound a
swell (the same envelope keys, no new configuration) — merges consecutive
out-of-envelope samples in the same direction into one event, and counts the
caller's already-resolved interruption episodes (the authoritative EventLog
spans when the log parses, otherwise the item 6 inference — the same
precedence the dashboard episode strips apply, passed in rather than
re-implemented) into one row per calendar month: sag, swell, and
interruption counts, recorded days (the month's covered span minus the
`detect_gaps` gap time falling inside it), events per recorded day so
gap-heavy months read honestly, mean depth per direction (per event, each
event's deepest sample's deviation beyond the violated bound), and the worst
event (timestamp, voltage, direction). Only the reference-table presentation
shipped, per the roadmap's own "the table should prove the value first": a
"Grid Quality Trend" table on the dashboard (`_grid_quality_table_html` in
`pcss/dashboard.py`, localized via `_STRINGS_ES`) rendered only when at
least one month has samples, a console "GRID QUALITY TREND" section, and a
`grid_quality` key in the `--json` summary. Every surface labels the counts
as events visible at the sampling cadence, naming the interval from
`datalog_expected_interval_min` rather than hardcoding 20. No new chart
panel, no new `PANELS` key. `tests/test_gridquality.py`.

`detect_voltage_anomalies` finds out-of-envelope samples, but nothing
aggregates them over time. Per-month counts of sags, swells, and
interruptions — with mean depth and the worst event — would answer whether
the Coopesantos supply is getting better or worse, a question no current
view addresses.

Challenges:

- Normalization: months with sampling gaps under-count events, so the trend
  must report rates per recorded day, using `detect_gaps` output to compute
  the recorded time.
- Classification: envelope violations split into sag and swell by direction;
  interruptions come from item 6 episodes or the authoritative EventLog
  spans. The split thresholds belong under `[thresholds]`.
- Cadence honesty: 20-minute samples miss short events entirely (the item 6
  caveat), so this is a trend of events visible at the sampling cadence and
  must be labeled as such.
- Presentation: a reference-table block is the cheap first step; a dedicated
  bar panel is a new key that must join `PANELS` in `tests/harness.py` with
  the conftest render assertion, so the table should prove the value first.

### 25. Payload budget for multi-year archives

SHIPPED: the cheap alternative the item itself favored, not the server-side
decimation pass — `[dashboard] max_days` in `pcss/config.py` (default `0`,
no effect at all). A positive value windows only the raw per-sample frames
fed to `build_dashboard` — the DataLog and energylog series, the
size-history growth series, and the gap/voltage-anomaly/on-battery-episode
overlays that ride alongside them on the same time panels — to the newest
`max_days` days, anchored to the newest DataLog sample rather than the wall
clock. The cut is a single pair of helpers in `analyze_ups.py`
(`_dashboard_window` computes the cutoff or returns `None` when nothing
should change; `_window_df` filters one frame by its own timestamp column)
applied once, right before `build_dashboard()` is called. Everything
computed earlier in `main()` — the console summary, `--json`, alerts, the
archive append, and every fitted stats surface (the battery replace-by
projection, the cost forecast, bill reconciliation, grid-quality trend) —
still runs against the complete history, since those are computed from the
unwindowed frames before the window is applied; the archive on disk is
never touched or truncated either way. When the window actually removes
rows, the dashboard footer names the days shown and points at
`output/archive/` for the rest, localized via `_STRINGS_ES`; `max_days = 0`
or a `max_days` larger than the recorded span both leave the page
byte-identical, with no note. The roadmap's own ponytail question — does
the server-side min/max decimation pass need to exist at all — is answered
"not yet": no decimation shipped with this change, and the decimation
variant described below (full resolution inside a horizon, thinned min/max
buckets before it, a CSV honesty flag on decimated series) remains the
deliberate follow-up if `max_days` alone ever proves insufficient for a
genuinely multi-year archive. `tests/test_dashboard_window.py`.

The DataLog archive grows without bound by design. At 20-minute cadence a
year is roughly 26,000 rows per series; a few years multiplied across the
panels will noticeably fatten `dashboard.html` (every series ships in full
so the page stays offline). A server-side decimation pass — min/max buckets
per series above a point budget, mirroring `decimateMinMax` in
`pcss/charts.js` — would cap the payload while preserving spikes.

Challenges:

- Zooming must not lie: the client re-decimates from the shipped arrays, so
  server-side thinning limits the maximum zoom detail for old data. The
  budget therefore should apply only beyond a horizon (for example, full
  resolution for the last 90 days, min/max buckets before that), and the
  page should say so when a thinned range is displayed.
- The tooltip and CSV export read the shipped arrays; both keep working but
  represent the thinned data — the CSV header should mark decimated series
  to stay machine-honest.
- The cheap alternative — a `[dashboard] max_days` window with the archive
  still intact on disk — should be weighed first; it may be all a personal
  dashboard ever needs (the ponytail question: does the fancy version need
  to exist at all?).
