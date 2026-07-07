<script>
(function () {
  "use strict";
  // Chart engine for the generated dashboard. The SVG renderers (line, bar,
  // heatmap, sparkline, tick math, bands, markers) are ported from the Claude
  // Design mockup "UPS Dashboard.dc.html"; the interaction layer (tooltips,
  // pinning, zoom/pan/wheel, the shared time window, presets, export,
  // lightbox, keyboard, load reveal) is original. No chart library — the page
  // is fully offline. The __DASH_DATA__ token below is replaced by
  // pcss/dashboard.py with the real JSON payload.
  const DATA = __DASH_DATA__;
  const S = DATA.strings || {};   // localized UI strings (English fallbacks)
  const SVGNS = "http://www.w3.org/2000/svg";

  // Theme (roadmap item 30). Both palettes ride the payload; charts.js resolves
  // the palette-neutral role names the panels emit (e.g. "blue", "amber") to
  // concrete hex from whichever palette is active at DRAW time. Because chart
  // ink is always a concrete SVG attribute value — never a CSS variable inside
  // the SVG — the PNG export keeps working by construction. The manual override
  // rides the permalink hash (item 1's encoder) rather than localStorage, so a
  // shared link carries its theme rather than keeping it per-machine.
  const PALETTES = DATA.palettes || {};
  const CONFIG_THEME = DATA.theme || "dark";   // "auto" | "dark" | "light"
  let THEME_MODE = CONFIG_THEME;               // current mode (config, hash, or toggle)
  function prefersLight() {
    return !!(window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches);
  }
  function resolveTheme(mode) {
    // "auto" follows the viewer's OS preference; anything unavailable falls
    // back to the design's dark palette.
    if (mode === "light") return "light";
    if (mode === "dark") return "dark";
    return prefersLight() ? "light" : "dark";
  }
  // The active concrete palette; a theme switch swaps it and redraws.
  let C = PALETTES[resolveTheme(THEME_MODE)] || PALETTES.dark || {};
  function resolveColor(role) {
    // Map a palette role name to the active palette's hex; a literal color
    // (an rgba(), or a value that is not a role key) passes straight through.
    if (role == null) return role;
    return Object.prototype.hasOwnProperty.call(C, role) ? C[role] : role;
  }

  // Per-panel interaction state. Every time-axis line panel zooms and pans
  // LOCALLY — gestures never spill over to other charts. What IS shared
  // across the spec.sync group is the hover crosshair (a read-only
  // indicator) and the explicit range-preset pills, which set every time
  // panel's own window at once.
  const STATE = {};       // key -> { zoom: [t0,t1]|null, hidden: Set, views: [view] }
  const LAST_HOVER = {};  // key -> snapped-point info (test surface)
  let PINNED = null;      // { key, html, clientX, clientY }
  let READY = false;      // all load reveals finished (test gate)
  let ACTIVE_KEY = null;  // last-hovered panel, target for keyboard control
  let LIGHTBOX_KEY = null;
  let LAST_PRESET = null; // "30" | "7" | "1" while a preset window is applied
  let INSPECT = null;     // { key, idx } while a panel is in keyboard inspect mode
  // Period Comparison baseline selection (roadmap item 22): "previous"
  // (the default), "quarter" (three periods back), or an explicit period
  // label picked from the dropdown. Period labels look like "2026-06" or
  // "2026-06-15", so they never collide with the two keyword values.
  let CMP_SEL = "previous";

  // Crosshair-sync state only (never drives zooming).
  const TIME = { hoverTs: null };

  const NOANIM = /[?&]noanim=1/.test(location.search) ||
    (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);

  // ------------------------------------------------------------------
  // Small utilities
  // ------------------------------------------------------------------
  function svgEl(tag, attrs, parent) {
    const e = document.createElementNS(SVGNS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (parent) parent.appendChild(e);
    return e;
  }
  function htmlEl(tag, cls, parent) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (parent) parent.appendChild(e);
    return e;
  }
  function fmt(n) {
    if (!isFinite(n)) return "—";
    if (Math.abs(n) >= 1000) return Math.round(n).toLocaleString("en-US");
    return (Math.round(n * 10) / 10).toString();
  }
  function fmtVal(n, dec) {
    if (!isFinite(n)) return "—";
    if (dec == null) return fmt(n);
    return n.toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });
  }
  function niceStep(raw) {
    const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
    const norm = (raw || 1) / mag;
    let s;
    if (norm < 1.5) s = 1; else if (norm < 3) s = 2; else if (norm < 7) s = 5; else s = 10;
    return s * mag;
  }
  function ticks(min, max, n) {
    if (!(max > min)) return [min];
    const step = niceStep((max - min) / n);
    const start = Math.ceil(min / step) * step;
    const out = [];
    for (let v = start; v <= max + step * 1e-6; v += step) out.push(+v.toFixed(6));
    return out;
  }
  function pad2dom(min, max) {
    const sp = (max - min) || Math.abs(min) || 1;
    return [min - sp * 0.08, max + sp * 0.08];
  }
  // All timestamp formatting uses UTC getters: Python emits naive local
  // wall-clock times as epoch-ms "as if UTC", so UTC formatting reproduces
  // the log's wall-clock labels in any browser timezone (the timezone trap
  // that tests/test_chart_payload.py pins).
  function p2(n) { return (n < 10 ? "0" : "") + n; }
  function fmtTick(ms, spanMs) {
    const d = new Date(ms);
    if (spanMs > 3 * 86400e3) return (d.getUTCMonth() + 1) + "/" + d.getUTCDate();
    if (spanMs > 6 * 3600e3) {
      return (d.getUTCMonth() + 1) + "/" + d.getUTCDate() + " " + p2(d.getUTCHours()) + ":" + p2(d.getUTCMinutes());
    }
    return p2(d.getUTCHours()) + ":" + p2(d.getUTCMinutes());
  }
  function fmtFull(ms) {
    const d = new Date(ms);
    return d.getUTCFullYear() + "-" + p2(d.getUTCMonth() + 1) + "-" + p2(d.getUTCDate()) +
      " " + p2(d.getUTCHours()) + ":" + p2(d.getUTCMinutes());
  }
  function fmtCrc(v) {
    // Compact colón axis labels: whole colones below 10k, then k-units with
    // enough precision that adjacent ticks never round to the same label.
    const a = Math.abs(v);
    if (a >= 100000) return "₡" + (v / 1000).toFixed(0) + "k";
    if (a >= 10000) return "₡" + (v / 1000).toFixed(1) + "k";
    return "₡" + Math.round(v).toLocaleString("en-US");
  }
  function lowerBound(arr, v) {
    let lo = 0, hi = arr.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] < v) lo = mid + 1; else hi = mid; }
    return lo;
  }
  function nearestIdx(xs, v) {
    if (!xs.length) return -1;
    const i = lowerBound(xs, v);
    if (i <= 0) return 0;
    if (i >= xs.length) return xs.length - 1;
    return (v - xs[i - 1] <= xs[i] - v) ? i - 1 : i;
  }
  function heatColor(t) {
    t = Math.max(0, Math.min(1, t));
    // The ramp rides the active palette (roadmap item 30), so the hourly power
    // map follows the theme like every other chart.
    const stops = (C && C.heat) || [[14, 30, 40], [31, 122, 140], [79, 209, 197], [243, 193, 75]];
    const seg = t * (stops.length - 1);
    const i = Math.min(stops.length - 2, Math.floor(seg));
    const f = seg - i, a = stops[i], b = stops[i + 1];
    const m = (p, q) => Math.round(p + (q - p) * f);
    return "rgb(" + m(a[0], b[0]) + "," + m(a[1], b[1]) + "," + m(a[2], b[2]) + ")";
  }
  function starPath(cx, cy, spikes, outer, inner) {
    let p = "", rot = -Math.PI / 2;
    const step = Math.PI / spikes;
    for (let i = 0; i < spikes; i++) {
      let x = cx + Math.cos(rot) * outer, y = cy + Math.sin(rot) * outer;
      p += (i ? "L" : "M") + x.toFixed(2) + "," + y.toFixed(2);
      rot += step;
      x = cx + Math.cos(rot) * inner; y = cy + Math.sin(rot) * inner;
      p += "L" + x.toFixed(2) + "," + y.toFixed(2);
      rot += step;
    }
    return p + "Z";
  }
  function download(name, blob) {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
  }

  // ------------------------------------------------------------------
  // Scales + geometry
  // ------------------------------------------------------------------
  function xWindowFor(key, spec) {
    if (spec.xkind === "linear") return spec.xDomain || autoXDomain(spec);
    const st = STATE[key];
    if (st && st.zoom) return st.zoom;
    return autoXDomain(spec);
  }
  function autoXDomain(spec) {
    let lo = Infinity, hi = -Infinity;
    if (spec.kind === "events") {
      // The event timeline carries no series; its span is the extent of the
      // event dots themselves.
      (spec.events || []).forEach(e => { if (e.x < lo) lo = e.x; if (e.x > hi) hi = e.x; });
    } else {
      (spec.series || []).forEach(s => {
        if (s.x.length) { lo = Math.min(lo, s.x[0]); hi = Math.max(hi, s.x[s.x.length - 1]); }
      });
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (lo === hi) { lo -= 1; hi += 1; }
    return [lo, hi];
  }

  // ------------------------------------------------------------------
  // Renderers (ported from the design mockup's DCLogic class)
  // ------------------------------------------------------------------
  function renderLine(view) {
    const key = view.key, spec = view.spec, st = STATE[key];
    const W = view.vb[0], H = view.vb[1];
    const p = spec.pad || { l: 46, r: (spec.series || []).some(s => s.right) ? 56 : 16, t: spec.legend ? 26 : 14, b: 26 };
    const iw = W - p.l - p.r, ih = H - p.t - p.b;
    const xd = xWindowFor(key, spec);
    const zoomed = isZoomed(key, spec);

    // Visible slice per series (one extra point each side keeps the line
    // continuous through the window edge; the plot area is clipped).
    const visible = [];
    (spec.series || []).forEach((s, si) => {
      if (st.hidden.has(si)) return;
      let i0 = lowerBound(s.x, xd[0]), i1 = lowerBound(s.x, xd[1]);
      i0 = Math.max(0, i0 - 1); i1 = Math.min(s.x.length, i1 + 1);
      if (i1 <= i0) return;
      const budget = Math.max(200, Math.floor(iw * 2));
      const idx = decimateMinMax(s.x, s.y, i0, i1, budget);
      visible.push({ s, si, idx });
    });

    // y domains: refit to visible data unless the spec pins them. At full
    // range, the envelope band, threshold lines, and any suggested domain
    // stay in view (matching the mockup); zoomed in, y hugs the data.
    let yd, ryd = null;
    if (spec.yFixed && spec.yDomain) {
      yd = spec.yDomain;
    } else {
      let lo = Infinity, hi = -Infinity;
      visible.forEach(v => { if (!v.s.right) v.idx.forEach(i => { const y = v.s.y[i]; if (y < lo) lo = y; if (y > hi) hi = y; }); });
      if (!isFinite(lo)) { lo = 0; hi = 1; }
      if (!zoomed) {
        (spec.hlines || []).forEach(l => { lo = Math.min(lo, l.y); hi = Math.max(hi, l.y); });
        if (spec.band) { lo = Math.min(lo, spec.band[0]); hi = Math.max(hi, spec.band[1]); }
        if (spec.yDomain) { lo = Math.min(lo, spec.yDomain[0]); hi = Math.max(hi, spec.yDomain[1]); }
      }
      yd = pad2dom(lo, hi);
    }
    const hasRight = (spec.series || []).some(s => s.right);
    if (hasRight) {
      let lo = Infinity, hi = -Infinity;
      visible.forEach(v => { if (v.s.right) v.idx.forEach(i => { const y = v.s.y[i]; if (y < lo) lo = y; if (y > hi) hi = y; }); });
      ryd = isFinite(lo) ? pad2dom(lo, hi) : [0, 1];
      if (ryd[0] === ryd[1]) ryd = [ryd[0] - 1, ryd[1] + 1];
    }

    const sx = x => p.l + (x - xd[0]) / (xd[1] - xd[0]) * iw;
    const syL = y => p.t + ih - (y - yd[0]) / ((yd[1] - yd[0]) || 1) * ih;
    const syR = y => ryd ? p.t + ih - (y - ryd[0]) / ((ryd[1] - ryd[0]) || 1) * ih : 0;

    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "xMidYMid meet" });
    svg.style.width = "100%"; svg.style.height = "auto"; svg.style.display = "block";
    const clipId = "clip-" + key + "-" + view.id;
    const defs = svgEl("defs", {}, svg);
    const clip = svgEl("clipPath", { id: clipId }, defs);
    svgEl("rect", { x: p.l, y: 0, width: iw, height: H }, clip);

    // Gridlines + y tick labels.
    const yTicks = (spec.yTicks && !zoomed && spec.yFixed !== false && spec.yDomain && yd === spec.yDomain)
      ? spec.yTicks : ticks(yd[0], yd[1], 4);
    yTicks.forEach(v => {
      const y = syL(v);
      if (y < p.t - 1 || y > p.t + ih + 1) return;
      svgEl("line", { x1: p.l, x2: W - p.r, y1: y, y2: y, stroke: C.grid, "stroke-width": 1, "vector-effect": "non-scaling-stroke" }, svg);
      const t = svgEl("text", { x: p.l - 7, y: y + 4, "text-anchor": "end", "font-size": 12, fill: C.faint, "font-family": "ui-monospace,monospace" }, svg);
      t.textContent = fmt(v);
    });
    if (ryd) {
      ticks(ryd[0], ryd[1], 4).forEach(v => {
        const y = syR(v);
        if (y < p.t - 1 || y > p.t + ih + 1) return;
        const t = svgEl("text", { x: W - p.r + 6, y: y + 4, "text-anchor": "start", "font-size": 12, fill: spec.y2color || C.violet, "font-family": "ui-monospace,monospace" }, svg);
        t.textContent = (spec.y2fmt === "crc-k") ? fmtCrc(v) : fmt(v);
      });
    }
    // x ticks
    const spanMs = xd[1] - xd[0];
    const nx = W > 600 ? 5 : 3;
    for (let i = 0; i < nx; i++) {
      const v = xd[0] + spanMs * i / (nx - 1);
      const t = svgEl("text", { x: sx(v), y: H - 8, "text-anchor": i === 0 ? "start" : (i === nx - 1 ? "end" : "middle"), "font-size": 12, fill: C.faint, "font-family": "ui-monospace,monospace" }, svg);
      t.textContent = spec.xkind === "linear"
        ? fmt(v) + (i === nx - 1 && spec.xunit ? spec.xunit : "")
        : fmtTick(v, spanMs);
    }

    const plot = svgEl("g", { "clip-path": "url(#" + clipId + ")" }, svg);

    // Normal-envelope band. The tint is the green role at low opacity, so it
    // resolves to the active palette rather than a baked-in rgba().
    if (spec.band) {
      const y1 = syL(spec.band[1]), y0 = syL(spec.band[0]);
      svgEl("rect", { x: p.l, y: Math.min(y0, y1), width: iw, height: Math.abs(y0 - y1),
                      fill: C.green, "fill-opacity": 0.09 }, plot);
    }
    // DataLog gaps: a slim strip along the x-axis (full-height shading read
    // as "the panel has no data" when half the timeline is nightly gaps).
    if (spec.gaps && DATA.gaps && spec.xkind !== "linear") {
      DATA.gaps.forEach(g => {
        if (g[1] < xd[0] || g[0] > xd[1]) return;
        const x0 = sx(Math.max(g[0], xd[0])), x1 = sx(Math.min(g[1], xd[1]));
        if (x1 - x0 < 0.5) return;
        svgEl("rect", { x: x0, y: p.t + ih - 4, width: x1 - x0, height: 4, rx: 1,
                        fill: C.red, "fill-opacity": 0.4, class: "gap-strip" }, plot);
      });
    }
    // Inferred on-battery episodes: an amber strip just above the gap strip.
    // A single-sample episode is only one cadence interval wide, so enforce a
    // small minimum width to keep it visible at full range.
    if (spec.gaps && DATA.episodes && spec.xkind !== "linear") {
      DATA.episodes.forEach(g => {
        if (g[1] < xd[0] || g[0] > xd[1]) return;
        const x0 = sx(Math.max(g[0], xd[0])), x1 = sx(Math.min(g[1], xd[1]));
        svgEl("rect", { x: x0, y: p.t + ih - 9, width: Math.max(2, x1 - x0), height: 4, rx: 1,
                        fill: C.amber, "fill-opacity": 0.55, class: "ep-strip" }, plot);
      });
    }
    // Battery lifecycle annotations (roadmap item 26): a dashed vertical
    // line with a short label near the top of the plot area, on every
    // time-axis panel — distinct from the red gap strips and the amber
    // episode strips along the x-axis, so "the battery was replaced here"
    // reads apart from "the log has a gap here" at a glance. Re-derives its
    // position from the current window on every render, so it re-positions
    // under zoom/pan and disappears once it scrolls outside xd, the same as
    // the point markers below.
    if (DATA.annotations && DATA.annotations.length && spec.xkind !== "linear") {
      DATA.annotations.forEach(a => {
        if (a.x < xd[0] || a.x > xd[1]) return;
        const x = sx(a.x);
        svgEl("line", { x1: x, x2: x, y1: p.t, y2: p.t + ih, stroke: C.violet,
                        "stroke-width": 1.5, "stroke-dasharray": "3 3",
                        "vector-effect": "non-scaling-stroke", class: "annotation-marker" }, plot);
        // Keep the label inside the plot near the right edge: flip to an
        // end-anchor so it grows leftward (the same pattern the threshold-line
        // labels use) instead of overflowing past the right padding. The width
        // is estimated from the character count because the text is not yet in
        // the live DOM to measure at build time.
        const approxLabelW = (a.label || "").length * 6.2;
        const flipLabel = x + 4 + approxLabelW > W - p.r;
        const t = svgEl("text", { x: flipLabel ? x - 4 : x + 4, y: p.t + 11,
                                  "text-anchor": flipLabel ? "end" : "start",
                                  "font-size": 11, fill: C.violet,
                                  "font-family": "ui-monospace,monospace",
                                  class: "annotation-label" }, svg);
        t.textContent = a.label;
      });
    }
    // Threshold lines. l.color is a palette role or a theme-neutral literal.
    (spec.hlines || []).forEach(l => {
      const y = syL(l.y);
      if (y < p.t || y > p.t + ih) return;
      const col = resolveColor(l.color) || C.red;
      svgEl("line", { x1: p.l, x2: W - p.r, y1: y, y2: y, stroke: col, "stroke-width": 1.5, "stroke-dasharray": l.dash || "5 4", "vector-effect": "non-scaling-stroke" }, plot);
      if (l.label) {
        const t = svgEl("text", { x: W - p.r - 4, y: y - 5, "text-anchor": "end", "font-size": 11.5, fill: col, "font-family": "ui-monospace,monospace" }, svg);
        t.textContent = l.label;
      }
    });

    // Series paths. s.color is a palette role; resolve once for the stroke and
    // for the low-opacity area fill (hex + "14" alpha suffix).
    visible.forEach(v => {
      const s = v.s, sy = s.right ? syR : syL;
      const col = resolveColor(s.color);
      let d = "";
      v.idx.forEach((i, k) => { d += (k ? "L" : "M") + sx(s.x[i]).toFixed(1) + "," + sy(s.y[i]).toFixed(1); });
      if (!d) return;
      if (s.fill && v.idx.length > 1) {
        const base = syL(yd[0]);
        const fd = d + "L" + sx(s.x[v.idx[v.idx.length - 1]]).toFixed(1) + "," + base.toFixed(1) +
          "L" + sx(s.x[v.idx[0]]).toFixed(1) + "," + base.toFixed(1) + "Z";
        svgEl("path", { d: fd, fill: resolveColor(s.fillColor) || (col + "14") }, plot);
      }
      svgEl("path", {
        d, fill: "none", stroke: col, "stroke-width": s.width || 2,
        "stroke-dasharray": s.dash || "none", "stroke-linejoin": "round", "stroke-linecap": "round",
        "vector-effect": "non-scaling-stroke", opacity: s.opacity == null ? 1 : s.opacity,
      }, plot);
    });

    // Markers (anomaly X's, projection dots, the runtime star).
    (spec.markers || []).forEach(m => {
      if (spec.xkind !== "linear" && (m.x < xd[0] || m.x > xd[1])) return;
      const x = sx(m.x), y = syL(m.y);
      const mc = resolveColor(m.color);
      if (m.type === "x") {
        const r = 4.5;
        svgEl("line", { x1: x - r, y1: y - r, x2: x + r, y2: y + r, stroke: mc || C.red, "stroke-width": 2, "vector-effect": "non-scaling-stroke" }, plot);
        svgEl("line", { x1: x - r, y1: y + r, x2: x + r, y2: y - r, stroke: mc || C.red, "stroke-width": 2, "vector-effect": "non-scaling-stroke" }, plot);
      } else if (m.type === "star") {
        svgEl("path", { d: starPath(x, y, 5, 8.5, 3.8), fill: mc || C.red, stroke: C.text, "stroke-width": 1, "vector-effect": "non-scaling-stroke" }, plot);
      } else {
        svgEl("circle", { cx: x, cy: y, r: 3.5, fill: mc || C.amber, stroke: C.panel, "stroke-width": 1.5 }, plot);
      }
      if (m.label) {
        // Clamp the label to stay at or below the plot's top padding (item
        // B4): a marker on the tallest point sits near p.t, and an unclamped
        // y - 13 would push its label into (or above) the top padding.
        const labelY = Math.max(p.t + 12, y - 13);
        const t = svgEl("text", { x, y: labelY, "text-anchor": "middle", "font-size": 12, fill: mc || C.text, "font-family": "ui-monospace,monospace", "font-weight": 600 }, svg);
        t.textContent = m.label;
      }
    });

    // Legend chips (clickable, toggle series).
    if (spec.legend && (spec.series || []).length > 1) {
      let lx = p.l + 2;
      spec.series.forEach((s, si) => {
        const g = svgEl("g", { class: "legend-chip", "data-si": si }, svg);
        g.style.cursor = "pointer";
        if (st.hidden.has(si)) g.setAttribute("opacity", "0.35");
        svgEl("line", { x1: lx, x2: lx + 16, y1: p.t - 8, y2: p.t - 8, stroke: resolveColor(s.color), "stroke-width": 2.6, "stroke-dasharray": s.dash || "none", "vector-effect": "non-scaling-stroke" }, g);
        const t = svgEl("text", { x: lx + 20, y: p.t - 4, "font-size": 11.5, fill: C.mut, "font-family": "ui-monospace,monospace" }, g);
        t.textContent = s.name;
        // Wider invisible hit target.
        svgEl("rect", { x: lx - 3, y: p.t - 18, width: 26 + s.name.length * 6.7, height: 18, fill: "transparent" }, g);
        g.addEventListener("click", ev => {
          ev.stopPropagation();
          // A legend toggle on the inspected panel can change which series
          // is "first visible" (inspectRefSeries), silently re-targeting the
          // walking index against a different array — simplest clean
          // semantics is to just exit inspect mode on that toggle.
          if (INSPECT && INSPECT.key === key) exitInspect();
          if (st.hidden.has(si)) st.hidden.delete(si); else st.hidden.add(si);
          if (st.hidden.size === spec.series.length) st.hidden.delete(si); // never hide everything
          rerenderPanel(key);
        });
        lx += 26 + s.name.length * 6.7;
      });
    }

    // Overlay group for crosshair / selection rect (kept last = on top).
    const overlay = svgEl("g", { class: "chart-overlay", "clip-path": "url(#" + clipId + ")" }, svg);

    view.svg = svg;
    view.geom = { W, H, p, iw, ih, xd, yd, ryd, sx, syL, syR, spanMs };
    view.overlay = overlay;
    view.visible = visible;
    return svg;
  }

  // Min/max decimation: cap a series at ~budget drawn points by bucketing on
  // x and keeping each bucket's minimum and maximum sample (in x order), so
  // anomaly spikes survive. Returns index positions into the original arrays.
  function decimateMinMax(xs, ys, i0, i1, budget) {
    const n = i1 - i0;
    const idx = [];
    if (n <= budget) {
      for (let i = i0; i < i1; i++) idx.push(i);
      return idx;
    }
    const nBuckets = Math.max(1, Math.floor(budget / 2));
    const per = n / nBuckets;
    for (let b = 0; b < nBuckets; b++) {
      const s = i0 + Math.floor(b * per);
      const e = Math.min(i0 + Math.floor((b + 1) * per), i1);
      if (s >= e) continue;
      let minJ = s, maxJ = s;
      for (let j = s + 1; j < e; j++) {
        if (ys[j] < ys[minJ]) minJ = j;
        if (ys[j] > ys[maxJ]) maxJ = j;
      }
      if (minJ === maxJ) idx.push(minJ);
      else if (minJ < maxJ) { idx.push(minJ); idx.push(maxJ); }
      else { idx.push(maxJ); idx.push(minJ); }
    }
    return idx;
  }

  function renderBar(view) {
    const spec = view.spec;
    const W = view.vb[0], H = view.vb[1];
    const p = spec.pad || { l: 44, r: 14, t: 16, b: 28 };
    const iw = W - p.l - p.r, ih = H - p.t - p.b;
    const data = spec.data || [];
    const yd = spec.yDomain || [0, Math.max(1e-9, ...data.map(d => d.y)) * 1.14];
    const n = Math.max(1, data.length), bw = iw / n;
    const sy = v => p.t + ih - (v - yd[0]) / ((yd[1] - yd[0]) || 1) * ih;
    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "xMidYMid meet" });
    svg.style.width = "100%"; svg.style.height = "auto"; svg.style.display = "block";
    const base = sy(yd[0]);
    ticks(yd[0], yd[1], 4).forEach(v => {
      const y = sy(v);
      svgEl("line", { x1: p.l, x2: W - p.r, y1: y, y2: y, stroke: C.grid, "stroke-width": 1, "vector-effect": "non-scaling-stroke" }, svg);
      const t = svgEl("text", { x: p.l - 6, y: y + 4, "text-anchor": "end", "font-size": 12, fill: C.faint, "font-family": "ui-monospace,monospace" }, svg);
      t.textContent = spec.dec != null ? fmtVal(v, spec.dec) : fmt(v);
    });
    const labelEvery = spec.labelEvery || (n > 10 ? Math.ceil(n / 8) : 1);
    data.forEach((d, i) => {
      const x = p.l + i * bw + bw * 0.16, w = bw * 0.68, y = sy(d.y);
      svgEl("rect", { x, y, width: w, height: Math.max(0, base - y), rx: 2, fill: resolveColor(d.color) || resolveColor(spec.color) || C.blue, "data-bar": i }, svg);
      if (i % labelEvery === 0) {
        const t = svgEl("text", { x: p.l + i * bw + bw / 2, y: H - 8, "text-anchor": "middle", "font-size": 11, fill: C.faint, "font-family": "ui-monospace,monospace" }, svg);
        t.textContent = d.label;
      }
    });
    // Markers (baseline-deviation flags on the Daily Energy bars, the same
    // glyph shapes and label placement as the line renderer's markers, just
    // anchored to a bar's center and nudged above its top so the glyph never
    // overlaps the bar it flags).
    (spec.markers || []).forEach(m => {
      if (m.x < 0 || m.x >= data.length) return;
      const x = p.l + m.x * bw + bw / 2;
      const barY = sy(m.y != null ? m.y : (data[m.x] ? data[m.x].y : yd[0]));
      const y = barY - 10;
      const mc = resolveColor(m.color);
      if (m.type === "x") {
        const r = 4.5;
        svgEl("line", { x1: x - r, y1: y - r, x2: x + r, y2: y + r, stroke: mc || C.red, "stroke-width": 2, "vector-effect": "non-scaling-stroke" }, svg);
        svgEl("line", { x1: x - r, y1: y + r, x2: x + r, y2: y - r, stroke: mc || C.red, "stroke-width": 2, "vector-effect": "non-scaling-stroke" }, svg);
      } else if (m.type === "star") {
        svgEl("path", { d: starPath(x, y, 5, 8.5, 3.8), fill: mc || C.red, stroke: C.text, "stroke-width": 1, "vector-effect": "non-scaling-stroke" }, svg);
      } else {
        svgEl("circle", { cx: x, cy: y, r: 3.5, fill: mc || C.amber, stroke: C.panel, "stroke-width": 1.5 }, svg);
      }
      if (m.label) {
        // Clamp the label to stay at or below the plot's top padding (item
        // B4): the tallest bar's marker sits near p.t, and an unclamped
        // y - 13 would push its label into (or above) the top padding.
        const labelY = Math.max(p.t + 12, y - 13);
        const t = svgEl("text", { x, y: labelY, "text-anchor": "middle", "font-size": 12, fill: mc || C.text, "font-family": "ui-monospace,monospace", "font-weight": 600 }, svg);
        t.textContent = m.label;
      }
    });

    const overlay = svgEl("g", { class: "chart-overlay" }, svg);
    view.svg = svg;
    view.geom = { W, H, p, iw, ih, bw, sy, base, yd };
    view.overlay = overlay;
    return svg;
  }

  function renderHeatmap(view) {
    const spec = view.spec;
    const W = view.vb[0], H = view.vb[1];
    const p = spec.pad || { l: 48, r: 14, t: 14, b: 26 };
    const iw = W - p.l - p.r, ih = H - p.t - p.b;
    const rows = spec.days.length, cols = 24;
    const cw = iw / cols, ch = ih / rows;
    const zf = spec.z.flat().filter(v => v != null);
    const zmin = Math.min(...zf), zmax = Math.max(...zf);
    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "xMidYMid meet" });
    svg.style.width = "100%"; svg.style.height = "auto"; svg.style.display = "block";
    for (let r = 0; r < rows; r++) {
      for (let c = 0; c < cols; c++) {
        const v = spec.z[r][c];
        svgEl("rect", {
          x: p.l + c * cw, y: p.t + r * ch, width: cw + 0.6, height: ch + 0.6,
          fill: v == null ? C.grid : heatColor((v - zmin) / ((zmax - zmin) || 1)),
        }, svg);
      }
    }
    const every = Math.max(1, Math.ceil(rows / 8));
    spec.days.forEach((dl, r) => {
      if (r % every) return;
      const t = svgEl("text", { x: p.l - 5, y: p.t + r * ch + ch / 2 + 4, "text-anchor": "end", "font-size": 10.5, fill: C.faint, "font-family": "ui-monospace,monospace" }, svg);
      t.textContent = dl;
    });
    [0, 6, 12, 18, 23].forEach(hr => {
      const t = svgEl("text", { x: p.l + hr * cw + cw / 2, y: H - 8, "text-anchor": "middle", "font-size": 10.5, fill: C.faint, "font-family": "ui-monospace,monospace" }, svg);
      t.textContent = hr;
    });
    const overlay = svgEl("g", { class: "chart-overlay" }, svg);
    view.svg = svg;
    view.geom = { W, H, p, iw, ih, cw, ch, rows, cols, zmin, zmax };
    view.overlay = overlay;
    return svg;
  }

  // Event timeline (roadmap item 20): the fourth chart shape. One categorical
  // row per event category, one dot per occurrence, time on the x axis. The
  // category row labels double as the legend — clicking one toggles its row
  // via st.hidden (seeded from the payload's default-visible flags). Dots that
  // would overlap in the same minute are lane-packed vertically within the row
  // band so every one stays hoverable.
  function renderEvents(view) {
    const key = view.key, spec = view.spec, st = STATE[key];
    const W = view.vb[0], H = view.vb[1];
    const p = spec.pad || { l: 108, r: 16, t: 14, b: 26 };
    const iw = W - p.l - p.r, ih = H - p.t - p.b;
    const xd = xWindowFor(key, spec);
    const rows = spec.rows || [];
    const n = Math.max(1, rows.length);
    const rowH = ih / n;
    const sx = x => p.l + (x - xd[0]) / ((xd[1] - xd[0]) || 1) * iw;
    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "xMidYMid meet" });
    svg.style.width = "100%"; svg.style.height = "auto"; svg.style.display = "block";
    const clipId = "clip-" + key + "-" + view.id;
    const defs = svgEl("defs", {}, svg);
    const clip = svgEl("clipPath", { id: clipId }, defs);
    svgEl("rect", { x: p.l, y: 0, width: iw, height: H }, clip);

    // Row separators + category labels (labels double as the legend toggles).
    rows.forEach((r, i) => {
      const y0 = p.t + i * rowH, yMid = y0 + rowH / 2;
      svgEl("line", { x1: p.l, x2: W - p.r, y1: y0, y2: y0, stroke: C.rowline || C.grid,
                      "stroke-width": 1, "vector-effect": "non-scaling-stroke" }, svg);
      const hidden = st.hidden.has(i);
      const g = svgEl("g", { class: "ev-legend", "data-cat": r.cat, "data-row": i }, svg);
      g.style.cursor = "pointer";
      if (hidden) g.setAttribute("opacity", "0.4");
      svgEl("circle", { cx: p.l - 16, cy: yMid, r: 4, fill: resolveColor(r.color) }, g);
      const t = svgEl("text", { x: p.l - 26, y: yMid + 4, "text-anchor": "end", "font-size": 11.5,
                                fill: C.mut, "font-family": "ui-monospace,monospace" }, g);
      t.textContent = r.label;
      // Wide invisible hit target spanning the row's label gutter.
      svgEl("rect", { x: 0, y: y0, width: p.l - 2, height: rowH, fill: "transparent" }, g);
      g.addEventListener("click", ev => {
        ev.stopPropagation();
        if (st.hidden.has(i)) st.hidden.delete(i); else st.hidden.add(i);
        if (st.hidden.size >= rows.length) st.hidden.delete(i);  // never hide every row
        rerenderPanel(key);
      });
    });

    // x ticks (time).
    const spanMs = xd[1] - xd[0];
    const nx = W > 600 ? 5 : 3;
    for (let i = 0; i < nx; i++) {
      const v = xd[0] + spanMs * i / (nx - 1);
      const t = svgEl("text", { x: sx(v), y: H - 8,
                                "text-anchor": i === 0 ? "start" : (i === nx - 1 ? "end" : "middle"),
                                "font-size": 12, fill: C.faint, "font-family": "ui-monospace,monospace" }, svg);
      t.textContent = fmtTick(v, spanMs);
    }

    const plot = svgEl("g", { "clip-path": "url(#" + clipId + ")" }, svg);
    // Dots per visible row, lane-packed so same-minute events stay separable.
    const dotR = 3.4, laneGap = 9;
    const dots = [];
    rows.forEach((r, i) => {
      if (st.hidden.has(i)) return;
      const y0 = p.t + i * rowH, yMid = y0 + rowH / 2;
      const evs = (spec.events || []).filter(e => e.cat === r.cat && e.x >= xd[0] && e.x <= xd[1])
        .sort((a, b) => a.x - b.x);
      const rowColor = resolveColor(r.color);
      const lanes = [];
      evs.forEach(e => {
        const cx = sx(e.x);
        let lane = 0;
        while (lane < lanes.length && cx - lanes[lane] < 2 * dotR + 1) lane++;
        lanes[lane] = cx;
        const off = (lane % 2 === 0 ? 1 : -1) * Math.ceil(lane / 2) * laneGap;
        const cy = Math.max(y0 + dotR + 1, Math.min(y0 + rowH - dotR - 1, yMid + off));
        svgEl("circle", { cx, cy, r: dotR, fill: rowColor, stroke: C.panel, "stroke-width": 1,
                          class: "ev-dot" }, plot);
        dots.push({ cx, cy, e, color: rowColor, label: r.label });
      });
    });

    const overlay = svgEl("g", { class: "chart-overlay", "clip-path": "url(#" + clipId + ")" }, svg);
    view.svg = svg;
    view.geom = { W, H, p, iw, ih, xd, sx, spanMs, rowH };
    view.overlay = overlay;
    view.evDots = dots;
    return svg;
  }

  // The event name is the one data-derived string on the page (it comes from
  // the EventLog / PCSS bundles), and eventsTooltipHTML assembles it into
  // innerHTML, so escape it rather than trusting it as markup.
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, ch => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  }

  function eventsTooltipHTML(dot) {
    return '<div class="tt-ts">' + fmtFull(dot.e.x) + "</div>" +
      '<div class="tt-row"><span class="tt-dot" style="background:' + dot.color + '"></span>' +
      '<span class="tt-name">' + escapeHtml(dot.e.name) + "</span>" +
      '<span class="tt-val">' + dot.label + "</span></div>";
  }

  function renderSparkline(container, spark) {
    const W = 130, H = 34, pd = 3;
    const xs = spark.x, ys = spark.y;
    if (!xs || xs.length < 2) return;
    const x0 = xs[0], x1 = xs[xs.length - 1];
    let y0 = Infinity, y1 = -Infinity;
    ys.forEach(v => { if (v < y0) y0 = v; if (v > y1) y1 = v; });
    const sx = x => pd + (x - x0) / ((x1 - x0) || 1) * (W - 2 * pd);
    const sy = y => H - pd - (y - y0) / ((y1 - y0) || 1) * (H - 2 * pd);
    const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
    svg.style.width = "100%"; svg.style.height = "34px"; svg.style.display = "block";
    const col = resolveColor(spark.color);
    let d = "";
    xs.forEach((x, i) => { d += (i ? "L" : "M") + sx(x).toFixed(1) + "," + sy(ys[i]).toFixed(1); });
    svgEl("path", { d: d + "L" + sx(x1).toFixed(1) + "," + (H - pd) + "L" + sx(x0).toFixed(1) + "," + (H - pd) + "Z", fill: col + "22" }, svg);
    svgEl("path", { d, fill: "none", stroke: col, "stroke-width": 1.8, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    container.appendChild(svg);
  }

  // Re-render every KPI sparkline (used on a theme switch and by resetAll, so
  // the sparklines follow the active palette like the panels do).
  function renderSparks() {
    (DATA.sparks || []).forEach((spark, i) => {
      const cont = document.querySelector('.kpi-spark[data-idx="' + i + '"]');
      if (cont && spark) { cont.replaceChildren(); renderSparkline(cont, spark); }
    });
  }

  // ------------------------------------------------------------------
  // Rendering orchestration
  // ------------------------------------------------------------------
  function rerenderPanel(key) {
    const st = STATE[key];
    if (!st) return;
    st.views.forEach(v => renderView(v));
    updateResetPill(key);
    // A re-render replaces the SVG wholesale (fresh, empty overlay), so a
    // panel under keyboard inspect (e.g. after a +/-/0 zoom key) needs its
    // crosshair/tooltip re-shown at the same sample or it goes stale until
    // the next step.
    if (INSPECT && INSPECT.key === key) showInspectSample(key);
  }
  function renderView(view) {
    const spec = view.spec;
    let svg;
    if (spec.kind === "bar") svg = renderBar(view);
    else if (spec.kind === "heatmap") svg = renderHeatmap(view);
    else if (spec.kind === "events") svg = renderEvents(view);
    else svg = renderLine(view);
    view.container.replaceChildren(svg);
  }
  function isZoomed(key) {
    const st = STATE[key];
    return !!(st && st.zoom);
  }
  function syncKeys() {
    // The crosshair-mirror group (spec.sync) — hover indication only.
    return Object.keys(STATE).filter(k => STATE[k].spec && STATE[k].spec.sync);
  }
  function timePanels() {
    // Every panel with a time x-axis: these zoom locally, and the range
    // presets (plus the setWindow test hook) address them all at once. The
    // event timeline (kind "events") has a time x-axis too, so it joins.
    return Object.keys(STATE).filter(k => {
      const s = STATE[k].spec;
      return s && (s.kind === "line" || s.kind === "events") &&
        s.xkind !== "linear" && STATE[k].views.length > 0;
    });
  }
  function setWindow(t0, t1) {
    // Apply an absolute window to every time panel, clamped to each panel's
    // own data span (used by the preset pills and the E2E suites).
    if (!isFinite(t0) || !isFinite(t1) || t1 <= t0) return;
    timePanels().forEach(k => {
      const full = autoXDomain(STATE[k].spec);
      const a = Math.max(full[0], t0), b = Math.min(full[1], t1);
      STATE[k].zoom = (b <= a || (a <= full[0] + 1 && b >= full[1] - 1)) ? null : [a, b];
      rerenderPanel(k);
    });
    LAST_PRESET = null;
    updatePresetPills();
    updateHash();
  }
  function applyPreset(days) {
    if (days === "all") {
      timePanels().forEach(k => { STATE[k].zoom = null; rerenderPanel(k); });
      LAST_PRESET = null;
    } else {
      // Anchor each panel to ITS OWN newest sample; a preset longer than a
      // panel's history leaves that panel at full range.
      timePanels().forEach(k => {
        const full = autoXDomain(STATE[k].spec);
        const t0 = Math.max(full[0], full[1] - (+days) * 86400e3);
        STATE[k].zoom = (t0 <= full[0] + 1) ? null : [t0, full[1]];
        rerenderPanel(k);
      });
      LAST_PRESET = days;
    }
    updatePresetPills();
    updateHash();
  }
  // ------------------------------------------------------------------
  // Period Comparison baseline selection (roadmap item 22). The payload's
  // cmp panel carries every billing period (spec.periods); this picks the
  // current period (always the last) and whichever baseline period is
  // selected, and rebuilds the two-series spec.series the line renderer
  // already knows how to draw — so renderLine, the legend, inspect mode,
  // and CSV export all keep working unmodified.
  // ------------------------------------------------------------------
  function cmpPeriods() {
    const st = STATE.cmp;
    return (st && st.spec && st.spec.periods) || [];
  }
  function cmpCurrentPeriod() {
    const periods = cmpPeriods();
    return periods.length ? periods[periods.length - 1] : null;
  }
  function cmpResolveBaseline(sel) {
    // Returns the baseline period object for a selection value, or null if
    // it cannot be resolved against the periods actually in the payload —
    // the caller falls back to the default rather than rendering nothing.
    const periods = cmpPeriods();
    const n = periods.length;
    if (n < 2) return null;
    if (sel === "previous") return periods[n - 2];
    if (sel === "quarter") return n >= 4 ? periods[n - 4] : null;
    if (sel === periods[n - 1].label) return null;   // never compare current to itself
    const found = periods.find(p => p.label === sel);
    return found || null;
  }
  function rebuildCmpSeries() {
    const st = STATE.cmp;
    if (!st || !st.spec || !st.spec.periods) return;
    const spec = st.spec;
    const cur = cmpCurrentPeriod();
    const base = cmpResolveBaseline(CMP_SEL);
    if (!cur || !base) return;
    // Store palette-neutral role names (not resolved hex) so a live theme
    // switch re-resolves these on the next redraw, like every other series.
    spec.series = [
      { name: base.label, color: "faint", x: base.x, y: base.y, width: 1.6, dash: "5 4" },
      { name: cur.label, color: "teal", x: cur.x, y: cur.y, width: 2.2 },
    ];
  }
  function updateCmpControls() {
    document.querySelectorAll(".cmp-pill").forEach(b => {
      const active = b.dataset.mode === CMP_SEL;
      b.classList.toggle("is-active", active);
      // Keep the toggle-group aria state current (item B3).
      b.setAttribute("aria-pressed", active ? "true" : "false");
    });
    const sel = document.querySelector(".cmp-period-select");
    if (sel) {
      // The select only reflects an explicit pick-a-period choice; the two
      // named modes own the pills instead, so the select shows its
      // placeholder while either of those is active.
      const isPill = CMP_SEL === "previous" || CMP_SEL === "quarter";
      sel.value = isPill ? "" : CMP_SEL;
    }
  }
  function setCmpSelection(sel) {
    CMP_SEL = cmpResolveBaseline(sel) ? sel : "previous";
    rebuildCmpSeries();
    updateCmpControls();
    if (STATE.cmp) rerenderPanel("cmp");
    updateHash();
  }

  // ------------------------------------------------------------------
  // Theme control (roadmap item 30). applyTheme swaps the active palette and
  // redraws every chart plus the sparklines; the header toggle cycles from
  // auto to dark to light; the override rides the permalink hash (encoded only
  // when it differs from the config default) and resetAll clears it back to
  // that default. __chartsDebug exposes the resolved active theme for tests.
  // ------------------------------------------------------------------
  function applyTheme() {
    C = PALETTES[resolveTheme(THEME_MODE)] || PALETTES.dark || {};
    document.documentElement.setAttribute("data-theme", THEME_MODE);
    Object.keys(STATE).forEach(rerenderPanel);
    renderSparks();
    // A pinned tooltip is a snapshot of ttLive's innerHTML taken at pin time,
    // dot colors baked in from the prior palette. Rebuilding it would need the
    // resolved series color per row, which LAST_HOVER does not carry; hiding
    // it is the simpler and honest choice over showing stale hues until the
    // next hover.
    if (PINNED) unpin();
  }
  function updateThemeToggle() {
    const b = document.getElementById("theme-btn");
    if (!b) return;
    const names = { auto: S.themeAuto || "auto", dark: S.themeDark || "dark",
                    light: S.themeLight || "light" };
    b.textContent = (S.themeLabel || "theme") + ": " + (names[THEME_MODE] || THEME_MODE);
  }
  function setTheme(mode) {
    if (mode !== "auto" && mode !== "dark" && mode !== "light") return;
    THEME_MODE = mode;
    applyTheme();
    updateThemeToggle();
    updateHash();
  }
  function cycleTheme() {
    setTheme(THEME_MODE === "auto" ? "dark" : (THEME_MODE === "dark" ? "light" : "auto"));
  }

  // ------------------------------------------------------------------
  // Permalink view state: the active preset, or each panel's non-default
  // zoom window, lives in the URL hash so a view can be bookmarked and
  // survives a meta-refresh reload. replaceState keeps drags out of the
  // browser history; base-36 epoch-ms keeps the hash short.
  // ------------------------------------------------------------------
  function updateHash() {
    const parts = [];
    if (LAST_PRESET) {
      parts.push("p=" + LAST_PRESET);
    } else {
      const zparts = [];
      timePanels().forEach(k => {
        const z = STATE[k].zoom;
        if (z) zparts.push(k + "." + Math.round(z[0]).toString(36) + "." + Math.round(z[1]).toString(36));
      });
      if (zparts.length) parts.push("z=" + zparts.join(","));
    }
    if (CMP_SEL !== "previous") parts.push("c=" + encodeURIComponent(CMP_SEL));
    // The manual theme override rides the hash (roadmap item 30), encoded only
    // when it differs from the config default so a default page stays cleanly
    // hashless.
    if (THEME_MODE !== CONFIG_THEME) parts.push("t=" + THEME_MODE);
    const hash = parts.join("&");
    try {
      history.replaceState(null, "", location.pathname + location.search + (hash ? "#" + hash : ""));
    } catch (e) { /* some file:// contexts refuse replaceState; the view still works */ }
  }
  function restoreFromHash() {
    const h = location.hash.replace(/^#/, "");
    if (!h) return;
    try {
      const params = new URLSearchParams(h);
      const p = params.get("p");
      if (p && ["30", "7", "1"].indexOf(p) >= 0) {
        applyPreset(p);
      } else {
        const z = params.get("z");
        if (z) {
          z.split(",").forEach(part => {
            const bits = part.split(".");
            if (bits.length !== 3) return;
            const key = bits[0], t0 = parseInt(bits[1], 36), t1 = parseInt(bits[2], 36);
            const st = STATE[key];
            if (!st || !st.spec || !zoomCapable(st.spec)) return;
            if (!isFinite(t0) || !isFinite(t1) || t1 <= t0) return;
            // applyWindow clamps to the panel's own data span, so a stored
            // window that predates the current logs degrades to the full range.
            applyWindow(key, st.spec, [t0, t1]);
          });
        }
      }
      const c = params.get("c");
      if (c) setCmpSelection(c);
      const t = params.get("t");
      if (t) setTheme(t);   // ignores anything that is not auto/dark/light
    } catch (e) { /* a malformed hash falls back to the default view */ }
  }

  function updateResetPill(key) {
    document.querySelectorAll('.tool-reset[data-panel="' + key + '"]').forEach(b => {
      b.hidden = !isZoomed(key);
    });
  }
  function updatePresetPills() {
    const anyZoom = timePanels().some(k => STATE[k].zoom);
    document.querySelectorAll(".preset-pill").forEach(b => {
      const d = b.dataset.days;
      const active = d === "all" ? !anyZoom : LAST_PRESET === d;
      b.classList.toggle("is-active", active);
      // Keep the toggle-group aria state current (item B3).
      b.setAttribute("aria-pressed", active ? "true" : "false");
    });
  }

  // ------------------------------------------------------------------
  // Tooltip + crosshair
  // ------------------------------------------------------------------
  let ttLive = null, ttPin = null;
  function ensureTooltips() {
    ttLive = htmlEl("div", "chart-tooltip", document.body);
    ttPin = htmlEl("div", "chart-tooltip is-pinned", document.body);
    ttLive.hidden = true; ttPin.hidden = true;
  }
  function placeTooltip(tt, clientX, clientY) {
    tt.hidden = false;
    const r = tt.getBoundingClientRect();
    let x = clientX + 14, y = clientY + 12;
    if (x + r.width > innerWidth - 8) x = clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = clientY - r.height - 12;
    tt.style.left = Math.max(4, x) + "px";
    tt.style.top = Math.max(4, y) + "px";
  }
  function hideHover(key) {
    if (ttLive) ttLive.hidden = true;
    const st = STATE[key];
    if (st) st.views.forEach(v => { if (v.overlay) v.overlay.replaceChildren(); });
    if (st && st.spec && st.spec.sync) {
      TIME.hoverTs = null;
      syncKeys().forEach(k => { if (k !== key) clearOverlay(k); });
      clearHeatmapHighlight();
    }
    delete LAST_HOVER[key];
  }
  function heatmapKeys() {
    return Object.keys(STATE).filter(k =>
      STATE[k].spec && STATE[k].spec.kind === "heatmap" && STATE[k].views.length);
  }
  // The heatmap sits outside the crosshair-mirror group (its y axis is a day
  // list, not time), so it gets its own lightweight highlight: outline the
  // hovered timestamp's day row and hour cell.
  function highlightHeatmap(ts) {
    heatmapKeys().forEach(k => {
      const spec = STATE[k].spec;
      const keys = spec.dayKeys || [];
      const r = keys.indexOf(Math.floor(ts / 86400e3) * 86400e3);
      STATE[k].views.forEach(v => {
        if (!v.overlay || !v.geom) return;
        v.overlay.replaceChildren();
        if (r < 0) return;
        const g = v.geom;
        // The row/cell outline is the active palette's text role rather than
        // a hardcoded white, so it stays visible on the light heat ramp too.
        svgEl("rect", { x: g.p.l, y: g.p.t + r * g.ch, width: g.iw, height: g.ch,
                        fill: "none", stroke: C.text, "stroke-opacity": 0.5, "stroke-width": 1,
                        "vector-effect": "non-scaling-stroke" }, v.overlay);
        const c = new Date(ts).getUTCHours();
        svgEl("rect", { x: g.p.l + c * g.cw, y: g.p.t + r * g.ch, width: g.cw, height: g.ch,
                        fill: "none", stroke: C.text, "stroke-width": 1.5,
                        "vector-effect": "non-scaling-stroke" }, v.overlay);
      });
    });
  }
  function clearHeatmapHighlight() {
    heatmapKeys().forEach(clearOverlay);
  }
  function clearOverlay(key) {
    const st = STATE[key];
    if (st) st.views.forEach(v => { if (v.overlay) v.overlay.replaceChildren(); });
  }

  function lineTooltipHTML(key, spec, ts, rows) {
    let h = "";
    if (spec.xkind === "linear") h += '<div class="tt-ts">' + fmt(ts) + " " + (spec.xunit || "") + "</div>";
    else h += '<div class="tt-ts">' + fmtFull(ts) + "</div>";
    rows.forEach(r => {
      h += '<div class="tt-row"><span class="tt-dot" style="background:' + r.color + '"></span>' +
        '<span class="tt-name">' + r.name + "</span>" +
        '<span class="tt-val">' + r.val + "</span></div>";
    });
    return h;
  }

  function hoverLineAt(key, ts, clientX, clientY, fromSync) {
    const st = STATE[key];
    if (!st || !st.spec || st.spec.kind !== "line") return;
    const spec = st.spec;
    const rows = [];
    let snappedTs = null;
    (spec.series || []).forEach((s, si) => {
      if (st.hidden.has(si)) return;
      const i = nearestIdx(s.x, ts);
      if (i < 0) return;
      if (snappedTs == null || Math.abs(s.x[i] - ts) < Math.abs(snappedTs - ts)) snappedTs = s.x[i];
      rows.push({
        name: s.name, color: resolveColor(s.color), si, i,
        val: fmtVal(s.y[i], spec.dec) + " " + (s.right ? (spec.y2unit || "") : (spec.unit || "")),
        x: s.x[i], y: s.y[i], right: !!s.right,
      });
    });
    if (snappedTs == null) return;
    // "val" (the already-formatted-and-unit-suffixed reading, e.g. "120.3 V")
    // rides along so the inspect-mode aria-live announcement can reuse the
    // exact text the tooltip shows instead of re-deriving it.
    LAST_HOVER[key] = { ts: snappedTs, rows: rows.map(r => ({ name: r.name, x: r.x, y: r.y, val: r.val })) };
    // Crosshair + dots on every view of this panel.
    st.views.forEach(v => {
      if (!v.geom || !v.overlay) return;
      const g = v.geom;
      if (snappedTs < g.xd[0] || snappedTs > g.xd[1]) { v.overlay.replaceChildren(); return; }
      v.overlay.replaceChildren();
      const x = g.sx(snappedTs);
      // The crosshair guide is drawn in the active palette's text role at low
      // opacity rather than a hardcoded white, so it stays visible against a
      // light background too and follows a live theme switch like every other
      // chart mark.
      svgEl("line", { x1: x, x2: x, y1: g.p.t, y2: g.p.t + g.ih, stroke: C.text, "stroke-opacity": 0.28, "stroke-width": 1, "stroke-dasharray": "3 3", "vector-effect": "non-scaling-stroke" }, v.overlay);
      rows.forEach(r => {
        const sy = r.right ? g.syR : g.syL;
        svgEl("circle", { cx: g.sx(r.x), cy: sy(r.y), r: 3.6, fill: r.color, stroke: C.panel, "stroke-width": 1.5 }, v.overlay);
      });
    });
    if (!fromSync && clientX != null) {
      ttLive.innerHTML = lineTooltipHTML(key, spec, snappedTs, rows);
      placeTooltip(ttLive, clientX, clientY);
    }
    // Mirror the crosshair across the sync group, and light up the matching
    // day row in the hourly power map.
    if (spec.sync && !fromSync) {
      TIME.hoverTs = snappedTs;
      syncKeys().forEach(k => { if (k !== key) hoverLineAt(k, snappedTs, null, null, true); });
      highlightHeatmap(snappedTs);
    }
  }

  // Event timeline hover: snap to the nearest dot in 2D (dots are lane-packed
  // vertically within a row so x alone is not enough), show the name-and-time
  // tooltip, and ring the dot. Shared by mouse hover and the touch tap.
  function hoverEventsAt(view, pt, clientX, clientY) {
    const key = view.key;
    const dots = view.evDots || [];
    let best = null, bestD = 1e9;
    for (const dt of dots) {
      const dx = pt.x - dt.cx, dy = pt.y - dt.cy, d = dx * dx + dy * dy;
      if (d < bestD) { bestD = d; best = dt; }
    }
    if (!best || bestD > 13 * 13) { hideHover(key); return; }
    LAST_HOVER[key] = { ts: best.e.x, name: best.e.name, oid: best.e.oid, cat: best.e.cat };
    if (clientX != null) {
      ttLive.innerHTML = eventsTooltipHTML(best);
      placeTooltip(ttLive, clientX, clientY);
    }
    view.overlay.replaceChildren();
    svgEl("circle", { cx: best.cx, cy: best.cy, r: 6, fill: "none", stroke: C.text,
                      "stroke-width": 1.5, "vector-effect": "non-scaling-stroke" }, view.overlay);
  }

  // ------------------------------------------------------------------
  // Pointer gestures: hover, drag (zoom-select / pan), wheel, dblclick
  // ------------------------------------------------------------------
  function vbPoint(view, ev) {
    const rect = view.svg.getBoundingClientRect();
    return {
      x: (ev.clientX - rect.left) / rect.width * view.geom.W,
      y: (ev.clientY - rect.top) / rect.height * view.geom.H,
    };
  }
  function attachInteractions(view) {
    const key = view.key;
    const cont = view.container;
    let drag = null;   // { startVbX, startClientX/Y, mode, winAtStart, moved, pending }
    const pointers = new Map();   // active pointerId -> {x, y} (touch tracking)
    let pinch = null;  // { startDist, winAtStart, midClientX }

    cont.addEventListener("pointermove", ev => {
      const st = STATE[key], spec = st.spec;
      if (!view.geom || !view.svg) return;
      // Inspect mode follows the panel: hovering a DIFFERENT panel than the
      // one being inspected drops the mode rather than leaving its cue
      // orphaned while arrows silently act elsewhere (same key — e.g. moving
      // the mouse inside that panel's own lightbox view — is not a move).
      if (INSPECT && INSPECT.key !== key) exitInspect();
      ACTIVE_KEY = key;
      const pt = vbPoint(view, ev);
      const g = view.geom;
      if (pointers.has(ev.pointerId)) pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (pinch) {
        if (pointers.size >= 2) {
          // Scale the window at pinch start around the fingers' midpoint.
          const pts = Array.from(pointers.values());
          const dist = Math.max(8, Math.abs(pts[0].x - pts[1].x));
          const scale = pinch.startDist / dist;
          const rect = view.svg.getBoundingClientRect();
          const midVbX = (pinch.midClientX - rect.left) / rect.width * g.W;
          const frac = Math.max(0, Math.min(1, (midVbX - g.p.l) / g.iw));
          const w = pinch.winAtStart;
          const center = w[0] + frac * (w[1] - w[0]);
          applyWindow(key, spec, [center - (center - w[0]) * scale,
                                  center + (w[1] - center) * scale]);
        }
        return;
      }
      if (drag) {
        if (drag.pending) {
          // Touch gesture arbitration: claim the drag only on clear
          // horizontal intent; a mostly-vertical move stays the page's
          // scroll (touch-action: pan-y does the actual scrolling).
          const dx = ev.clientX - drag.startClientX, dy = ev.clientY - drag.startClientY;
          if (Math.abs(dx) > 10 && Math.abs(dx) > Math.abs(dy) * 1.2) {
            drag.pending = false;
            cont.setPointerCapture && cont.setPointerCapture(ev.pointerId);
          } else if (Math.abs(dy) > 14) {
            drag = null;
            return;
          } else {
            return;
          }
        }
        const dxVb = pt.x - drag.startVbX;
        if (Math.abs(ev.clientX - drag.startClientX) > 4) drag.moved = true;
        if (drag.mode === "pan" && drag.moved) {
          const dt = -dxVb / g.iw * (drag.winAtStart[1] - drag.winAtStart[0]);
          applyWindow(key, spec, [drag.winAtStart[0] + dt, drag.winAtStart[1] + dt]);
        } else if (drag.mode === "select" && drag.moved) {
          drawSelection(view, drag.startVbX, pt.x);
        }
        return;
      }
      if (spec.kind === "line") {
        if (pt.x < g.p.l - 4 || pt.x > g.W - g.p.r + 4) { hideHover(key); return; }
        const ts = g.xd[0] + (pt.x - g.p.l) / g.iw * (g.xd[1] - g.xd[0]);
        hoverLineAt(key, ts, ev.clientX, ev.clientY, false);
      } else if (spec.kind === "bar") {
        const i = Math.floor((pt.x - g.p.l) / g.bw);
        const data = spec.data || [];
        if (i < 0 || i >= data.length || pt.x < g.p.l) { hideHover(key); return; }
        LAST_HOVER[key] = { label: data[i].label, y: data[i].y };
        ttLive.innerHTML = '<div class="tt-ts">' + data[i].label + "</div>" +
          '<div class="tt-row"><span class="tt-dot" style="background:' + (resolveColor(data[i].color) || resolveColor(spec.color) || C.blue) + '"></span>' +
          '<span class="tt-name">' + (spec.barName || "value") + '</span><span class="tt-val">' +
          fmtVal(data[i].y, spec.dec) + " " + (spec.unit || "") + "</span></div>";
        placeTooltip(ttLive, ev.clientX, ev.clientY);
        view.overlay.replaceChildren();
        // Same palette-derived overlay as the crosshair guide: the text role
        // at low opacity, so the highlighted bar shows on either theme.
        svgEl("rect", { x: g.p.l + i * g.bw + g.bw * 0.16 - 1.5, y: g.p.t, width: g.bw * 0.68 + 3, height: g.ih, fill: C.text, "fill-opacity": 0.05, stroke: C.text, "stroke-opacity": 0.18, "stroke-width": 1, rx: 3 }, view.overlay);
      } else if (spec.kind === "heatmap") {
        const c = Math.floor((pt.x - g.p.l) / g.cw), r = Math.floor((pt.y - g.p.t) / g.ch);
        if (c < 0 || c >= g.cols || r < 0 || r >= g.rows) { hideHover(key); return; }
        const v = spec.z[r][c];
        LAST_HOVER[key] = { day: spec.days[r], hour: c, value: v };
        ttLive.innerHTML = '<div class="tt-ts">' + spec.days[r] + " · " + p2(c) + ":00</div>" +
          '<div class="tt-row"><span class="tt-name">' + (S.meanPower || "mean power") +
          '</span><span class="tt-val">' +
          (v == null ? (S.noData || "no data") : fmtVal(v, 0) + " " + (spec.unit || "")) + "</span></div>";
        placeTooltip(ttLive, ev.clientX, ev.clientY);
        view.overlay.replaceChildren();
        // Same palette-derived outline as highlightHeatmap, not a hardcoded white.
        svgEl("rect", { x: g.p.l + c * g.cw, y: g.p.t + r * g.ch, width: g.cw, height: g.ch, fill: "none", stroke: C.text, "stroke-width": 1.2, "vector-effect": "non-scaling-stroke" }, view.overlay);
      } else if (spec.kind === "events") {
        hoverEventsAt(view, pt, ev.clientX, ev.clientY);
      }
    });

    cont.addEventListener("pointerleave", () => { if (!drag) hideHover(key); });

    // Keyboard focus (tabindex on the chart box) targets the shortcuts at
    // this panel without needing mouse hover.
    cont.addEventListener("focus", () => {
      if (INSPECT && INSPECT.key !== key) exitInspect();
      ACTIVE_KEY = key;
    });

    cont.addEventListener("pointerdown", ev => {
      const st = STATE[key], spec = st.spec;
      if (!view.geom || ev.button !== 0) return;
      if (!zoomCapable(spec)) return;
      if (ev.pointerType === "touch") pointers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (pointers.size === 2) {
        // A second finger turns the gesture into a pinch; abandon any
        // in-progress one-finger drag.
        drag = null;
        clearSelection(view);
        const pts = Array.from(pointers.values());
        pinch = {
          startDist: Math.max(8, Math.abs(pts[0].x - pts[1].x)),
          winAtStart: currentWindow(key, spec).slice(),
          midClientX: (pts[0].x + pts[1].x) / 2,
        };
        return;
      }
      const pt = vbPoint(view, ev);
      if (ev.pointerType === "touch") {
        // Touch drags start pending: they are only claimed once the move
        // shows horizontal intent (see pointermove), so vertical scrolling
        // over the chart keeps working.
        drag = {
          startVbX: pt.x, startClientX: ev.clientX, startClientY: ev.clientY,
          moved: false, pending: true, mode: "select",
          winAtStart: currentWindow(key, spec).slice(),
        };
        return;
      }
      // Without this, the drag also starts a native text selection; a later
      // drag that lands on that selection becomes a drag-and-drop and the
      // browser cancels our pointer mid-gesture (user-select: none on the
      // chart box is the second half of the defense).
      ev.preventDefault();
      // Consistent gestures: a plain drag ALWAYS draws a zoom selection
      // (never a mode-dependent pan, which read as confusing); holding
      // Shift turns the drag into a pan of the current window.
      drag = {
        startVbX: pt.x, startClientX: ev.clientX, startClientY: ev.clientY,
        moved: false, pending: false,
        mode: ev.shiftKey ? "pan" : "select",
        winAtStart: currentWindow(key, spec).slice(),
      };
      cont.setPointerCapture && cont.setPointerCapture(ev.pointerId);
    });

    cont.addEventListener("pointercancel", ev => {
      // The browser reclaimed the pointer (e.g. a native drag or scroll
      // gesture won): abandon the gesture instead of leaving a stuck drag.
      pointers.delete(ev.pointerId);
      pinch = null;
      if (drag) { drag = null; clearSelection(view); }
    });

    cont.addEventListener("pointerup", ev => {
      pointers.delete(ev.pointerId);
      if (pinch) {
        // Keep the pinched window; ignore the remaining finger until it
        // lifts too (resuming a one-finger pan mid-lift reads as a jump).
        if (pointers.size < 2) pinch = null;
        return;
      }
      if (!drag) return;
      const st = STATE[key], spec = st.spec;
      const pt = vbPoint(view, ev);
      const d = drag; drag = null;
      clearSelection(view);
      if (!d.moved) {
        // A plain click pins / unpins the tooltip. A touch tap has no hover
        // history, so synthesize the hover at the tap point first.
        if (ev.pointerType === "touch" && spec.kind === "line") {
          const g = view.geom;
          if (pt.x >= g.p.l - 4 && pt.x <= g.W - g.p.r + 4) {
            const ts = g.xd[0] + (pt.x - g.p.l) / g.iw * (g.xd[1] - g.xd[0]);
            hoverLineAt(key, ts, ev.clientX, ev.clientY, false);
          }
        } else if (ev.pointerType === "touch" && spec.kind === "events") {
          hoverEventsAt(view, pt, ev.clientX, ev.clientY);
        }
        togglePin(key, ev);
        return;
      }
      if (d.mode === "select") {
        const g = view.geom;
        let a = Math.min(d.startVbX, pt.x), b = Math.max(d.startVbX, pt.x);
        if (b - a < 8) return;   // too small to be an intentional zoom
        const w = d.winAtStart;
        const t0 = w[0] + (Math.max(a, g.p.l) - g.p.l) / g.iw * (w[1] - w[0]);
        const t1 = w[0] + (Math.min(b, g.W - g.p.r) - g.p.l) / g.iw * (w[1] - w[0]);
        applyWindow(key, spec, [t0, t1]);
      }
    });

    cont.addEventListener("dblclick", () => {
      const spec = STATE[key].spec;
      if (zoomCapable(spec)) resetZoom(key);
    });

    cont.addEventListener("wheel", ev => {
      const st = STATE[key], spec = st.spec;
      if (!zoomCapable(spec) || !view.geom) return;
      ev.preventDefault();
      const g = view.geom;
      const pt = vbPoint(view, ev);
      const w = currentWindow(key, spec);
      const frac = Math.max(0, Math.min(1, (pt.x - g.p.l) / g.iw));
      const center = w[0] + frac * (w[1] - w[0]);
      const factor = ev.deltaY > 0 ? 1.25 : 0.8;
      const t0 = center - (center - w[0]) * factor;
      const t1 = center + (w[1] - center) * factor;
      applyWindow(key, spec, [t0, t1]);
    }, { passive: false });
  }
  function zoomCapable(spec) {
    // Time-axis line panels and the event timeline zoom/pan; linear-axis and
    // categorical-value panels (bar, heatmap) do not.
    return spec && (spec.kind === "line" || spec.kind === "events") && spec.xkind !== "linear";
  }
  function currentWindow(key, spec) {
    const st = STATE[key];
    return (st && st.zoom) || autoXDomain(spec);
  }
  function applyWindow(key, spec, win) {
    // Local to one panel — a gesture never zooms or pans any other chart.
    if (!win || !isFinite(win[0]) || !isFinite(win[1])) return;
    const st = STATE[key];
    const full = autoXDomain(spec);
    let [t0, t1] = win;
    const minSpan = Math.min(60e3, full[1] - full[0]);
    if (t1 - t0 < minSpan) { const c = (t0 + t1) / 2; t0 = c - minSpan / 2; t1 = c + minSpan / 2; }
    if (t0 < full[0]) { t1 = Math.min(full[1], t1 + (full[0] - t0)); t0 = full[0]; }
    if (t1 > full[1]) { t0 = Math.max(full[0], t0 - (t1 - full[1])); t1 = full[1]; }
    const isFull = t0 <= full[0] + 1 && t1 >= full[1] - 1;
    st.zoom = isFull ? null : [t0, t1];
    LAST_PRESET = null;
    rerenderPanel(key);
    updatePresetPills();
    updateHash();
  }
  function resetZoom(key) {
    const st = STATE[key];
    if (!st) return;
    st.zoom = null;
    LAST_PRESET = null;
    rerenderPanel(key);
    updatePresetPills();
    updateHash();
  }

  // ------------------------------------------------------------------
  // Anomaly jump: cycle the panel window through the marker clusters.
  // Markers closer together than twice the padding read as one event and
  // are framed as a single view instead of two nearly identical jumps.
  // ------------------------------------------------------------------
  const ANOM_PAD_MS = 6 * 3600e3;
  function anomalyClusters(key) {
    const st = STATE[key];
    if (!st || !st.spec || !(st.spec.markers || []).length) return [];
    if (!st.anomClusters) {
      const xs = st.spec.markers.map(m => m.x).sort((a, b) => a - b);
      const clusters = [];
      xs.forEach(x => {
        const last = clusters[clusters.length - 1];
        if (last && x - last[1] <= 2 * ANOM_PAD_MS) last[1] = x;
        else clusters.push([x, x]);
      });
      st.anomClusters = clusters;
    }
    return st.anomClusters;
  }
  function jumpAnomaly(key) {
    const clusters = anomalyClusters(key);
    if (!clusters.length) return;
    const st = STATE[key];
    st.anomIdx = ((st.anomIdx == null ? -1 : st.anomIdx) + 1) % clusters.length;
    const c = clusters[st.anomIdx];
    applyWindow(key, st.spec, [c[0] - ANOM_PAD_MS, c[1] + ANOM_PAD_MS]);
  }
  function drawSelection(view, x0, x1) {
    const g = view.geom;
    view.overlay.replaceChildren();
    const a = Math.max(g.p.l, Math.min(x0, x1)), b = Math.min(g.W - g.p.r, Math.max(x0, x1));
    svgEl("rect", { x: a, y: g.p.t, width: Math.max(0, b - a), height: g.ih, fill: "rgba(90,169,240,.14)", stroke: "rgba(90,169,240,.5)", "stroke-width": 1, "vector-effect": "non-scaling-stroke" }, view.overlay);
  }
  function clearSelection(view) {
    if (view.overlay) view.overlay.replaceChildren();
  }

  function togglePin(key, ev) {
    if (PINNED && PINNED.key === key) { unpin(); return; }
    if (!LAST_HOVER[key] || ttLive.hidden) { unpin(); return; }
    PINNED = { key, ...LAST_HOVER[key] };
    ttPin.innerHTML = ttLive.innerHTML;
    ttPin.hidden = false;
    placeTooltip(ttPin, ev.clientX, ev.clientY);
  }
  function unpin() {
    PINNED = null;
    if (ttPin) ttPin.hidden = true;
  }

  // ------------------------------------------------------------------
  // Card tools: reset / png / csv / expand — bound by data-panel attribute
  // ------------------------------------------------------------------
  function bindTools() {
    document.querySelectorAll(".tool-reset").forEach(b => {
      b.addEventListener("click", () => resetZoom(b.dataset.panel));
    });
    document.querySelectorAll(".tool-png").forEach(b => {
      b.addEventListener("click", () => exportPng(b.dataset.panel));
    });
    document.querySelectorAll(".tool-csv").forEach(b => {
      b.addEventListener("click", () => exportCsv(b.dataset.panel));
    });
    document.querySelectorAll(".tool-expand").forEach(b => {
      b.addEventListener("click", () => openLightbox(b.dataset.panel));
    });
    document.querySelectorAll(".tool-anom").forEach(b => {
      b.addEventListener("click", () => jumpAnomaly(b.dataset.panel));
    });
    document.querySelectorAll(".preset-pill").forEach(b => {
      b.addEventListener("click", () => applyPreset(b.dataset.days));
    });
    document.querySelectorAll(".cmp-pill").forEach(b => {
      b.addEventListener("click", () => { if (!b.disabled) setCmpSelection(b.dataset.mode); });
    });
    document.querySelectorAll(".cmp-period-select").forEach(sel => {
      sel.addEventListener("change", () => { if (sel.value) setCmpSelection(sel.value); });
    });
    const pb = document.getElementById("print-btn");
    if (pb) pb.addEventListener("click", () => window.print());
    const tb = document.getElementById("theme-btn");
    if (tb) tb.addEventListener("click", () => cycleTheme());
  }

  function pngCanvas(key) {
    // Rasterize one panel's SVG to a 2x canvas. The SVG carries only concrete
    // color attributes (chart ink is resolved from the active palette at draw
    // time, never a CSS variable), so the serialized image reflects the active
    // theme — the card background is baked in from the active palette's panel
    // color. Returns a Promise so both the download and the test hook can use
    // the pixels once the image has decoded.
    return new Promise((resolve, reject) => {
      const st = STATE[key];
      const view = st && st.views[0];
      if (!view || !view.svg || !view.geom) { reject(new Error("no view")); return; }
      const g = view.geom;
      const clone = view.svg.cloneNode(true);
      const bg = svgEl("rect", { x: 0, y: 0, width: g.W, height: g.H, fill: C.panel });
      clone.insertBefore(bg, clone.firstChild);
      clone.setAttribute("width", g.W * 2);
      clone.setAttribute("height", g.H * 2);
      const xml = new XMLSerializer().serializeToString(clone);
      const img = new Image();
      img.onload = () => {
        const canvas = document.createElement("canvas");
        canvas.width = g.W * 2; canvas.height = g.H * 2;
        canvas.getContext("2d").drawImage(img, 0, 0);
        resolve(canvas);
      };
      img.onerror = reject;
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(xml);
    });
  }

  function exportPng(key) {
    pngCanvas(key).then(canvas => {
      canvas.toBlob(blob => { if (blob) download("ups-" + key + ".png", blob); }, "image/png");
    }).catch(() => { /* nothing rendered to export */ });
  }

  function exportCsv(key) {
    const st = STATE[key];
    if (!st || !st.spec) return;
    const spec = st.spec;
    let csv = "";
    if (spec.kind === "line") {
      const series = (spec.series || []).filter((s, si) => !st.hidden.has(si));
      if (!series.length) return;
      const headTime = spec.xkind === "linear" ? (spec.xunit || "x") : "time";
      csv = headTime + "," + series.map(s => '"' + s.name.replace(/"/g, '""') + '"').join(",") + "\n";
      // Union of x values across visible series (they usually share x).
      const base = series[0];
      for (let i = 0; i < base.x.length; i++) {
        const t = base.x[i];
        const cells = series.map(s => {
          const j = (s === base) ? i : nearestIdx(s.x, t);
          return (j >= 0 && Math.abs(s.x[j] - t) < 1e9) ? s.y[j] : "";
        });
        csv += (spec.xkind === "linear" ? t : fmtFull(t)) + "," + cells.join(",") + "\n";
      }
    } else if (spec.kind === "bar") {
      csv = "label,value\n";
      (spec.data || []).forEach(d => { csv += '"' + String(d.label).replace(/"/g, '""') + '",' + d.y + "\n"; });
    } else if (spec.kind === "heatmap") {
      csv = "day," + Array.from({ length: 24 }, (_, h) => p2(h) + ":00").join(",") + "\n";
      spec.days.forEach((dl, r) => { csv += dl + "," + spec.z[r].map(v => v == null ? "" : v).join(",") + "\n"; });
    } else if (spec.kind === "events") {
      // Machine-standard en-US: the stable category key (not the localized
      // row label), the ObjectId, and the resolved event name. Hidden
      // category rows are excluded, matching the line panel's hidden-series
      // behavior.
      const hiddenCats = new Set();
      (spec.rows || []).forEach((r, i) => { if (st.hidden.has(i)) hiddenCats.add(r.cat); });
      csv = "time,category,event id,event name\n";
      (spec.events || []).slice().sort((a, b) => a.x - b.x).forEach(e => {
        if (hiddenCats.has(e.cat)) return;
        csv += fmtFull(e.x) + ",\"" + String(e.cat).replace(/"/g, '""') + "\",\"" +
          String(e.oid).replace(/"/g, '""') + "\",\"" + String(e.name).replace(/"/g, '""') + "\"\n";
      });
    }
    if (csv) download("ups-" + key + ".csv", new Blob([csv], { type: "text/csv" }));
  }

  // ------------------------------------------------------------------
  // Lightbox (card expand)
  // ------------------------------------------------------------------
  function openLightbox(key) {
    closeLightbox();
    const st = STATE[key];
    if (!st) return;
    // Opening a DIFFERENT panel's lightbox with no hover/focus crossing would
    // otherwise leave inspect mode stuck on the previous panel (item B2) —
    // drop it, the same rule pointermove/focus already apply when the active
    // panel changes.
    if (INSPECT && INSPECT.key !== key) exitInspect();
    const lb = document.getElementById("lightbox");
    const box = document.getElementById("lightbox-chart");
    const title = document.getElementById("lightbox-title");
    if (!lb || !box) return;
    title.textContent = st.title || key;
    lb.hidden = false;
    const view = {
      key, id: "lb", container: box, spec: st.spec,
      vb: [1280, Math.round(1280 * (st.spec.kind === "heatmap" ? 0.34 : 0.36))],
    };
    st.views.push(view);
    renderView(view);
    attachInteractions(view);
    LIGHTBOX_KEY = key;
    // If this lightbox shows the very panel being inspected, carry the inspect
    // cue and the current sample onto the expanded view too (item B2).
    if (INSPECT && INSPECT.key === key) {
      updateInspectCue(key, true);
      showInspectSample(key);
    }
  }
  function closeLightbox() {
    const lb = document.getElementById("lightbox");
    if (!lb || lb.hidden) { LIGHTBOX_KEY = null; return; }
    lb.hidden = true;
    const key = LIGHTBOX_KEY;
    LIGHTBOX_KEY = null;
    // Clear any inspect cue mirrored onto the lightbox view (item B2); the
    // grid card keeps its own cue if inspect mode is still active there.
    const lbBox = document.getElementById("lightbox-chart");
    if (lbBox) lbBox.classList.remove("is-inspecting");
    const lbBadge = document.getElementById("lightbox-inspect-badge");
    if (lbBadge) lbBadge.hidden = true;
    if (key && STATE[key]) STATE[key].views = STATE[key].views.filter(v => v.id !== "lb");
    const box = document.getElementById("lightbox-chart");
    if (box) box.replaceChildren();
  }

  // ------------------------------------------------------------------
  // Keyboard sample step-through ("inspect mode", roadmap item 21): Enter
  // toggles it on the focused chart, ArrowLeft/ArrowRight then walk the
  // panel's full sample array — never the decimated index decimateMinMax
  // renders, so a step never skips a sample at a wide zoom — one entry at a
  // time, and Escape leaves it. Only line-kind panels qualify: bar and
  // heatmap have no per-sample index to walk, and the event timeline's dots
  // are categorical occurrences found by 2D nearest-dot matching
  // (hoverEventsAt), not a single ordered array, so it is excluded rather
  // than bolted onto a different tooltip path.
  // ------------------------------------------------------------------
  function inspectCapable(spec) {
    return !!(spec && spec.kind === "line" && (spec.series || []).some(s => s.x && s.x.length));
  }
  function inspectRefSeries(spec, st) {
    // The first visible series with data is the walking reference — for
    // most panels that is the one full-length raw series (overlays like a
    // rolling mean or a fitted trend line are shorter derived series).
    const series = spec.series || [];
    for (let si = 0; si < series.length; si++) {
      if (!st.hidden.has(si) && series[si].x.length) return series[si];
    }
    return series[0] || null;
  }
  function activeInspectView(key) {
    const st = STATE[key];
    if (!st) return null;
    const id = (LIGHTBOX_KEY === key) ? "lb" : "grid";
    return st.views.find(v => v.id === id) || st.views[0] || null;
  }
  function updateInspectCue(key, active) {
    const box = document.getElementById("panel-" + key);
    if (box) box.classList.toggle("is-inspecting", active);
    document.querySelectorAll('.inspect-badge[data-panel="' + key + '"]')
      .forEach(b => { b.hidden = !active; });
    // Mirror the cue onto the lightbox view when it is showing the very panel
    // being inspected (item B2), so an expanded chart under keyboard inspect
    // carries the same amber outline and badge as its grid card.
    const onLightbox = active && LIGHTBOX_KEY === key;
    const lbBox = document.getElementById("lightbox-chart");
    if (lbBox) lbBox.classList.toggle("is-inspecting", onLightbox);
    const lbBadge = document.getElementById("lightbox-inspect-badge");
    if (lbBadge) lbBadge.hidden = !onLightbox;
  }
  function showInspectSample(key) {
    const st = STATE[key], spec = st.spec;
    const ref = inspectRefSeries(spec, st);
    if (!ref) return;
    // The reference series can shrink out from under a live inspect cursor
    // (for example a cmp baseline switch rebuilds the panel with a shorter
    // series). stepInspect already clamps on every step; clamp here too so a
    // rebuild does not leave INSPECT.idx pointing past the new array's end.
    INSPECT.idx = Math.max(0, Math.min(ref.x.length - 1, INSPECT.idx));
    const ts = ref.x[INSPECT.idx];
    const view = activeInspectView(key);
    let clientX = null, clientY = null;
    if (view && view.geom && view.svg) {
      const g = view.geom, rect = view.svg.getBoundingClientRect();
      clientX = rect.left + (g.sx(ts) / g.W) * rect.width;
      clientY = rect.top + ((g.p.t + g.ih / 2) / g.H) * rect.height;
    }
    // Reuses the exact hover code path: crosshair, series dots, and tooltip
    // content are identical to what a mouse hover at this x would show.
    hoverLineAt(key, ts, clientX, clientY, false);
  }
  function announceInspect(key) {
    const live = document.getElementById("inspect-live");
    const hov = LAST_HOVER[key];
    if (!live || !hov) return;
    const spec = STATE[key].spec;
    const tsLabel = (spec.xkind === "linear")
      ? fmt(hov.ts) + (spec.xunit ? " " + spec.xunit : "")
      : fmtFull(hov.ts);
    // Reuses the tooltip's own name and formatted value+unit strings, so the
    // announcement text cannot drift from what is visibly shown, and any
    // localization already baked into those strings rides along for free.
    const parts = hov.rows.map(r => r.name + " " + r.val);
    live.textContent = tsLabel + ", " + parts.join(", ");
  }
  function toggleInspect(key) {
    if (INSPECT && INSPECT.key === key) { exitInspect(); return; }
    if (INSPECT) exitInspect();
    const st = STATE[key], spec = st.spec;
    const ref = inspectRefSeries(spec, st);
    if (!ref || !ref.x.length) return;
    const w = currentWindow(key, spec);
    INSPECT = { key, idx: nearestIdx(ref.x, (w[0] + w[1]) / 2) };
    updateInspectCue(key, true);
    showInspectSample(key);   // entering the mode is not itself a step: no announce
  }
  function exitInspect() {
    if (!INSPECT) return;
    const key = INSPECT.key;
    INSPECT = null;
    updateInspectCue(key, false);
    hideHover(key);
  }
  function stepInspect(dir) {
    if (!INSPECT) return;
    const key = INSPECT.key;
    const st = STATE[key];
    const ref = st && inspectRefSeries(st.spec, st);
    if (!ref || !ref.x.length) return;
    INSPECT.idx = Math.max(0, Math.min(ref.x.length - 1, INSPECT.idx + dir));
    showInspectSample(key);
    announceInspect(key);
  }

  // ------------------------------------------------------------------
  // Keyboard: arrows pan, +/- zoom, 0 reset, Esc unpin/close
  // ------------------------------------------------------------------
  function bindKeyboard() {
    document.addEventListener("keydown", ev => {
      if (ev.target && /INPUT|TEXTAREA|SELECT/.test(ev.target.tagName)) return;
      if (ev.key === "Escape") {
        unpin();
        closeLightbox();
        exitInspect();
        return;
      }
      const key = LIGHTBOX_KEY || ACTIVE_KEY;
      if (!key || !STATE[key]) return;
      const spec = STATE[key].spec;
      if (ev.key === "Enter") {
        if (inspectCapable(spec)) { toggleInspect(key); ev.preventDefault(); }
        return;
      }
      if (INSPECT && INSPECT.key === key && (ev.key === "ArrowLeft" || ev.key === "ArrowRight")) {
        stepInspect(ev.key === "ArrowRight" ? 1 : -1);
        ev.preventDefault();
        return;
      }
      if (!zoomCapable(spec)) return;
      const w = currentWindow(key, spec);
      const span = w[1] - w[0];
      if (ev.key === "ArrowLeft") applyWindow(key, spec, [w[0] - span * 0.1, w[1] - span * 0.1]);
      else if (ev.key === "ArrowRight") applyWindow(key, spec, [w[0] + span * 0.1, w[1] + span * 0.1]);
      else if (ev.key === "+" || ev.key === "=") applyWindow(key, spec, [w[0] + span * 0.125, w[1] - span * 0.125]);
      else if (ev.key === "-" || ev.key === "_") applyWindow(key, spec, [w[0] - span * 0.125, w[1] + span * 0.125]);
      else if (ev.key === "0") resetZoom(key);
      else return;
      ev.preventDefault();
    });
  }

  // ------------------------------------------------------------------
  // Load reveal: one-time clip-path sweep, staggered per panel.
  // ------------------------------------------------------------------
  function reveal() {
    const panels = Object.keys(STATE).filter(k => STATE[k].views.length);
    const sparks = Array.from(document.querySelectorAll(".kpi-spark svg"));
    const total = panels.length + sparks.length;
    if (NOANIM || total === 0) { READY = true; return; }
    let done = 0;
    const finish = () => { if (++done >= total) READY = true; };
    const animate = (svgRoot, i) => {
      if (!svgRoot) { finish(); return; }
      const vb = svgRoot.getAttribute("viewBox").split(" ").map(Number);
      const W = vb[2], H = vb[3];
      const clipId = "reveal-" + i + "-" + Math.floor(W);
      const defs = svgEl("defs", {}, svgRoot);
      const cp = svgEl("clipPath", { id: clipId }, defs);
      const rect = svgEl("rect", { x: 0, y: 0, width: 0, height: H }, cp);
      const g = svgEl("g", { "clip-path": "url(#" + clipId + ")" });
      // Wrap all existing children (except defs) in the clipped group.
      Array.from(svgRoot.childNodes).forEach(ch => {
        if (ch !== defs && ch.tagName !== "defs") g.appendChild(ch);
      });
      svgRoot.appendChild(g);
      const t0 = performance.now() + i * 60;
      const dur = 700;
      const tick = now => {
        const t = Math.max(0, Math.min(1, (now - t0) / dur));
        const e = 1 - Math.pow(1 - t, 3);   // ease-out cubic
        rect.setAttribute("width", (W * e).toFixed(1));
        if (t < 1) requestAnimationFrame(tick);
        else {
          // Unwrap: remove the clip so later re-renders are unaffected.
          rect.setAttribute("width", W);
          finish();
        }
      };
      requestAnimationFrame(tick);
    };
    let i = 0;
    panels.forEach(k => { animate(STATE[k].views[0].svg, i++); });
    sparks.forEach(s => { animate(s, i++); });
  }

  // ------------------------------------------------------------------
  // Staleness badge (age of the newest DataLog sample at page-open time)
  // ------------------------------------------------------------------
  function fillStaleness() {
    const el = document.getElementById("staleness");
    if (!el || !DATA.meta || !DATA.meta.last_sample_ms) return;
    // last_sample_ms is wall-clock-as-UTC; compare against the same encoding
    // of "now" so the difference is real elapsed time.
    const now = new Date();
    const nowEnc = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate(),
      now.getHours(), now.getMinutes(), now.getSeconds());
    const ageMin = Math.max(0, (nowEnc - DATA.meta.last_sample_ms) / 60e3);
    let t;
    if (ageMin < 90) t = Math.round(ageMin) + " min";
    else if (ageMin < 48 * 60) t = (ageMin / 60).toFixed(1) + " h";
    else t = (ageMin / 1440).toFixed(1) + " d";
    el.textContent = (S.staleness || "last sample {t} ago").replace("{t}", t);
    const expected = (DATA.meta.expected_interval_min || 20) * 2;
    if (ageMin > expected) el.classList.add("is-stale");
  }

  // ------------------------------------------------------------------
  // Reset (used by the E2E fixtures between tests) + debug surface
  // ------------------------------------------------------------------
  function resetAll() {
    unpin();
    closeLightbox();
    if (INSPECT) updateInspectCue(INSPECT.key, false);
    INSPECT = null;
    const live = document.getElementById("inspect-live");
    if (live) live.textContent = "";
    if (ttLive) ttLive.hidden = true;
    TIME.hoverTs = null;
    LAST_PRESET = null;
    CMP_SEL = "previous";
    // The theme override clears back to the config default (roadmap item 30),
    // so a reset restores the palette the build shipped with.
    THEME_MODE = CONFIG_THEME;
    C = PALETTES[resolveTheme(THEME_MODE)] || PALETTES.dark || {};
    document.documentElement.setAttribute("data-theme", THEME_MODE);
    updateThemeToggle();
    rebuildCmpSeries();
    updateCmpControls();
    Object.keys(STATE).forEach(k => {
      STATE[k].zoom = null;
      // Re-seed the default filter rather than clearing it, so the event
      // timeline's housekeeping-off default survives a between-test reset.
      STATE[k].hidden = defaultHidden(STATE[k].spec);
      STATE[k].anomIdx = null;
      delete LAST_HOVER[k];
    });
    Object.keys(STATE).forEach(rerenderPanel);
    renderSparks();
    updatePresetPills();
    updateHash();
    READY = true;   // reveals are long gone by the time tests reset
  }

  window.__chartsDebug = {
    ready: () => READY,
    panelKeys: () => Object.keys(STATE).filter(k => STATE[k].views.length > 0),
    xwin: k => (STATE[k] ? currentWindow(k, STATE[k].spec).slice() : null),
    fullXwin: k => (STATE[k] ? autoXDomain(STATE[k].spec) : null),
    setWindow: (t0, t1) => setWindow(t0, t1),
    zoom: k => (STATE[k] && STATE[k].zoom ? STATE[k].zoom.slice() : null),
    isZoomed: k => isZoomed(k),
    hover: k => LAST_HOVER[k] || null,
    pinned: () => (PINNED ? { key: PINNED.key } : null),
    hidden: k => (STATE[k] ? Array.from(STATE[k].hidden) : []),
    lightbox: () => LIGHTBOX_KEY,
    openLightbox,
    cmpSelection: () => ({
      mode: CMP_SEL,
      baseline: (cmpResolveBaseline(CMP_SEL) || {}).label || null,
      current: (cmpCurrentPeriod() || {}).label || null,
    }),
    setCmpSelection: sel => setCmpSelection(sel),
    anomalyClusters: k => anomalyClusters(k).map(c => c.slice()),
    jumpAnomaly,
    inspect: () => (INSPECT ? { key: INSPECT.key, idx: INSPECT.idx } : null),
    // Theme (roadmap item 30): the current mode ("auto"|"dark"|"light") and the
    // resolved active palette theme ("dark"|"light"), plus programmatic control.
    themeMode: () => THEME_MODE,
    theme: () => resolveTheme(THEME_MODE),
    setTheme: mode => setTheme(mode),
    cycleTheme,
    pngDataUrl: key => pngCanvas(key).then(canvas => canvas.toDataURL("image/png")),
    resetAll,
  };

  // The set of row indices hidden by default. For the event timeline this is
  // the payload's default filter (communication/monitoring housekeeping off);
  // every other panel starts with nothing hidden. resetAll re-applies this so
  // the default filter survives a between-test reset instead of being cleared
  // to "everything visible".
  function defaultHidden(spec) {
    const s = new Set();
    if (spec && spec.kind === "events") {
      (spec.rows || []).forEach((r, i) => { if (!r.visible) s.add(i); });
    }
    return s;
  }

  // ------------------------------------------------------------------
  // Init
  // ------------------------------------------------------------------
  function init() {
    ensureTooltips();
    Object.entries(DATA.panels || {}).forEach(([key, spec]) => {
      const cont = document.getElementById("panel-" + key);
      if (!cont) return;
      if (!spec || (spec.kind === "line" && !(spec.series || []).some(s => s.x.length)) ||
        (spec.kind === "bar" && !(spec.data || []).length) ||
        (spec.kind === "heatmap" && !(spec.days || []).length) ||
        (spec.kind === "events" && !(spec.events || []).length)) {
        const d = htmlEl("div", "chart-empty", cont);
        d.textContent = S.empty || "no data in the analyzed window";
        STATE[key] = { spec: spec || { kind: "empty" }, zoom: null, hidden: new Set(), views: [], title: cont.dataset.title || key };
        return;
      }
      const view = { key, id: "grid", container: cont, spec, vb: spec.vb || [460, 250] };
      STATE[key] = { spec, zoom: null, hidden: defaultHidden(spec), views: [view], title: cont.dataset.title || key };
      renderView(view);
      attachInteractions(view);
      updateResetPill(key);
    });

    // KPI sparklines.
    renderSparks();

    bindTools();
    bindKeyboard();
    fillStaleness();
    updatePresetPills();
    updateCmpControls();
    updateThemeToggle();
    // In "auto" mode, follow a live OS light/dark switch: the CSS chrome
    // reacts on its own through prefers-color-scheme, and this redraws the
    // chart ink to match (roadmap item 30).
    if (window.matchMedia) {
      const mq = matchMedia("(prefers-color-scheme: light)");
      const onSchemeChange = () => { if (THEME_MODE === "auto") applyTheme(); };
      if (mq.addEventListener) mq.addEventListener("change", onSchemeChange);
      else if (mq.addListener) mq.addListener(onSchemeChange);
    }
    restoreFromHash();
    // Long archives open on the 30-day preset so the initial view stays
    // readable; a permalink hash (restored above) takes precedence.
    if (DATA.meta && DATA.meta.default_preset_days && !location.hash) {
      applyPreset(String(DATA.meta.default_preset_days));
    }
    const lb = document.getElementById("lightbox");
    if (lb) lb.addEventListener("click", ev => { if (ev.target === lb || ev.target.classList.contains("lightbox-close")) closeLightbox(); });
    reveal();
    if (Object.keys(STATE).length === 0) READY = true;
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
</script>
