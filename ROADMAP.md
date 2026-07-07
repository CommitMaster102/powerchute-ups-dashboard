# Roadmap — candidate features and the challenges each one brings

This file collects features that would be worth adding to the analyzer and the
dashboard, together with the technical problems each one has to solve. Nothing
here is committed work. Items are grouped by theme and roughly ordered by
value for effort inside each group. When a feature touches the current
architecture, the relevant files and symbols are named so the entry stays
actionable later.

The three items in the first group were consciously deferred during the 2026-07
dashboard redesign and can be added without reworking anything; the rest are
new ideas.

## Deferred from the redesign (the architecture already supports these)

### 1. Permalink view state

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

## Data and analysis

### 4. DataLog archiving beyond the PCSS retention window

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

## Dashboard and interaction

### 10. Touch support

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

## Alerting and automation

### 14. Notifications

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
