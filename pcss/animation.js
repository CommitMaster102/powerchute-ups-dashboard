<script>
(function() {
  const ANIM_DATA = __ANIM_DATA__;
  const STEPPERS = {};   // legacy alias kept for the test harness
  const RAF = {};        // group -> requestAnimationFrame id
  const SPEED = {};      // group -> playback rate (1.0 default)
  const EASING = {};     // group -> easing function name
  const ORIG = {};       // original trace data, captured once
  // Easing curves on [0,1]. Apply to the linear time `t` from rAF before
  // feeding into applyAtT — only the *visual rate* changes; the underlying
  // data and the total animation duration stay untouched. The user can
  // pick any of these per panel from the easing dropdown.
  const EASINGS = {
    "linear":       t => t,
    "ease-in":      t => t * t,
    "ease-out":     t => 1 - (1 - t) * (1 - t),
    "ease-in-out":  t => t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2,
    "cubic-in":     t => t * t * t,
    "cubic-out":    t => 1 - Math.pow(1 - t, 3),
    "cubic-in-out": t => t < 0.5 ? 4*t*t*t : 1 - Math.pow(-2*t + 2, 3) / 2,
    "smooth":       t => t * t * (3 - 2 * t),  // smoothstep, classic
  };
  // Per-group state machine.
  //   STATE[g] : 'idle' | 'playing' | 'paused' | 'ended'
  //   SAVED_T[g] : last virtual t (0..1) when paused — used to resume
  // Used by the play/pause buttons to decide what action to take and
  // which buttons are enabled. The pause button doubles as resume:
  // playing → pause, paused → resume. When the animation is at the end
  // (t === 1) pause is disabled — you can't pause nothing, and per
  // user request we don't auto-restart.
  const STATE = {};
  const SAVED_T = {};
  const LAST_KEY = {};   // group -> last frame key applied; skips redundant restyle
  let target = null;

  function gd() { return document.getElementsByClassName("plotly-graph-div")[0]; }

  function panelAxesFor(a) {
    // The subplot an animation lives on, read from its first trace.
    const idxs = a.trace_indices || (a.trace_idx != null ? [a.trace_idx] : []);
    const tr = idxs.length ? target.data[idxs[0]] : null;
    if (!tr) return null;
    return {
      xa: (tr.xaxis || "x").replace("x", "xaxis"),
      ya: (tr.yaxis || "y").replace("y", "yaxis"),
    };
  }
  function positionOverlays() {
    // Place each overlay in the gap BELOW its panel, under the x-axis month/
    // tick labels, right-aligned to the panel — so the controls never cover the
    // plotted data or panel annotations (e.g. the "80% threshold" label). All
    // positions are derived from the live axis domains and _fullLayout._size
    // (the real plot-area box in px), so they track margin/legend/viewport
    // changes instead of drifting against an assumed geometry.
    if (!target || !target._fullLayout) return;
    const fl = target._fullLayout;
    const W = fl.width, H = fl.height;
    const sz = fl._size || {
      l: fl.margin.l, t: fl.margin.t,
      w: W - fl.margin.l - fl.margin.r, h: H - fl.margin.t - fl.margin.b,
    };
    const parent = target.parentElement;
    if (!parent) return;
    const gdRect = target.getBoundingClientRect();
    const pRect = parent.getBoundingClientRect();
    const offTop = gdRect.top - pRect.top;
    const offLeft = gdRect.left - pRect.left;
    document.querySelectorAll('.anim-overlay').forEach(ov => {
      const a = ANIM_DATA[ov.dataset.group];
      if (!a) return;
      const ax = panelAxesFor(a);
      if (!ax || !fl[ax.xa] || !fl[ax.ya]) return;
      const xdom = fl[ax.xa].domain, ydom = fl[ax.ya].domain;
      if (!xdom || !ydom) return;
      const rightPx = offLeft + sz.l + xdom[1] * sz.w;       // panel data-area right edge
      const bottomPx = offTop + sz.t + (1 - ydom[0]) * sz.h; // panel data-area BOTTOM edge
      ov.style.left = "";
      ov.style.right = (pRect.width - rightPx + 6) + "px";
      // Drop below the x-axis tick labels (the first tick shows a 2-line
      // "Mon DD / YYYY", ~30px) into the inter-panel gap.
      ov.style.top = (bottomPx + 40) + "px";
      ov.style.visibility = "visible";
    });
  }
  function toMs(x) {
    // Plotly serializes pandas datetimes as ISO strings with NO timezone
    // suffix ("2026-04-27T23:18:52.000000"). Per ECMAScript spec, that
    // form is parsed as LOCAL time. But our Python-side cutoffs use
    // pd.Timestamp.value (naive treated as UTC). Force UTC interpretation
    // by appending 'Z' so both sides line up — otherwise we get a
    // tz-offset mismatch (6h in CR) that empties every cumulative trace.
    if (x == null) return NaN;
    if (typeof x === "number") return x;
    if (x instanceof Date) return x.getTime();
    if (typeof x === "string") {
      const hasTz = /[zZ]$|[+-]\d\d:?\d\d$/.test(x);
      return Date.parse(hasTz ? x : x + "Z");
    }
    return NaN;
  }
  function setTime(g, idx) {
    const lbl = document.querySelector('.anim-time[data-group="'+g+'"]');
    if (lbl) lbl.textContent = ANIM_DATA[g].labels[idx];
  }
  function clearStepper(g) {
    // Cancel both legacy interval and rAF — defensive in case of mixed state.
    if (STEPPERS[g]) { clearInterval(STEPPERS[g]); STEPPERS[g] = null; }
    if (RAF[g]) { cancelAnimationFrame(RAF[g]); RAF[g] = null; }
    const playBtn = document.querySelector('.anim-play[data-group="'+g+'"]');
    if (playBtn) playBtn.classList.remove("is-active");
  }
  function refreshButtons(g) {
    const playBtn = document.querySelector('.anim-play[data-group="'+g+'"]');
    const pauseBtn = document.querySelector('.anim-pause[data-group="'+g+'"]');
    if (!playBtn || !pauseBtn) return;
    const st = STATE[g] || "idle";
    // Pause button title text reflects what next click will do
    if (st === "paused") {
      pauseBtn.disabled = false;
      pauseBtn.classList.add("is-resume");
      pauseBtn.title = "Continuar";
    } else if (st === "playing") {
      pauseBtn.disabled = false;
      pauseBtn.classList.remove("is-resume");
      pauseBtn.title = "Pausar";
    } else {
      // idle or ended → nothing to pause / resume
      pauseBtn.disabled = true;
      pauseBtn.classList.remove("is-resume");
      pauseBtn.title = "Sin animación que pausar";
    }
    // Play is enabled except while actively playing (it would just
    // restart, which would feel jarring; let the user pause first).
    playBtn.disabled = (st === "playing");
    if (st === "playing") playBtn.classList.add("is-active");
    else playBtn.classList.remove("is-active");
  }
  function setState(g, next) {
    STATE[g] = next;
    refreshButtons(g);
  }
  function safeArray(v) {
    if (v == null) return [];
    if (Array.isArray(v)) return v.slice();
    if (typeof v === "string") return [v];
    if (typeof v.length === "number") return Array.from(v);
    return [];
  }
  function readTraceArr(idx, key) {
    // Plotly 3.x binary-encodes large numeric trace arrays as
    // {dtype, bdata, _inputArray} on gd.data[i]. The encoded blob has no
    // .length so safeArray() returns []. Three fallbacks, in order:
    //   1) plain Array (string datetimes, small numeric arrays)
    //   2) _inputArray on the blob (Plotly preserves the original)
    //   3) gd._fullData[i][key] (decoded Float64Array)
    const tr = target.data[idx];
    const raw = tr && tr[key];
    if (Array.isArray(raw)) return raw.slice();
    if (raw && raw._inputArray && Array.isArray(raw._inputArray)) {
      return raw._inputArray.slice();
    }
    const ft = target._fullData && target._fullData[idx];
    const fv = ft && ft[key];
    if (Array.isArray(fv)) return fv.slice();
    if (fv && typeof fv.length === "number") return Array.from(fv);
    return [];
  }
  function stashOriginals() {
    Object.entries(ANIM_DATA).forEach(([g, a]) => {
      try {
        if (a.type === "cumulative") {
          ORIG[g] = a.trace_indices.map(idx => {
            const xs = readTraceArr(idx, "x");
            const ys = readTraceArr(idx, "y");
            return { x: xs, y: ys, xms: xs.map(toMs) };
          });
        } else if (a.type === "heatmap_reveal") {
          ORIG[g] = { z: a.z_full };
        } else if (a.type === "marker") {
          ORIG[g] = {
            x: readTraceArr(a.trace_idx, "x"),
            y: readTraceArr(a.trace_idx, "y"),
            text: readTraceArr(a.trace_idx, "text"),
          };
        }
      } catch (e) {
        console.warn("stashOriginals failed for", g, e);
      }
    });
  }
  function applyAtT(g, t) {
    // Continuous interpolation in [0, 1]. Cumulative reveal computes a
    // cutoff in real time (not snapped to one of n_frames buckets), so
    // each rAF frame slides the line forward by sub-frame increments.
    // That's what makes the motion smooth — discrete setInterval ticks
    // were what gave it the "jumpy" feel.
    const a = ANIM_DATA[g];
    if (a.type === "cumulative") {
      const tMin = a.cutoffs_ms[0];
      const tMax = a.cutoffs_ms[a.cutoffs_ms.length - 1];
      const cutoff = tMin + (tMax - tMin) * t;
      const xs = [], ys = [], ends = [];
      for (let ti = 0; ti < a.trace_indices.length; ti++) {
        const orig = ORIG[g][ti];
        const xms = orig.xms;
        let end = 0;
        for (let j = 0; j < xms.length; j++) {
          if (xms[j] > cutoff) break;
          end = j + 1;
        }
        if (end === 0 && orig.x.length > 0) end = 1;
        ends.push(end);
        xs.push(orig.x.slice(0, end));
        ys.push(orig.y.slice(0, end));
      }
      // Sub-frame rAF ticks often land on the same slice; skip the restyle
      // when no trace gained a point (cheap motion, no wasted redraw).
      const key = "c" + ends.join(",");
      if (LAST_KEY[g] === key) return;
      LAST_KEY[g] = key;
      Plotly.restyle(target, {x: xs, y: ys}, a.trace_indices);
    } else if (a.type === "heatmap_reveal") {
      const i = Math.min(Math.floor(t * a.n_frames), a.n_frames - 1);
      if (LAST_KEY[g] === "h" + i) return;
      LAST_KEY[g] = "h" + i;
      const z = a.z_full.map((row, ridx) =>
        ridx <= i ? row.slice() : new Array(row.length).fill(null));
      Plotly.restyle(target, { z: [z] }, [a.trace_idx]);
    } else if (a.type === "marker") {
      const i = Math.min(Math.floor(t * a.n_frames), a.n_frames - 1);
      if (LAST_KEY[g] === "m" + i) return;
      LAST_KEY[g] = "m" + i;
      const d = a.marker_data[i];
      Plotly.restyle(target, {
        x: [[d.w]], y: [[d.rt]],
        text: [["  " + d.w.toFixed(0) + "W \u2192 " + d.rt.toFixed(1) + "min"]]
      }, [a.trace_idx]);
    }
  }
  function setLabelByT(g, t) {
    const a = ANIM_DATA[g];
    const idx = Math.min(Math.floor(t * a.labels.length), a.labels.length - 1);
    setTime(g, idx);
  }
  function axisNamesFor(traceIndices) {
    const seen = new Set();
    const out = [];
    traceIndices.forEach(i => {
      const tr = target.data[i] || {};
      const xa = (tr.xaxis || "x").replace("x", "xaxis");
      const ya = (tr.yaxis || "y").replace("y", "yaxis");
      [xa, ya].forEach(ax => {
        if (!seen.has(ax)) { seen.add(ax); out.push(ax); }
      });
    });
    return out;
  }
  function lockAxes(g) {
    // Snapshot the *currently displayed* axis bounds AND type, then
    // pin both. plotly.js #2546/#2823: restyling a date-typed trace
    // with x=[] makes Plotly switch the axis to a numeric default
    // (-1..6) and the saved date range is silently dropped. Pinning
    // .type='date' alongside .range keeps the axis date-typed even
    // when a transient empty frame goes through.
    const a = ANIM_DATA[g];
    const idxs = a.trace_indices || [a.trace_idx];
    const upd = {};
    axisNamesFor(idxs).forEach(ax => {
      const cur = target._fullLayout && target._fullLayout[ax];
      if (cur && cur.range) {
        upd[ax + ".range"] = cur.range.slice();
        upd[ax + ".autorange"] = false;
        if (cur.type) upd[ax + ".type"] = cur.type;
      }
    });
    if (Object.keys(upd).length) Plotly.relayout(target, upd);
  }
  function restoreOriginals(g) {
    // Note: axes are already locked (lockAxes ran on Play), so we
    // don't need to call autorange here — Plotly will keep the
    // pre-animation bounds, and the now-full data will fit inside them.
    const a = ANIM_DATA[g];
    if (!ORIG[g]) return;
    LAST_KEY[g] = null;   // next play must re-render from scratch
    if (a.type === "cumulative") {
      Plotly.restyle(target, {
        x: ORIG[g].map(o => o.x),
        y: ORIG[g].map(o => o.y),
      }, a.trace_indices);
    } else if (a.type === "heatmap_reveal") {
      Plotly.restyle(target, {z: [ORIG[g].z]}, [a.trace_idx]);
    } else if (a.type === "marker") {
      Plotly.restyle(target, {
        x: [ORIG[g].x], y: [ORIG[g].y], text: [ORIG[g].text]
      }, [a.trace_idx]);
    }
  }
  function resetAllGroups() {
    // Return every panel to its pristine state (full data, idle, label at
    // frame 0) WITHOUT a page reload. The E2E fixtures call this between tests
    // so each one starts clean and is order-independent under pytest-xdist —
    // far cheaper than reloading the ~1MB dashboard, which matters most on
    // low-core CI runners where reloads can't be parallelized away.
    Object.keys(ANIM_DATA).forEach(g => {
      clearStepper(g);          // cancels any in-flight rAF
      LAST_KEY[g] = null;
      restoreOriginals(g);      // full data back on every trace
      SAVED_T[g] = 0;
      setState(g, "idle");      // resets the play/pause button states
      if (ANIM_DATA[g].labels) setTime(g, 0);
    });
  }
  function setup() {
    target = gd();
    if (!target || !window.Plotly) { setTimeout(setup, 100); return; }
    if (!target.data || target.data.length === 0) { setTimeout(setup, 100); return; }
    // Wait until Plotly has actually populated traces, then snapshot.
    stashOriginals();
    const parent = target.parentElement;
    parent.style.position = "relative";
    document.querySelectorAll('.anim-overlay').forEach(ov => {
      if (ov.parentElement !== parent) parent.appendChild(ov);
    });
    document.querySelectorAll('.anim-speed').forEach(sel => {
      SPEED[sel.dataset.group] = parseFloat(sel.value) || 1;
      sel.addEventListener("change", () => {
        SPEED[sel.dataset.group] = parseFloat(sel.value) || 1;
      });
    });
    document.querySelectorAll('.anim-easing').forEach(sel => {
      // Easing is read live each rAF tick, so a change applies immediately to
      // an in-progress animation (and to the next play).
      EASING[sel.dataset.group] = sel.value || "linear";
      sel.addEventListener("change", () => {
        EASING[sel.dataset.group] = sel.value || "linear";
      });
    });
    const GEN = {};   // per-group generation token to fence stale rAF ticks
    function startPlayback(g, fromT) {
      const a = ANIM_DATA[g];
      clearStepper(g);
      lockAxes(g);
      const speed = SPEED[g] || 1;
      const totalMs = (a.n_frames * a.speed_ms) / speed;
      const t0 = Math.max(0, Math.min(1, fromT || 0));
      const tStart = performance.now() - t0 * totalMs;
      // Each startPlayback bumps the generation. In-flight tick closures
      // from a prior call check this and bail — a previously-queued tick
      // that survives cancelAnimationFrame can otherwise reschedule itself
      // and overwrite RAF[g], producing two parallel chains with stale
      // tStarts and ultimately negative elapsed times.
      const myGen = (GEN[g] || 0) + 1;
      GEN[g] = myGen;
      LAST_KEY[g] = null;     // force a fresh render of the first frame
      applyAtT(g, t0);
      setLabelByT(g, t0);
      setState(g, "playing");
      const tick = (now) => {
        if (GEN[g] !== myGen) return;       // superseded; let the new chain run
        const elapsed = Math.max(0, now - tStart);
        const tLinear = Math.min(elapsed / totalMs, 1);
        // Save linear t so resume picks up the actual elapsed point, not
        // the eased visual position. The easing fn is read live each tick so
        // changing the easing dropdown mid-play takes effect immediately.
        SAVED_T[g] = tLinear;
        const easeFn = EASINGS[EASING[g] || "linear"] || EASINGS["linear"];
        const tEased = easeFn(tLinear);
        applyAtT(g, tEased);
        setLabelByT(g, tEased);
        if (tLinear < 1) {
          RAF[g] = requestAnimationFrame(tick);
        } else {
          RAF[g] = null;
          restoreOriginals(g);
          setTime(g, ANIM_DATA[g].labels.length - 1);
          SAVED_T[g] = 1;
          setState(g, "ended");
        }
      };
      RAF[g] = requestAnimationFrame(tick);
    }
    function pausePlayback(g) {
      // Stop the timer at the current virtual t, leave panel where it is.
      // SAVED_T already updated by the most recent tick.
      clearStepper(g);
      setState(g, "paused");
    }
    document.querySelectorAll('.anim-play').forEach(btn => {
      btn.addEventListener("click", () => {
        const g = btn.dataset.group;
        const st = STATE[g] || "idle";
        if (st === "playing") return;          // play is no-op while playing
        // From idle / paused / ended → start fresh from t=0.
        // (User explicitly asked: at end, do NOT auto-restart unless they
        // press Play again — which they're doing right now, so honour it.)
        startPlayback(g, 0);
      });
    });
    document.querySelectorAll('.anim-pause').forEach(btn => {
      btn.addEventListener("click", () => {
        const g = btn.dataset.group;
        const st = STATE[g] || "idle";
        if (st === "playing") {
          pausePlayback(g);
        } else if (st === "paused") {
          // Resume from where we paused — explicitly NOT a restart.
          startPlayback(g, SAVED_T[g] || 0);
        }
        // idle / ended → button is disabled so this branch shouldn't
        // fire, but no-op defensively.
      });
    });
    // Initialise every group as idle so the disabled state is correct
    // before the user touches anything.
    Object.keys(ANIM_DATA).forEach(g => {
      STATE[g] = "idle";
      SAVED_T[g] = 0;
      refreshButtons(g);
    });
    // Position the overlays now and again on the next frame (in case Plotly's
    // _fullLayout/_size isn't fully settled on the first call), then keep them
    // aligned on viewport resize — the figure height is fixed but width (and
    // therefore each panel's pixel box) changes responsively.
    positionOverlays();
    requestAnimationFrame(positionOverlays);
    let _resizeRaf = null;
    window.addEventListener("resize", () => {
      if (_resizeRaf) cancelAnimationFrame(_resizeRaf);
      _resizeRaf = requestAnimationFrame(positionOverlays);
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup);
  } else {
    setup();
  }
  // Test/debug surface — lets the E2E suite peek at what stashOriginals
  // captured. Removing this hook would not change behaviour for users.
  window.__animDebug = {
    getOrig: (g) => ORIG[g],
    // Both naming conventions: legacy (stepper) for old tests, current
    // (raf) for the rAF engine. Either returns true while the group
    // animation is in progress.
    getStepperKeys: () => Object.keys(RAF).filter(k => RAF[k] != null)
      .concat(Object.keys(STEPPERS).filter(k => STEPPERS[k] != null)),
    isStepping: (g) => (RAF[g] != null) || (STEPPERS[g] != null),
    isPlaying: (g) => (RAF[g] != null) || (STEPPERS[g] != null),
    getSpeed: (g) => SPEED[g] || 1,
    getState: (g) => STATE[g] || "idle",
    getSavedT: (g) => SAVED_T[g] || 0,
    // Used by the E2E fixtures to reset between tests without a page reload.
    resetAll: () => resetAllGroups(),
  };
})();
</script>