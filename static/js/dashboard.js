(() => {
  "use strict";

  const RANGE_PRESETS = [
    { label: "1d", days: 1 },
    { label: "2d", days: 2 },
    { label: "7d", days: 7 },
    { label: "30d", days: 30 },
    { label: "90d", days: 90 },
    { label: "1y", days: 365 },
    { label: "All", days: null },
  ];

  // Per-category "1y" anchor. Hardcoded fallback matches db.py's
  // fiscal_year_config defaults, so the "1y" button behaves correctly even
  // in the brief window before refreshFiscalYears()'s fetch resolves.
  const state = {
    rangeDays: 90,
    customFrom: null,
    customTo: null,
    lastRefreshedIso: null,
    fiscalYears: {
      power: { month: 5, day: 1 },
      gas: { month: 5, day: 1 },
      water: { month: 1, day: 1 },
    },
    // Defaults to all-visible, matching db.py's own DEFAULT 1, so
    // nothing hides in the brief window before loadVisibilitySection()'s
    // fetch resolves.
    visibility: { gas: true, water: true, battery: true, sufficiency: true },
  };
  const charts = {};

  // Which fiscal-year config a tab's "1y" button resolves against. Costs
  // combines Power+Gas cost, and Overview/Battery/Self-sufficiency aren't
  // billed on their own cycle at all -- Power's boundary is the reasonable
  // shared default for all of those (matches Gas's own default anyway).
  // Only "gas" and "water" get their own distinct entries.
  const FISCAL_CATEGORY_BY_TAB = {
    power: "power",
    gas: "gas",
    water: "water",
    costs: "power",
    overview: "power",
    battery: "power",
    sufficiency: "power",
  };

  // Current fiscal year, start-to-today, for a boundary like {month:5, day:1}.
  // If today hasn't reached this year's anchor date yet, the fiscal year
  // in progress started last calendar year instead.
  function currentFiscalYearRange(month, day) {
    const today = new Date();
    const anchorThisYear = new Date(today.getFullYear(), month - 1, day);
    const start = anchorThisYear <= today ? anchorThisYear : new Date(today.getFullYear() - 1, month - 1, day);
    return { from: fmtDate(start), to: fmtDate(today) };
  }

  async function refreshFiscalYears() {
    const fy = await fetchJson("/api/settings/fiscal-years");
    if (fy.power_fy_start_month != null) {
      state.fiscalYears = {
        power: { month: fy.power_fy_start_month, day: fy.power_fy_start_day },
        gas: { month: fy.gas_fy_start_month, day: fy.gas_fy_start_day },
        water: { month: fy.water_fy_start_month, day: fy.water_fy_start_day },
      };
    }
    return fy;
  }

  function escHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function fmtDate(d) {
    // Local calendar date, NOT toISOString (which is UTC): between local
    // midnight and 02:00 CEST, UTC is still "yesterday", which shifted the
    // presets' `to` date and mislabeled a to=today custom range as historical.
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${y}-${m}-${day}`;
  }

  function rangeParams() {
    if (state.customFrom && state.customTo) {
      return { from: state.customFrom, to: state.customTo };
    }
    const to = new Date();
    if (state.rangeDays === null) {
      return { from: "2000-01-01", to: fmtDate(to) };
    }
    const from = new Date(to);
    from.setDate(from.getDate() - state.rangeDays);
    return { from: fmtDate(from), to: fmtDate(to) };
  }

  function rangeLabel() {
    if (state.customFrom && state.customTo) return `${state.customFrom} to ${state.customTo}`;
    const preset = RANGE_PRESETS.find((p) => p.days === state.rangeDays);
    return preset ? preset.label : "";
  }

  async function fetchJson(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url} -> ${res.status}`);
    return res.json();
  }

  // Every write request must carry this header (checked server-side
  // in app.py's before_request). window.OMNIMETER_WRITE_API_TOKEN is set
  // inline in index.html before this script loads.
  function writeHeaders(extra = {}) {
    return { "X-OmniMeter-Write-Api-Token": window.OMNIMETER_WRITE_API_TOKEN, ...extra };
  }

  // Hand-rolled rather than vendoring chartjs-plugin-annotation: this
  // codebase has zero existing Chart.js plugins, and a single-purpose
  // shaded-band need doesn't justify a new external dependency. Draws in
  // beforeDatasetsDraw (after axes/gridlines, before the actual line/bar
  // data), so bands sit behind the real data and gridlines stay faintly
  // visible through the low-alpha fill, rather than the band hiding either.
  function occupancyOverlayPlugin(labels, occupancyRows) {
    return {
      id: "occupancyOverlay",
      beforeDatasetsDraw(chart) {
        if (!labels.length || !occupancyRows || !occupancyRows.length) return;
        const { ctx, chartArea, scales } = chart;
        const xScale = scales.x;
        if (!chartArea || !xScale) return;
        // Category scale: half a tick-step of padding on each side so the
        // band covers the full width of the start/end bar or point, not
        // just the hairline at their exact centre.
        const step = labels.length > 1 ? xScale.getPixelForValue(1) - xScale.getPixelForValue(0) : xScale.width;
        const half = step / 2;
        ctx.save();
        ctx.fillStyle = cssVar("--series-occupancy");
        ctx.globalAlpha = 0.15;
        for (const r of occupancyRows) {
          // date_from/date_to now carry a time component
          // ("YYYY-MM-DD HH:MM"), but labels are plain "YYYY-MM-DD" day
          // columns -- shading stays whole-day (no sub-day rendering), so
          // compare against the date-only slice rather than the full
          // datetime string (a bare label would otherwise sort before that
          // same day's datetime value and silently lose its shading).
          const dFrom = r.date_from.slice(0, 10);
          const dTo = r.date_to.slice(0, 10);
          let startIdx = -1;
          let endIdx = -1;
          for (let i = 0; i < labels.length; i++) {
            if (labels[i] >= dFrom && labels[i] <= dTo) {
              if (startIdx === -1) startIdx = i;
              endIdx = i;
            }
          }
          if (startIdx === -1) continue;
          const xStart = Math.max(chartArea.left, xScale.getPixelForValue(startIdx) - half);
          const xEnd = Math.min(chartArea.right, xScale.getPixelForValue(endIdx) + half);
          ctx.fillRect(xStart, chartArea.top, xEnd - xStart, chartArea.bottom - chartArea.top);
        }
        ctx.restore();
      },
    };
  }

  // Per-day sunshine as a rail along the BOTTOM of the chart.
  //
  // Two constraints shaped this, both measured rather than assumed:
  //
  // 1. It is NOT a second dataset on a second y-axis. A dual-scale chart is
  //    the one thing the charting guidance rules out outright -- two measures
  //    of different units sharing a plot invite reading one against the
  //    other's axis. The rail's height is a fixed fraction of the chart area
  //    and carries no y-value, so it reads as annotation, exactly like the
  //    occupancy bands.
  //
  // 2. It cannot be a background fill, because occupancy already is one and
  //    the two collide: --series-weather-sun vs --series-occupancy measures
  //    normal-vision dE 10.8 (light) / 13.7 (dark), below the 15 floor -- hard
  //    to tell apart even with full colour vision. Separating them by POSITION
  //    (bands full-height, sun pinned to the bottom) removes the ambiguity
  //    without needing a colour that survives adjacency, which in this palette
  //    there isn't room for.
  //
  // Height encodes % of typical for that date, capped at 200% so one freak day
  // cannot flatten the rest. Half-height = typical.
  // ---- Drag-to-select: zoom by narrowing the real date range ----
  //
  // Deliberately NOT a client-side visual zoom. The data is daily, so zooming
  // reveals no extra detail -- it is the same points drawn wider, which makes
  // "zoom" and "narrow the range" the same operation visually. Committing to
  // the app's own range state instead of holding a private zoom means there is
  // one answer to "what period am I looking at?": the range controls, the
  // prev/next stepper and every chart on the tab all agree, and prev/next then
  // steps by the span you just selected. It also ships no new dependency,
  // which matters for the public fork.
  //
  // Reset is the existing preset buttons -- no bespoke "reset zoom" control to
  // learn.
  const DRAG_SELECT_MIN_PX = 8; // below this it is a click, and tooltips keep working

  function rangeSelectPlugin(labels) {
    // Only for charts whose x-axis really is a run of dates.
    const isDateAxis = labels.length > 1 && labels.every((l) => /^\d{4}-\d{2}-\d{2}$/.test(l));
    if (!isDateAxis) return null;

    const sel = { dragging: false, startX: null, curX: null };

    const indexAt = (chart, px) => {
      const v = chart.scales.x.getValueForPixel(px);
      return Math.max(0, Math.min(labels.length - 1, Math.round(v)));
    };

    return {
      id: "rangeSelect",

      afterInit(chart) {
        const canvas = chart.canvas;
        const xOf = (e) => e.clientX - canvas.getBoundingClientRect().left;

        const onDown = (e) => {
          if (e.button !== undefined && e.button !== 0) return;
          sel.dragging = true;
          sel.startX = xOf(e);
          sel.curX = sel.startX;
        };
        const onMove = (e) => {
          if (!sel.dragging) return;
          sel.curX = xOf(e);
          chart.draw();
        };
        const onUp = (e) => {
          if (!sel.dragging) return;
          sel.dragging = false;
          const endX = xOf(e);
          const dist = Math.abs(endX - sel.startX);
          sel.curX = null;
          chart.draw();
          if (dist < DRAG_SELECT_MIN_PX) return; // a click, not a selection
          const a = indexAt(chart, sel.startX);
          const b = indexAt(chart, endX);
          const [lo, hi] = a <= b ? [a, b] : [b, a];
          if (labels[lo] === labels[hi]) return; // single day -- nothing to narrow to
          state.customFrom = labels[lo];
          state.customTo = labels[hi];
          syncRangeControlsUI();
          loadPanel(document.querySelector("nav.tabs button.active").dataset.tab);
        };

        canvas.addEventListener("pointerdown", onDown);
        window.addEventListener("pointermove", onMove);
        window.addEventListener("pointerup", onUp);
        // Charts are destroyed and rebuilt on every range change, so these
        // must come off with them or they accumulate on every reload.
        chart.$rangeSelectCleanup = () => {
          canvas.removeEventListener("pointerdown", onDown);
          window.removeEventListener("pointermove", onMove);
          window.removeEventListener("pointerup", onUp);
        };
      },

      afterDraw(chart) {
        if (!sel.dragging || sel.curX == null) return;
        const { ctx, chartArea } = chart;
        const x1 = Math.max(chartArea.left, Math.min(sel.startX, sel.curX));
        const x2 = Math.min(chartArea.right, Math.max(sel.startX, sel.curX));
        if (x2 <= x1) return;
        ctx.save();
        ctx.fillStyle = cssVar("--series-power-import");
        ctx.globalAlpha = 0.14;
        ctx.fillRect(x1, chartArea.top, x2 - x1, chartArea.bottom - chartArea.top);
        ctx.strokeStyle = cssVar("--series-power-import");
        ctx.globalAlpha = 0.5;
        ctx.lineWidth = 1;
        ctx.strokeRect(x1, chartArea.top, x2 - x1, chartArea.bottom - chartArea.top);
        ctx.restore();
      },

      beforeDestroy(chart) {
        if (chart.$rangeSelectCleanup) chart.$rangeSelectCleanup();
      },
    };
  }

  // Attached centrally in both chart builders rather than per caller, so every
  // date-axis chart gets drag-to-select without each loader remembering to ask
  // for it. rangeSelectPlugin returns null for non-date axes.
  function withRangeSelect(labels, extraPlugins) {
    const rangeSelect = rangeSelectPlugin(labels);
    return rangeSelect ? [...(extraPlugins || []), rangeSelect] : extraPlugins || [];
  }

  // Fraction of the plot height the rail occupies. Also used to reserve
  // gutter space on diverging charts, where the bottom half is real data.
  const SUN_RAIL_FRACTION = 0.18;

  function sunRailPlugin(labels, weatherDays) {
    const pctByDate = new Map((weatherDays || []).map((d) => [d.date, d.pct_of_typical]));
    const mjByDate = new Map((weatherDays || []).map((d) => [d.date, d.radiation_mj]));
    const state = { visible: true };

    const plugin = {
      id: "sunRail",

      // Precision belongs in the hover layer. Without a number behind it the
      // rail is decoration -- a reader can see one day is shorter than another
      // but has no way to learn by how much, which was the first thing a
      // user asked about it.
      beforeInit(chart) {
        const tooltip = (chart.options.plugins.tooltip ||= {});
        const callbacks = (tooltip.callbacks ||= {});
        callbacks.afterBody = (items) => {
          if (!state.visible || !items.length) return "";
          const label = labels[items[0].dataIndex];
          const pct = pctByDate.get(label);
          if (pct == null) return "";
          const mj = mjByDate.get(label);
          return `Sun: ${Math.round(pct)}% of typical${mj != null ? ` (${mj.toFixed(1)} MJ/m²)` : ""}`;
        };
      },

      beforeDatasetsDraw(chart) {
        if (!state.visible || !labels.length || !pctByDate.size) return;
        const { ctx, chartArea, scales } = chart;
        const xScale = scales.x;
        if (!chartArea || !xScale) return;
        const step = labels.length > 1 ? xScale.getPixelForValue(1) - xScale.getPixelForValue(0) : xScale.width;
        const barW = Math.max(1, Math.min(step * 0.7, 14));
        const railMax = Math.min(24, (chartArea.bottom - chartArea.top) * SUN_RAIL_FRACTION);
        const typicalY = chartArea.bottom - railMax / 2;

        ctx.save();

        // The reference line is what turns the rail into a reading: taller
        // than this means the day beat what that date normally gets, shorter
        // means it fell short. Drawn before the bars so it never hides them.
        ctx.strokeStyle = cssVar("--text-muted");
        ctx.globalAlpha = 0.75;
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(chartArea.left, typicalY);
        ctx.lineTo(chartArea.right, typicalY);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = cssVar("--series-weather-sun");
        ctx.globalAlpha = 0.65;
        labels.forEach((label, i) => {
          const pct = pctByDate.get(label);
          if (pct == null) return;
          const h = (railMax * Math.max(0, Math.min(200, pct))) / 200;
          if (h <= 0) return;
          ctx.fillRect(xScale.getPixelForValue(i) - barW / 2, chartArea.bottom - h, barW, h);
        });
        ctx.restore();
      },
    };

    // Lets the chart builders find this plugin among extraPlugins and hand it
    // a toggle, so the rail behaves like every other thing on a chart rather
    // than being a special case that cannot be turned off.
    plugin.isSunRail = true;
    plugin.setVisible = (v) => {
      state.visible = v;
    };
    return plugin;
  }

  // Cached per range so switching tabs doesn't refetch; charts that want the
  // rail all draw the same days.
  async function fetchWeatherDays() {
    const { from, to } = rangeParams();
    try {
      const data = await fetchJson(`/api/weather/daily?from=${from}&to=${to}`);
      return data.available ? data : null;
    } catch {
      return null;
    }
  }

  // The rail is meaningless unless the reader is told what it is, and the
  // Open-Meteo credit is a CC BY 4.0 obligation wherever the data appears.
  function setSunRailHint(elId, weather) {
    const el = document.getElementById(elId);
    if (el) el.innerHTML = sunRailHint(weather);
  }

  // A key with actual marks in it, not just prose. Rendered in HTML rather
  // than drawn on the canvas so there is no text competing with the plot for
  // space, and so a screen reader gets it as words.
  function sunRailHint(weather) {
    if (!weather) return "";
    return (
      `<p class="chart-hint sun-key">` +
      `<span class="sun-key-swatch"></span> Daily sunshine ` +
      `<span class="sun-key-line"></span> = typical for that date ` +
      `&mdash; taller means sunnier than that date usually gets. Hover a day for the figure; ` +
      `switch it off with the Sunshine toggle below the chart. ` +
      `<a href="${escHtml(weather.attribution.url)}" target="_blank" rel="noopener">` +
      `${escHtml(weather.attribution.text)}</a></p>`
    );
  }

  // Same rail mechanic as sunRailPlugin above, driven by heating-degree-days
  // instead of radiation -- taller means colder (more heating demand) than
  // that date usually gets. Kept as its own function rather than sharing
  // sunRailPlugin's body: the two are only ever used on different tabs (this
  // one never appears alongside the sun rail), and duplicating this
  // self-contained canvas-drawing routine is a smaller risk than reworking
  // an already-live one with no automated visual-regression coverage.
  // Reuses --series-weather-sun rather than a new palette entry -- both
  // rails mean "this is weather context, not a data series", the same
  // relationship --series-occupancy already has across Gas/Water/Power, and
  // this repo's palette is dE-validated per pairing, not free to extend
  // without re-running that check.
  function gasHeatingRailPlugin(labels, weatherDays) {
    const pctByDate = new Map((weatherDays || []).map((d) => [d.date, d.pct_of_typical]));
    const hddByDate = new Map((weatherDays || []).map((d) => [d.date, d.hdd]));
    const state = { visible: true };

    const plugin = {
      id: "gasHeatingRail",

      beforeInit(chart) {
        const tooltip = (chart.options.plugins.tooltip ||= {});
        const callbacks = (tooltip.callbacks ||= {});
        const prevAfterBody = callbacks.afterBody;
        callbacks.afterBody = (items) => {
          const prev = prevAfterBody ? prevAfterBody(items) : "";
          if (!state.visible || !items.length) return prev;
          const label = labels[items[0].dataIndex];
          const pct = pctByDate.get(label);
          if (pct == null) return prev;
          const hdd = hddByDate.get(label);
          const line = `Heating demand: ${Math.round(pct)}% of typical${hdd != null ? ` (${hdd.toFixed(1)} HDD)` : ""}`;
          return prev ? `${prev}\n${line}` : line;
        };
      },

      beforeDatasetsDraw(chart) {
        if (!state.visible || !labels.length || !pctByDate.size) return;
        const { ctx, chartArea, scales } = chart;
        const xScale = scales.x;
        if (!chartArea || !xScale) return;
        const step = labels.length > 1 ? xScale.getPixelForValue(1) - xScale.getPixelForValue(0) : xScale.width;
        const barW = Math.max(1, Math.min(step * 0.7, 14));
        const railMax = Math.min(24, (chartArea.bottom - chartArea.top) * SUN_RAIL_FRACTION);
        const typicalY = chartArea.bottom - railMax / 2;

        ctx.save();

        ctx.strokeStyle = cssVar("--text-muted");
        ctx.globalAlpha = 0.75;
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 3]);
        ctx.beginPath();
        ctx.moveTo(chartArea.left, typicalY);
        ctx.lineTo(chartArea.right, typicalY);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = cssVar("--series-weather-sun");
        ctx.globalAlpha = 0.65;
        labels.forEach((label, i) => {
          const pct = pctByDate.get(label);
          if (pct == null) return;
          const h = (railMax * Math.max(0, Math.min(200, pct))) / 200;
          if (h <= 0) return;
          ctx.fillRect(xScale.getPixelForValue(i) - barW / 2, chartArea.bottom - h, barW, h);
        });
        ctx.restore();
      },
    };

    plugin.isGasHeatingRail = true;
    plugin.setVisible = (v) => {
      state.visible = v;
    };
    return plugin;
  }

  async function fetchGasWeatherDays() {
    const { from, to } = rangeParams();
    try {
      const data = await fetchJson(`/api/weather/gas?from=${from}&to=${to}`);
      return data.available ? data : null;
    } catch {
      return null;
    }
  }

  function setGasHeatingRailHint(elId, weather) {
    const el = document.getElementById(elId);
    if (el) el.innerHTML = gasHeatingRailHint(weather);
  }

  function gasHeatingRailHint(weather) {
    if (!weather) return "";
    return (
      `<p class="chart-hint sun-key">` +
      `<span class="sun-key-swatch"></span> Daily heating demand ` +
      `<span class="sun-key-line"></span> = typical for that date ` +
      `&mdash; taller means colder than that date usually gets (heating-degree-days below ` +
      `${weather.base_temp_c}&deg;C). Hover a day for the figure; switch it off with the ` +
      `Heating demand toggle below the chart. ` +
      `<a href="${escHtml(weather.attribution.url)}" target="_blank" rel="noopener">` +
      `${escHtml(weather.attribution.text)}</a></p>`
    );
  }

  // A chart with zero rows for the period, or rows that are all-null (a data
  // gap covering the whole range), renders as an indistinguishable blank
  // canvas otherwise -- looks identical to a broken chart. Centralized here
  // and in divergingBarChart, the only two chart entry points every tab's
  // load*() function calls, so every chart on every tab gets this for free
  // rather than needing a check duplicated at each call site.
  function hasAnyData(labels, datasets) {
    if (!labels || labels.length === 0) return false;
    return datasets.some((ds) => (ds.data || []).some((v) => v != null));
  }

  // Card-level "offline" treatment: dashed border (reusing .empty-state's
  // existing --baseline token, not a new style) plus a message naming the
  // metric, read straight from the card's own <h3> so it can't drift out of
  // sync with the heading already sitting right above it.
  function showNoDataState(canvasId) {
    const canvas = document.getElementById(canvasId);
    const card = canvas.closest(".chart-card");
    if (card) card.classList.add("chart-card--no-data");
    canvas.style.display = "none";
    charts[canvasId] = null;
    const toggles = document.getElementById(`${canvasId}-toggles`);
    if (toggles) toggles.remove();
    let msg = document.getElementById(`${canvasId}-no-data`);
    if (!msg) {
      msg = document.createElement("p");
      msg.id = `${canvasId}-no-data`;
      msg.className = "empty-state chart-empty-state";
      canvas.insertAdjacentElement("afterend", msg);
    }
    const heading = card ? card.querySelector("h3") : null;
    const metric = heading ? heading.textContent.trim() : "this chart";
    msg.textContent = `No data available for ${metric} in this period.`;
  }

  function clearNoDataState(canvasId) {
    const canvas = document.getElementById(canvasId);
    const card = canvas.closest(".chart-card");
    if (card) card.classList.remove("chart-card--no-data");
    canvas.style.display = "";
    const msg = document.getElementById(`${canvasId}-no-data`);
    if (msg) msg.remove();
  }

  function lineChart(canvasId, labels, datasets, extraPlugins = []) {
    const ctx = document.getElementById(canvasId);
    if (charts[canvasId]) charts[canvasId].destroy();
    if (!hasAnyData(labels, datasets)) {
      showNoDataState(canvasId);
      return;
    }
    clearNoDataState(canvasId);
    const plugins = withRangeSelect(labels, extraPlugins);
    charts[canvasId] = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      plugins,
      options: {
        responsive: true,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: { enabled: true },
        },
        scales: {
          x: { ticks: { color: cssVar("--text-muted") }, grid: { color: cssVar("--gridline") } },
          y: { ticks: { color: cssVar("--text-muted") }, grid: { color: cssVar("--gridline") } },
        },
        elements: { line: { borderWidth: 2, tension: 0.15 }, point: { radius: 2, hoverRadius: 4 } },
      },
    });
    renderChartToggles(
      canvasId,
      charts[canvasId],
      datasets,
      [...sunToggleFor(extraPlugins), ...gasHeatingToggleFor(extraPlugins)]
    );
  }

  // Explicit on/off switches per series, replacing Chart.js's built-in
  // legend (a plain label whose click-to-hide behaviour isn't obviously
  // interactive) -- one switch row per dataset, only rendered once a chart
  // actually has 2+ series to disambiguate. Rebuilt on every chart redraw
  // (tab switch, range change), so a hidden series resets to visible on the
  // next redraw -- same reset behaviour the old legend already had, since
  // each redraw destroys and recreates the Chart instance from scratch.
  // `extraToggles`: [{label, color, onChange}] for things drawn by a plugin
  // rather than as a dataset -- the sun rail, today. They get the same pill in
  // the same row, because "anything on the chart can be switched off" should
  // hold for annotations too; a reader may well want Import vs Export only, or
  // Import vs sunshine only.
  function renderChartToggles(canvasId, chart, datasets, extraToggles = []) {
    const existing = document.getElementById(`${canvasId}-toggles`);
    if (existing) existing.remove();
    if (datasets.length < 2 && extraToggles.length === 0) return;
    const canvas = document.getElementById(canvasId);
    const row = document.createElement("div");
    row.className = "chart-toggles";
    row.id = `${canvasId}-toggles`;
    datasets.forEach((ds, i) => {
      const id = `${canvasId}-toggle-${i}`;
      const color = ds.borderColor || ds.backgroundColor;
      const item = document.createElement("label");
      item.className = "chart-toggle";
      item.setAttribute("for", id);
      item.style.setProperty("--toggle-color", color);
      item.innerHTML =
        `<span class="chart-toggle-switch">` +
        `<input type="checkbox" id="${id}" checked>` +
        `<span class="chart-toggle-slider"></span>` +
        `</span>` +
        `<span class="chart-toggle-label">${escHtml(ds.label)}</span>`;
      item.querySelector("input").addEventListener("change", (e) => {
        chart.setDatasetVisibility(i, e.target.checked);
        chart.update();
        item.classList.toggle("is-off", !e.target.checked);
      });
      row.appendChild(item);
    });
    extraToggles.forEach((t, i) => {
      const id = `${canvasId}-extra-toggle-${i}`;
      const item = document.createElement("label");
      item.className = "chart-toggle";
      item.setAttribute("for", id);
      item.style.setProperty("--toggle-color", t.color);
      item.innerHTML =
        `<span class="chart-toggle-switch">` +
        `<input type="checkbox" id="${id}" checked>` +
        `<span class="chart-toggle-slider"></span>` +
        `</span>` +
        `<span class="chart-toggle-label">${escHtml(t.label)}</span>`;
      item.querySelector("input").addEventListener("change", (e) => {
        t.onChange(e.target.checked);
        chart.update();
        item.classList.toggle("is-off", !e.target.checked);
      });
      row.appendChild(item);
    });
    canvas.insertAdjacentElement("afterend", row);
  }

  // Any sun-rail plugin among a chart's extraPlugins becomes a pill.
  function sunToggleFor(extraPlugins) {
    const rail = (extraPlugins || []).find((p) => p && p.isSunRail);
    if (!rail) return [];
    return [{ label: "Sunshine", color: cssVar("--series-weather-sun"), onChange: (on) => rail.setVisible(on) }];
  }

  // Same idea as sunToggleFor, for the Gas tab's heating-degree-day rail.
  function gasHeatingToggleFor(extraPlugins) {
    const rail = (extraPlugins || []).find((p) => p && p.isGasHeatingRail);
    if (!rail) return [];
    return [
      { label: "Heating demand", color: cssVar("--series-weather-sun"), onChange: (on) => rail.setVisible(on) },
    ];
  }

  function seriesDataset(label, data, colorVar, extra = {}) {
    const color = cssVar(colorVar);
    return { label, data, borderColor: color, backgroundColor: color, pointBackgroundColor: color, ...extra };
  }

  // Locale datetime for display -- ingested_at/last_refreshed are stored/served
  // as ISO8601 with a UTC offset (an absolute instant), not meant to be read
  // raw. timeZone is pinned to the deployment's configured
  // OMNIMETER_TIMEZONE (window.OMNIMETER_TIMEZONE, see app.py) rather than
  // left to the browser's own timezone -- every value shown here describes an
  // event local to the meter's own household, so it should read the same
  // regardless of where the viewer happens to be. Before this was
  // configurable, a genuinely correct instant (23:45 CEST) rendered as
  // "03:15" with no timezone label when viewed from a browser set to IST,
  // and looked like a wrong time rather than a correct one in a different
  // zone. timeZoneName: "short" makes the conversion visible instead of
  // silent. Locale left as the browser's own default (undefined) rather than
  // pinned, since formatting preference doesn't need to be deployment-wide.
  function fmtDateTime(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: window.OMNIMETER_TIMEZONE || "Europe/Amsterdam",
      timeZoneName: "short",
    });
  }

  // occupancy_log's date_from/date_to are naive local strings ("YYYY-MM-DD
  // HH:MM", see db.py/app.py -- same convention as power_readings.time, local
  // to the deployment's configured OMNIMETER_TIMEZONE), NOT an absolute
  // instant -- so unlike fmtDateTime above, this does no timeZone conversion.
  // The parsed components are used to build a Date purely as a formatting
  // vehicle; since no timeZone is requested on output, the displayed
  // components always match the input exactly regardless of the viewer's own
  // browser timezone. Locale left as the browser's own default (undefined),
  // same reasoning as fmtDateTime above.
  function fmtOccupancyDateTime(s) {
    const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$/.exec(s);
    if (!m) return s;
    const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]), Number(m[4]), Number(m[5]));
    return d.toLocaleString(undefined, { year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  function fmtRelative(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    const diffMin = Math.max(0, Math.round((Date.now() - d.getTime()) / 60000));
    if (diffMin < 1) return "just now";
    if (diffMin < 60) return `${diffMin} min ago`;
    const hr = Math.floor(diffMin / 60);
    const min = diffMin % 60;
    if (hr < 24) return min ? `${hr} hr ${min} min ago` : `${hr} hr ago`;
    const days = Math.floor(hr / 24);
    return `${days} day${days === 1 ? "" : "s"} ago`;
  }

  // Re-renders on a timer (state.lastRefreshedIso, set by loadOverview) so
  // the relative part keeps counting up without needing a fresh fetch.
  function renderLastRefreshed() {
    const el = document.getElementById("last-refreshed");
    if (!el) return;
    if (!state.lastRefreshedIso) {
      el.textContent = "";
      return;
    }
    el.textContent = `Data refreshed ${fmtRelative(state.lastRefreshedIso)} (${fmtDateTime(state.lastRefreshedIso)})`;
  }

  // Fire-and-forget, not awaited from DOMContentLoaded -- a GitHub outage
  // or a slow/blocked request must never delay the dashboard's first
  // render. Silent no-op if the toggle is off (server-side gate; see
  // update_check.py) or the check itself fails for any reason.
  // Dismissing stores the SPECIFIC version dismissed, not just a boolean --
  // so dismissing today's "v1.2.0 available" banner suppresses it going
  // forward, but a later "v1.3.0 available" (once that ships) shows again
  // even though the user already dismissed something once. A plain "seen
  // an update notice, never show one again" flag would silently hide every
  // future release too.
  const UPDATE_DISMISS_KEY = "omnimeter-dismissed-update-version";

  async function checkForUpdate() {
    const banner = document.getElementById("update-banner");
    if (!banner) return;
    try {
      const res = await fetch("/api/update-check");
      if (!res.ok) return;
      const data = await res.json();
      if (!data.enabled || !data.update_available || localStorage.getItem(UPDATE_DISMISS_KEY) === data.latest_version) {
        banner.hidden = true;
        return;
      }
      banner.innerHTML =
        `A newer version of OmniMeter is available: <strong>${escHtml(data.latest_version)}</strong> ` +
        `(you're running ${escHtml(data.current_version)}). ` +
        `<a href="${escHtml(data.release_url)}" target="_blank" rel="noopener noreferrer">View release &rarr;</a>` +
        `<button type="button" class="update-banner-dismiss" aria-label="Dismiss">&times;</button>`;
      banner.hidden = false;
      const dismissBtn = banner.querySelector(".update-banner-dismiss");
      if (dismissBtn) {
        dismissBtn.addEventListener("click", () => {
          localStorage.setItem(UPDATE_DISMISS_KEY, data.latest_version);
          banner.hidden = true;
        });
      }
    } catch (err) {
      console.error("update check failed", err);
    }
  }

  // Two bars per x-position, sharing a stack so positive/negative values
  // diverge from a common zero line (e.g. import above, export below) —
  // mirrors HA's energy usage chart style.
  function divergingBarChart(
    canvasId,
    labels,
    posLabel,
    posData,
    posColorVar,
    negLabel,
    negData,
    negColorVar,
    extraPlugins = []
  ) {
    const ctx = document.getElementById(canvasId);
    if (charts[canvasId]) charts[canvasId].destroy();
    if (!hasAnyData(labels, [{ data: posData }, { data: negData }])) {
      showNoDataState(canvasId);
      return;
    }
    clearNoDataState(canvasId);
    const hasSunRail = (extraPlugins || []).some((p) => p && p.isSunRail);
    const plugins = withRangeSelect(labels, extraPlugins);
    const posColor = cssVar(posColorVar);
    const negColor = cssVar(negColorVar);
    const datasets = [
      { label: posLabel, data: posData, backgroundColor: posColor, borderRadius: 2, maxBarThickness: 16, stack: "s" },
      {
        label: negLabel,
        data: negData.map((v) => (v == null ? null : -v)),
        backgroundColor: negColor,
        borderRadius: 2,
        maxBarThickness: 16,
        stack: "s",
      },
    ];
    charts[canvasId] = new Chart(ctx, {
      type: "bar",
      data: { labels, datasets },
      plugins,
      options: {
        responsive: true,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${Math.abs(c.raw).toFixed(1)}` } },
        },
        scales: {
          x: { stacked: true, ticks: { color: cssVar("--text-muted") }, grid: { color: cssVar("--gridline") } },
          y: {
            stacked: true,
            ticks: { color: cssVar("--text-muted") },
            grid: { color: cssVar("--gridline") },
            // Reserve gutter space below the bars when the rail is present.
            // On a diverging chart the negative series (Export) is drawn
            // DOWNWARD into exactly the strip the rail occupies, so pinning
            // the rail to the bottom put it behind the data -- visibly
            // overlapping on Power. Extending the axis floor creates empty
            // space for the rail instead of drawing into occupied space; no
            // data moves or is hidden, the plot just gains a margin.
            suggestedMin: hasSunRail ? railGutterMin(negData) : undefined,
          },
        },
      },
    });
    renderChartToggles(canvasId, charts[canvasId], datasets, sunToggleFor(extraPlugins));
  }

  // Enough headroom below the lowest bar for the rail to sit clear of it.
  // Only called when a rail is actually present; returns undefined if the
  // negative series is empty, leaving Chart.js to autoscale as before.
  function railGutterMin(negData) {
    const lowest = Math.min(0, ...negData.map((v) => (v == null ? 0 : -v)));
    if (lowest === 0) return undefined;
    return lowest * (1 + SUN_RAIL_FRACTION * 1.6);
  }

  // Mean of a numeric field across daily rows, ignoring days with no value
  // for that field (a gap shouldn't drag the average toward zero) — or null
  // if no row has a value at all.
  function avg(rows, key) {
    const vals = rows.map((r) => r[key]).filter((v) => v != null);
    if (vals.length === 0) return null;
    return vals.reduce((s, v) => s + v, 0) / vals.length;
  }

  function fmtAvg(value, decimals, unit) {
    return value == null ? "—" : `${value.toFixed(decimals)} ${unit}`;
  }

  // parts: [{label, value, colorVar}] -- colorVar optional (omit for a
  // single-series chart, where the card title already names the series).
  function renderChartAvg(elId, parts) {
    const el = document.getElementById(elId);
    if (!el) return;
    el.innerHTML = parts
      .map(
        (p) =>
          `<span class="chart-avg-item">${
            p.colorVar ? `<span class="swatch" style="background:${cssVar(p.colorVar)}"></span>` : ""
          }${escHtml(p.label)}: <strong>${escHtml(p.value)}</strong></span>`
      )
      .join("");
  }

  function renderSourcesTable(elId, rows) {
    // rows: [{label, value, colorVar, warn}] -- warn is optional, used by
    // the data-health check to flag categories with real gaps.
    const el = document.getElementById(elId);
    const body = rows
      .map(
        (r) =>
          `<tr><td class="swatch-cell"><span class="swatch" style="background:${cssVar(r.colorVar)}"></span></td>` +
          `<td>${escHtml(r.label)}</td><td class="value-cell${r.warn ? " value-warn" : ""}">${escHtml(r.value)}</td></tr>`
      )
      .join("");
    el.innerHTML = `<table class="sources-table"><tbody>${body}</tbody></table>`;
  }

  // ---- sun meter ----
  //
  // A "single ratio against a limit", so a meter with a same-ramp track --
  // deliberately NOT a red/green diverging scale, because there is no good/bad
  // polarity in "sunnier than usual" and colouring it as if there were would
  // assert a judgement the data does not carry.
  //
  // The value shown is % of typical FOR THAT TIME OF YEAR, never an absolute.
  // This location swings from ~2.0 MJ/m2 in December to ~22.6 in June, so a
  // raw figure is unreadable without the seasonal comparison: 4.1h of sun is
  // dismal in June and above average in December, and only the ratio says
  // which. Scale runs 0-200% with the tick at 100% = typical.
  function sunMeter(pctOfTypical) {
    const clamped = Math.max(0, Math.min(200, pctOfTypical));
    return (
      `<span class="sun-meter" role="img" aria-label="${escHtml(Math.round(pctOfTypical))}% of typical sunshine">` +
      `<span class="sun-meter-fill" style="width:${(clamped / 200) * 100}%"></span>` +
      `<span class="sun-meter-tick"></span></span>`
    );
  }

  function renderWeatherCredit(elId, attribution) {
    // CC BY 4.0 obligation -- shown wherever weather-derived values are, and
    // rendered from the server-supplied object so it cannot drift or be
    // dropped by accident.
    const el = document.getElementById(elId);
    if (!el || !attribution) return;
    el.innerHTML =
      `<a href="${escHtml(attribution.url)}" target="_blank" rel="noopener">${escHtml(attribution.text)}</a>`;
  }

  // ---- findings detail list (acknowledge / delete / fix-hint) ----
  const CATEGORY_LABELS = { power: "Power", gas: "Gas", water: "Water", battery: "Battery" };

  // Ordered: drives the Type filter's option order too.
  const ISSUE_TYPE_LABELS = {
    gap: "Missing days",
    negative_delta: "Meter reset",
    glitch_episode: "Sensor glitch",
    granularity_disagreement: "Sources disagree",
    implausible_value: "Impossible value",
    empty_run: "Never recorded",
    outlier_day: "Unusual usage",
    reconciliation_mismatch: "Total mismatch",
    reconciliation_unverifiable: "Unverifiable",
  };

  // Gaps carry no issue_type from the server (they come from a different
  // endpoint), so "gap" is the default throughout.
  function issueTypeOf(f) {
    return f.issue_type || "gap";
  }

  // The one date a finding is filtered and sorted by. Each finding shape
  // stores it under a different key, so normalise here rather than making
  // every caller know the difference.
  function findingDate(f) {
    switch (issueTypeOf(f)) {
      case "gap":
      case "empty_run":
      // An outlier is now an episode spanning start..end, not a
      // single day. Filtered and sorted by its start, same as the others.
      case "outlier_day":
        return f.start;
      case "negative_delta":
        return f.time.slice(0, 10);
      case "glitch_episode":
        return f.start_time.slice(0, 10);
      default:
        return f.date;
    }
  }

  const RANGE_LABELLED_TYPES = new Set(["gap", "empty_run", "outlier_day"]);

  function findingDateLabel(f) {
    if (RANGE_LABELLED_TYPES.has(issueTypeOf(f)) && f.start !== f.end) return `${f.start} → ${f.end}`;
    return findingDate(f);
  }

  // Just the specifics -- category, date and type are their own columns now,
  // so repeating them in the text would only make the table wider.
  function findingDetail(f) {
    switch (issueTypeOf(f)) {
      case "negative_delta":
        return `${f.metric} ${f.delta.toFixed(3)} at ${f.time.slice(11)}`;
      case "glitch_episode":
        return `${f.metric} −${f.magnitude.toFixed(3)} (${f.start_time.slice(11)}–${f.end_time.slice(11)})`;
      case "granularity_disagreement": {
        const parts = Object.entries(f.by_granularity)
          .map(([g, v]) => `${g} ${v.toFixed(2)}`)
          .join(" vs ");
        return `${f.metric}: ${parts} (${f.diff_pct.toFixed(0)}%)`;
      }
      case "implausible_value":
        return `${f.metric} = ${f.value}`;
      case "empty_run":
        return `${f.metric}: ${f.days} days before the meter ever registered anything`;
      case "reconciliation_mismatch":
        return (
          `${f.metric}: stored ${f.stored.toFixed(3)} vs meter ${f.expected.toFixed(3)} ` +
          `(${f.diff > 0 ? "+" : ""}${f.diff.toFixed(3)}, ${f.diff_pct.toFixed(2)}%)`
        );
      case "reconciliation_unverifiable":
        return `${f.metric}: ${f.reason}`;
      case "outlier_day": {
        const arrow = f.direction === "high" ? "↑" : "↓";
        // The logged headcount is a hint toward "you were away", never an
        // assertion that the day is explained -- the log covers only part of
        // the history, and an ill day at home reads as normal occupancy.
        // Reported only when every day of the stretch agreed.
        const occOf = (x) => {
          if (x.occupancy === 0) return " — nobody home logged";
          if (typeof x.occupancy === "number") return ` — ${x.occupancy} logged home`;
          return "";
        };
        // An event's numbers live on its parts (one per meter that saw
        // it). One metric can contribute SEVERAL parts -- three separate power
        // episodes inside one week-long absence, say -- so collapse to one
        // entry per metric showing its most extreme, rather than printing
        // "import_kwh 0.2x" three times as the first cut did.
        const parts = f.parts || [f];
        const worst = new Map();
        for (const p of parts) {
          const seen = worst.get(p.metric);
          const moreExtreme = !seen || (f.direction === "high" ? p.ratio > seen.ratio : p.ratio < seen.ratio);
          if (moreExtreme) worst.set(p.metric, p);
        }
        const detail = [...worst.values()]
          .map((p) => `${p.metric} ${arrow}${p.ratio.toFixed(1)}× (${p.value.toFixed(2)} vs ${p.baseline_median.toFixed(2)})`)
          .join(", ");
        const span = f.days > 1 ? `${f.days} days: ` : "";
        // Zero gas AND zero water is a direct absence signal, so say
        // so plainly instead of leaving the reader to infer it from ratios.
        // It replaces the occupancy hint rather than sitting beside it -- the
        // logged headcount is precisely what is wrong in these cases (a short
        // trip inside a longer "2 people home" span), and printing both would
        // be reporting a contradiction as if it were two facts.
        if (f.explained_by === "nobody home") return `${span}${detail} — nobody home (no gas, no water)`;
        if (f.explained_by === "EV charging") return `${span}${detail} — EV charging`;
        return `${span}${detail}${occOf(parts[0])}`;
      }
      default: {
        const days = Math.round((new Date(`${f.end}T00:00:00`) - new Date(`${f.start}T00:00:00`)) / 86400000) + 1;
        return days === 1 ? "1 day missing" : `${days} days missing`;
      }
    }
  }

  // Filter state per detail panel, kept outside the render so it survives the
  // full re-render that acknowledge/delete trigger -- otherwise acknowledging
  // one row would silently reset the filters the user just set.
  const findingsFilters = {};

  function selectHtml(name, label, options, selected) {
    const opts = [`<option value="">${escHtml(label)}</option>`]
      .concat(
        options.map(
          ([value, text]) =>
            `<option value="${escHtml(value)}"${value === selected ? " selected" : ""}>${escHtml(text)}</option>`
        )
      )
      .join("");
    return `<select data-filter="${escHtml(name)}">${opts}</select>`;
  }

  // findings: flat list, each already tagged with category/issue_type (see
  // flattenGaps/flattenQuality/flattenConsumptionNotes) plus
  // fingerprint/acknowledged from the server. extraAction(f) optionally
  // returns {label, className, onClick} for a second button (Fix / Delete
  // reading) alongside the always-present Acknowledge/Unacknowledge toggle.
  //
  // Rendered as a filterable table rather than a flat list: with 658
  // consumption notes spanning four categories and five years, one long list
  // is unreadable and un-actionable. Filters narrow by category, type,
  // status and date range; the count line always states how much is hidden,
  // so a filtered view can never be mistaken for the whole picture.
  // `bulkAck` opts a table into "acknowledge everything currently shown".
  // Enabled for consumption notes only. Deliberately NOT enabled
  // for the Data health tab: those findings are genuine instrument faults
  // and there are 25 of them, not 794 -- a one-click sweep there would make
  // real faults easy to clear without reading them, which is the opposite of
  // the problem this solves.
  function renderFindingsDetail(elId, findings, extraAction, bulkAck) {
    const el = document.getElementById(elId);
    if (findings.length === 0) {
      el.innerHTML = `<p class="empty-state">Nothing to show.</p>`;
      return;
    }
    const state = (findingsFilters[elId] ||= { category: "", type: "", status: "", from: "", to: "", sort: "desc" });

    const presentCategories = Object.keys(CATEGORY_LABELS).filter((c) =>
      findings.some((f) => findingCategories(f).includes(c))
    );
    const presentTypes = Object.keys(ISSUE_TYPE_LABELS).filter((t) => findings.some((f) => issueTypeOf(f) === t));

    const matches = (f) => {
      const d = findingDate(f);
      // An event matches a category filter if ANY of its categories does --
      // filtering to Gas must not hide the gas half of a gas+water event.
      if (state.category && !findingCategories(f).includes(state.category)) return false;
      if (state.type && issueTypeOf(f) !== state.type) return false;
      if (state.status === "open" && f.acknowledged) return false;
      if (state.status === "acked" && !f.acknowledged) return false;
      // "Needs a look" hides both the already-reviewed and the
      // already-explained -- the two ways a row can stop deserving attention.
      if (state.status === "unexplained" && (f.acknowledged || f.explained_by)) return false;
      if (state.from && d < state.from) return false;
      if (state.to && d > state.to) return false;
      return true;
    };

    const visible = findings
      .filter(matches)
      .sort((a, b) => (state.sort === "asc" ? 1 : -1) * findingDate(a).localeCompare(findingDate(b)));

    const filterBar =
      `<div class="findings-filters">` +
      selectHtml("category", "All categories", presentCategories.map((c) => [c, CATEGORY_LABELS[c]]), state.category) +
      (presentTypes.length > 1
        ? selectHtml("type", "All types", presentTypes.map((t) => [t, ISSUE_TYPE_LABELS[t]]), state.type)
        : "") +
      selectHtml(
        "status",
        "All statuses",
        findings.some((f) => f.explained_by)
          ? [["unexplained", "Needs a look"], ["open", "Not acknowledged"], ["acked", "Acknowledged"]]
          : [["open", "Not acknowledged"], ["acked", "Acknowledged"]],
        state.status
      ) +
      `<label>From <input type="date" data-filter="from" value="${escHtml(state.from)}"></label>` +
      `<label>To <input type="date" data-filter="to" value="${escHtml(state.to)}"></label>` +
      `<select data-filter="sort">` +
      `<option value="desc"${state.sort === "desc" ? " selected" : ""}>Newest first</option>` +
      `<option value="asc"${state.sort === "asc" ? " selected" : ""}>Oldest first</option>` +
      `</select>` +
      `<button type="button" class="link-button" data-filter-reset>Reset</button>` +
      `</div>`;

    const ackedShown = visible.filter((f) => f.acknowledged).length;
    const openShown = visible.length - ackedShown;
    const explainedShown = visible.filter((f) => f.explained_by && !f.acknowledged).length;
    const countLine =
      `<div class="findings-count">Showing ${visible.length} of ${findings.length}` +
      (ackedShown ? ` — ${ackedShown} acknowledged` : "") +
      (explainedShown ? ` — ${explainedShown} already explained` : "") +
      `</div>`;

    // Counts go in the labels, so the button always states exactly what it
    // is about to touch -- and both directions are offered whenever there is
    // something to act on, so a bulk acknowledge is always one click from
    // being undone.
    const bulkBar =
      bulkAck && (openShown || ackedShown)
        ? `<div class="findings-bulk">` +
          (openShown ? `<button type="button" data-bulk="ack">Acknowledge all ${openShown} shown</button>` : "") +
          (ackedShown
            ? `<button type="button" data-bulk="unack">Unacknowledge all ${ackedShown} shown</button>`
            : "") +
          `</div>`
        : "";

    if (visible.length === 0) {
      el.innerHTML = `${filterBar}${countLine}<p class="empty-state">No findings match these filters.</p>`;
      attachFilterHandlers(el, elId, findings, extraAction, bulkAck);
      return;
    }

    const rows = visible
      .map((f, i) => {
        const extra = extraAction ? extraAction(f) : null;
        const extraBtn = extra
          ? `<button type="button" class="${escHtml(extra.className || "")}" data-action="extra" data-i="${i}">${escHtml(extra.label)}</button>`
          : "";
        const ackBtn = f.acknowledged
          ? `<button type="button" data-action="unack" data-i="${i}">Unacknowledge</button>`
          : `<button type="button" data-action="ack" data-i="${i}">Acknowledge</button>`;
        return (
          `<tr class="${f.acknowledged ? "acknowledged" : ""}">` +
          `<td>${escHtml(findingDateLabel(f))}</td>` +
          `<td>${escHtml(findingCategories(f).map((c) => CATEGORY_LABELS[c]).join(" + "))}</td>` +
          `<td>${escHtml(ISSUE_TYPE_LABELS[issueTypeOf(f)])}</td>` +
          `<td class="finding-detail-cell">${escHtml(findingDetail(f))}</td>` +
          `<td>${f.acknowledged ? "Acknowledged" : "—"}</td>` +
          `<td class="finding-actions">${extraBtn}${ackBtn}</td></tr>`
        );
      })
      .join("");

    el.innerHTML =
      filterBar +
      countLine +
      bulkBar +
      `<div class="table-scroll"><table class="data-table findings-table">` +
      `<thead><tr><th>Date</th><th>Category</th><th>Type</th><th>Detail</th><th>Status</th><th></th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>`;

    el.querySelectorAll("button[data-action]").forEach((btn) => {
      const f = visible[Number(btn.dataset.i)];
      if (btn.dataset.action === "ack") btn.addEventListener("click", () => acknowledgeFinding(f, true));
      else if (btn.dataset.action === "unack") btn.addEventListener("click", () => acknowledgeFinding(f, false));
      else if (btn.dataset.action === "extra") btn.addEventListener("click", () => extraAction(f).onClick());
    });
    el.querySelectorAll("button[data-bulk]").forEach((btn) => {
      const acknowledge = btn.dataset.bulk === "ack";
      // The button counts EVENTS (what the user sees), but the request has to
      // carry the underlying notes, since those are what acknowledged_issues
      // is keyed on. An event with one part already acknowledged still sends
      // all of its parts -- INSERT OR IGNORE makes that a no-op.
      const targets = visible
        .filter((f) => !!f.acknowledged !== acknowledge)
        .flatMap((f) => f.parts || [f]);
      btn.addEventListener("click", () => acknowledgeFindingsBulk(targets, acknowledge));
    });
    attachFilterHandlers(el, elId, findings, extraAction, bulkAck);
  }

  function attachFilterHandlers(el, elId, findings, extraAction, bulkAck) {
    el.querySelectorAll("[data-filter]").forEach((input) => {
      input.addEventListener("change", () => {
        findingsFilters[elId][input.dataset.filter] = input.value;
        renderFindingsDetail(elId, findings, extraAction, bulkAck);
      });
    });
    const reset = el.querySelector("[data-filter-reset]");
    if (reset) {
      reset.addEventListener("click", () => {
        findingsFilters[elId] = { category: "", type: "", status: "", from: "", to: "", sort: "desc" };
        renderFindingsDetail(elId, findings, extraAction, bulkAck);
      });
    }
  }

  function flattenGaps(health) {
    const out = [];
    for (const category of Object.keys(CATEGORY_LABELS)) {
      for (const g of health[category].gaps) out.push({ ...g, category });
    }
    return out;
  }

  function flattenQuality(quality) {
    const issueLists = {
      negative_delta: (e) => e.negative_deltas.items,
      glitch_episode: (e) => e.glitch_episodes.items,
      granularity_disagreement: (e) => e.granularity_disagreements,
      implausible_value: (e) => e.implausible_values,
      empty_run: (e) => e.empty_runs,
    };
    const out = [];
    for (const category of Object.keys(CATEGORY_LABELS)) {
      for (const [issue_type, getList] of Object.entries(issueLists)) {
        for (const f of getList(quality[category])) out.push({ ...f, category, issue_type });
      }
    }
    return out;
  }

  // The detail list is one row per EVENT, not per meter that noticed
  // it. A week away is one thing that happened; it previously filed a gas
  // note, a water note and a power note. The server does the grouping (it is
  // unit-tested there, and there is no JS test harness in this project) --
  // this just tags them for the shared findings table.
  // The parts must be tagged too, not just the event. Acknowledging an event
  // sends its PARTS to the bulk endpoint, and an untagged part falls back to
  // issue_type "gap" -- which is a valid value, so the write succeeds, stores
  // a triple nothing ever reads back, and the row silently stays
  // unacknowledged. Caught in the browser; no test or syntax check would have
  // shown it, because nothing errors.
  function flattenConsumptionNotes(notes) {
    return notes.events.map((e) => ({
      ...e,
      issue_type: "outlier_day",
      parts: e.parts.map((p) => ({ ...p, issue_type: "outlier_day" })),
    }));
  }

  // Most findings belong to exactly one category; a consumption event can
  // span several. Normalising here keeps the shared table's filter and label
  // code from having to know which is which.
  function findingCategories(f) {
    return f.categories || [f.category];
  }

  // Mismatches and unverifiable dates share one detail table, separated by
  // the Type filter -- they're different outcomes of the same check, and
  // listing them together is what stops a clean run reading as a bare green
  // tick that quietly hid its own exemptions.
  function flattenReconciliation(rec) {
    const out = [];
    for (const category of Object.keys(CATEGORY_LABELS)) {
      for (const f of rec[category].mismatches) out.push({ ...f, category, issue_type: "reconciliation_mismatch" });
      for (const f of rec[category].unverifiable)
        out.push({ ...f, category, issue_type: "reconciliation_unverifiable" });
    }
    return out;
  }

  // Acknowledging never edits/deletes a reading, only records that
  // this specific flagged finding was reviewed. Re-runs the whole
  // check-data-health flow afterward -- simplest way to keep the summary
  // table, the open detail list, and the acknowledged counts all correctly
  // in sync, rather than trying to patch three views in place.
  // Both reports acknowledge through the same endpoint (the stored triple is
  // category+issue_type+fingerprint either way), but they live on different
  // tabs now, so the refresh has to re-run whichever check the finding came
  // from -- re-clicking the integrity button from the Comparison tab would
  // silently leave the consumption list stale.
  function refreshButtonFor(finding) {
    if (finding.issue_type === "outlier_day") return "check-consumption-notes";
    if (finding.issue_type && finding.issue_type.startsWith("reconciliation_")) return "check-reconciliation";
    return "check-data-health";
  }

  // A consumption event is a grouping of findings, not a finding of
  // its own -- it has no fingerprint in acknowledged_issues. Acknowledging one
  // acknowledges the notes underneath it, which is exactly what the bulk
  // endpoint already does. Keeping the stored unit unchanged is what lets
  // every acknowledgement made before grouping existed keep working.
  async function acknowledgeFinding(finding, acknowledge) {
    if (finding.parts) return acknowledgeFindingsBulk(finding.parts, acknowledge, { silent: true });
    await fetch("/api/data-quality/acknowledge", {
      method: acknowledge ? "POST" : "DELETE",
      headers: writeHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        category: finding.category,
        issue_type: finding.issue_type || "gap",
        fingerprint: finding.fingerprint,
      }),
    });
    document.getElementById(refreshButtonFor(finding)).click();
  }

  // Acknowledge/unacknowledge every finding currently shown, in one
  // request. Confirms with the exact count first -- this is reversible, but
  // silently retagging dozens of rows on a single click is not something to
  // do without saying so. Nothing is deleted or edited either way; the
  // findings stay in the list, tagged (tag-don't-filter, as everywhere else).
  async function acknowledgeFindingsBulk(findings, acknowledge, { silent = false } = {}) {
    if (findings.length === 0) return;
    const verb = acknowledge ? "Acknowledge" : "Unacknowledge";
    // `silent` is used when this is the mechanism behind a single-row action
    // (acknowledging one event, which happens to write several notes) -- the
    // user asked about one thing, so don't confront them with a count.
    const ok =
      silent ||
      confirm(
        `${verb} all ${findings.length} shown ${findings.length === 1 ? "finding" : "findings"}?\n\n` +
          `This applies to exactly what the current filters and date range show, and nothing else. ` +
          `No readings are changed, and this can be undone.`
      );
    if (!ok) return;
    try {
      const res = await fetch("/api/data-quality/acknowledge-bulk", {
        method: acknowledge ? "POST" : "DELETE",
        headers: writeHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          items: findings.map((f) => ({
            category: f.category,
            issue_type: f.issue_type || "gap",
            fingerprint: f.fingerprint,
          })),
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        alert(body.error || `${verb} failed.`);
      }
    } catch (err) {
      alert(`${verb} failed: ${err}`);
    }
    document.getElementById(refreshButtonFor(findings[0])).click();
  }

  // v1 deliberately only offers this for negative_delta findings --
  // outlier days, glitch episodes, disagreements, and implausible values are
  // derived from a whole day or multiple raw rows, not one deletable cell.
  // Targets the *later* reading in the pair (to_value) -- the more common
  // real-world case of "the new reading is the bad one," a documented v1
  // simplification, not always correct. `value` is echoed back exactly as
  // received (not reformatted) so the server's optimistic-lock check
  // succeeds on an unchanged reading.
  async function deleteReading(finding) {
    const ok = confirm(
      `Delete this ${finding.metric} reading?\n\n` +
        `Category: ${CATEGORY_LABELS[finding.category]}\nTime: ${finding.time}\nValue: ${finding.to_value}\n\n` +
        `This only clears the one bad value -- it cannot be undone by this tool, only by re-importing source data.`
    );
    if (!ok) return;
    try {
      const res = await fetch(`/api/readings/${finding.category}`, {
        method: "DELETE",
        headers: writeHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          time: finding.time,
          granularity: finding.granularity,
          metric: finding.metric,
          value: finding.to_value,
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        const byStatus = { 404: "Already gone -- refreshing.", 409: "Changed since this was flagged -- refresh and try again.", 503: "Busy, try again shortly." };
        alert(byStatus[res.status] || body.error || "Delete failed.");
      }
    } catch (err) {
      alert(`Delete failed: ${err}`);
    }
    document.getElementById("check-data-health").click();
  }

  // Activates whichever sub-tab contains el, if el sits inside a .subpanel.
  // No-op if el isn't inside one (not every panel has sub-tabs). Needed
  // because a sub-tab's own panel is display:none until it's the active
  // sub-tab, same reason the top-level nav click has to happen before any
  // scrollIntoView -- see fixGapHint below, which is exactly the case that
  // needs this now that Import has sub-tabs.
  function activateSubTabContaining(el) {
    const subpanel = el.closest(".subpanel");
    if (!subpanel) return;
    const nav = subpanel.parentElement.querySelector("nav.subtabs");
    if (!nav) return;
    const subtab = subpanel.id.replace(/^subpanel-/, "");
    const btn = nav.querySelector(`button[data-subtab="${subtab}"]`);
    if (btn) btn.click();
  }

  // Shows a plain text hint with the gap's date range, then scrolls to and
  // briefly highlights the CSV upload card.
  //
  // This is now a cross-tab jump: gaps are reported on the Data health tab
  // while the CSV upload they point at lives on Import. The nav click has to
  // come first -- the Import panel is display:none until it's the active
  // tab, and scrollIntoView on a hidden element does nothing. Clicking the
  // nav button (rather than toggling the classes directly) also keeps
  // loadPanel's per-tab refresh behaviour intact. Since Import gained
  // sub-tabs, the CSV card is also hidden unless its own sub-tab ("Meter
  // data") is active -- activateSubTabContaining handles that one level down.
  function fixGapHint(finding) {
    document.querySelector('nav.tabs button[data-tab="import"]').click();
    const card = document.getElementById("import-csv-card");
    activateSubTabContaining(card);
    const hint = document.getElementById("import-fix-hint");
    const range = finding.start === finding.end ? finding.start : `${finding.start} to ${finding.end}`;
    hint.textContent = `Missing ${CATEGORY_LABELS[finding.category]} data: ${range}. Upload a file covering this range here, if you have one.`;
    hint.hidden = false;
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.remove("flash-highlight");
    void card.offsetWidth; // force reflow so the animation restarts on repeated clicks
    card.classList.add("flash-highlight");
  }

  // rows: daily-row objects (each has `date` plus numeric fields). Finds the
  // date+value of the highest and lowest non-null reading for `key`. Returns
  // null if no row has a value for this key at all (rather than 0/0, which
  // would misleadingly imply a real reading of zero).
  function dailyHighLow(rows, key) {
    let high = null;
    let low = null;
    for (const r of rows) {
      const v = r[key];
      if (v == null) continue;
      if (!high || v > high.value) high = { date: r.date, value: v };
      if (!low || v < low.value) low = { date: r.date, value: v };
    }
    return high && low ? { high, low } : null;
  }

  // items: [{label, colorVar, rows, key, decimals, unit}] -- one row per
  // metric, matching whatever that tab's Sources table already lists.
  function renderHighLowTable(elId, items) {
    const el = document.getElementById(elId);
    if (!el) return;
    const fmtPoint = (point, decimals, unit) =>
      point ? `${point.value.toFixed(decimals)} ${unit} (${point.date})` : "—";
    const body = items
      .map((it) => {
        const hl = dailyHighLow(it.rows, it.key);
        return (
          `<tr><td class="swatch-cell"><span class="swatch" style="background:${cssVar(it.colorVar)}"></span></td>` +
          `<td>${escHtml(it.label)}</td>` +
          `<td class="value-cell">${escHtml(fmtPoint(hl && hl.high, it.decimals, it.unit))}</td>` +
          `<td class="value-cell">${escHtml(fmtPoint(hl && hl.low, it.decimals, it.unit))}</td></tr>`
        );
      })
      .join("");
    el.innerHTML =
      `<table class="sources-table hl-table"><thead><tr><th></th><th>Metric</th>` +
      `<th>Highest</th><th>Lowest</th></tr></thead><tbody>${body}</tbody></table>`;
  }

  // Simplified static energy-flow diagram (Grid/House/Battery/Gas) — not a
  // true animated Sankey, just a compact at-a-glance summary of period totals.
  // Each node keeps its label centered *inside* the circle and renders its
  // value line(s) *outside*, below the circle, so neither ever overlaps or
  // spills past the circle's edge regardless of node radius or text length.
  function flowNode(cx, cy, r, label, values, valuePosition = "below") {
    // "below" (default) stacks value lines downward starting just outside
    // the circle's bottom edge; "above" mirrors that upward, for nodes with
    // a neighbor close enough below them that a below-value would collide
    // with it (e.g. Gas sits directly above the house node in this diagram).
    const sign = valuePosition === "above" ? -1 : 1;
    const valueLines = values
      .map((v, i) => `<text class="flow-value${v.cls ? " " + v.cls : ""}" y="${sign * (r + 26 + i * 26)}">${v.text}</text>`)
      .join("");
    return `
      <g class="flow-node" transform="translate(${cx},${cy})">
        <circle r="${r}"></circle>
        <text class="flow-icon-label" y="7">${label}</text>
        ${valueLines}
      </g>`;
  }

  function renderEnergyFlow(elId, overviewData) {
    const el = document.getElementById(elId);
    const gridImport = overviewData.power ? overviewData.power.import_kwh : 0;
    const gridExport = overviewData.power ? overviewData.power.export_kwh : 0;
    const battCharge = overviewData.battery ? overviewData.battery.charge_kwh : 0;
    const battDischarge = overviewData.battery ? overviewData.battery.discharge_kwh : 0;
    const gas = overviewData.gas ? overviewData.gas.usage_m3 : 0;
    const houseTotal = gridImport + battDischarge;
    const homeLabel = window.OMNIMETER_HOME_LABEL || "Home";

    const importColor = cssVar("--series-power-import");
    const battColor = cssVar("--series-battery-charge");
    const gasColor = cssVar("--series-gas");

    // Taller viewBox + bigger circles/font than this diagram had before
    // an Overview redesign -- once the two-card row was
    // stretched to equal height (.overview-flow-grid), the ask was to make THIS
    // diagram actually use that height with bigger, clearer text rather than
    // floating centered in mostly blank space. Narrower viewBox width
    // (280 vs the old 320) raises the real on-screen scale for the same
    // ~226px rendered width, on top of the larger absolute circle/font sizes.
    el.innerHTML = `
      <svg class="energy-flow" viewBox="0 -10 300 470" xmlns="http://www.w3.org/2000/svg">
        <path class="flow-line" d="M 107 190 L 181 190" stroke="${importColor}"></path>
        <path class="flow-line" d="M 131 190 L 131 324" stroke="${battColor}"></path>
        <path class="flow-line" d="M 225 97 L 225 146" stroke="${gasColor}"></path>
        ${flowNode(65, 190, 42, "Grid", [
          { text: `↓ ${gridImport.toFixed(2)} kWh`, cls: "value-down" },
          { text: `↑ ${gridExport.toFixed(2)} kWh`, cls: "value-up" },
        ])}
        ${flowNode(225, 190, 44, homeLabel, [{ text: `${houseTotal.toFixed(2)} kWh` }])}
        ${flowNode(131, 362, 38, "Battery", [
          { text: `↓ ${battCharge.toFixed(2)} kWh`, cls: "value-down" },
          { text: `↑ ${battDischarge.toFixed(2)} kWh`, cls: "value-up" },
        ])}
        ${flowNode(225, 65, 32, "Gas", [{ text: `${gas.toFixed(2)} m3` }], "above")}
      </svg>`;
  }

  // A second, genuinely proportional energy-flow visual for
  // Overview, alongside renderEnergyFlow above (which stays -- fixed-width
  // lines, not proportional to value). Colours are set via CSS custom
  // properties in a `style` attribute rather than baked in with cssVar() at
  // render time (which is what renderEnergyFlow above does) -- that means a
  // theme toggle recolours this one live with no re-render needed, unlike
  // the older diagram.
  //
  // Node order is fixed for colour-safety, not narrative reasons: Solar
  // (--series-estimate) and Battery discharge (--series-battery-discharge)
  // fail the dataviz palette checker outright when adjacent (normal-vision
  // dE 7.1, below the 15 hard floor) -- Grid in sits between them instead.
  // See style.css's --series-load comment for the full validation record.
  const SANKEY_SOURCE_ORDER = ["solar", "grid_in", "battery_discharge"];
  const SANKEY_USE_ORDER = ["load", "battery_charge", "grid_out"];
  const SANKEY_COLOR_VAR = {
    solar: "--series-estimate",
    grid_in: "--series-power-import",
    battery_discharge: "--series-battery-discharge",
    load: "--series-load",
    battery_charge: "--series-battery-charge",
    grid_out: "--series-power-export",
  };
  const SANKEY_STATIC_LABEL = {
    solar: "Solar",
    grid_in: "Grid in",
    battery_discharge: "Battery discharge",
    battery_charge: "Battery charge",
    grid_out: "Grid out",
  };

  function sankeyLabel(key) {
    // Load reuses the same OMNIMETER_HOME_LABEL setting renderEnergyFlow
    // already uses for its house node, rather than a hardcoded "Load" --
    // one place to change a self-hoster's household label, not two.
    return key === "load" ? window.OMNIMETER_HOME_LABEL || "Home" : SANKEY_STATIC_LABEL[key];
  }

  function sankeyRibbonPath(x0, y0, x1, y1, width) {
    const dx = (x1 - x0) / 2;
    const y0b = y0 + width;
    const y1b = y1 + width;
    return (
      `M ${x0} ${y0} C ${x0 + dx} ${y0} ${x1 - dx} ${y1} ${x1} ${y1} ` +
      `L ${x1} ${y1b} C ${x1 - dx} ${y1b} ${x0 + dx} ${y0b} ${x0} ${y0b} Z`
    );
  }

  function renderSankeyFlow(elId, data) {
    const el = document.getElementById(elId);
    if (!el) return;

    const flows = (data && data.flows) || [];

    // Node totals for GEOMETRY come from the flows themselves, not the raw
    // sources/uses totals in `data` -- guarantees every node's ribbons
    // always exactly fill its bar. The two can genuinely differ: when
    // unbalanced_kwh > 0 (a short range where the battery charged from grid
    // before the window and discharged inside it, or similar), the source
    // total given to energy_flow_matrix() is less than the non-load use
    // demand, so some declared use demand has no source to draw a ribbon
    // from at all. The declared totals still drive the text labels; that
    // gap is exactly what the unbalanced_kwh caveat below explains.
    const sourceFlowTotal = {};
    const useFlowTotal = {};
    for (const f of flows) {
      sourceFlowTotal[f.from] = (sourceFlowTotal[f.from] || 0) + f.kwh;
      useFlowTotal[f.to] = (useFlowTotal[f.to] || 0) + f.kwh;
    }
    const sourceKeys = SANKEY_SOURCE_ORDER.filter((k) => (sourceFlowTotal[k] || 0) > 1e-9);
    const useKeys = SANKEY_USE_ORDER.filter((k) => (useFlowTotal[k] || 0) > 1e-9);

    if (sourceKeys.length === 0 || useKeys.length === 0) {
      el.innerHTML = `<p class="empty-state">No energy data for this period.</p>`;
      return;
    }

    const total = sourceKeys.reduce((sum, k) => sum + sourceFlowTotal[k], 0);

    // ---- geometry ----
    // Margins are wide (140px) because labels render OUTSIDE the node bars,
    // not inside them (a fixed-width bar can't hold "Battery discharge" at
    // any node size) -- 140px comfortably fits the longest label ("Battery
    // discharge", ~110px at 11px font) plus its value line underneath.
    const W = 760;
    const H = 340;
    const TOP = 20;
    const BOTTOM = 20;
    const NODE_GAP = 14;
    const BAR_W = 16;
    const MARGIN = 140;
    const SRC_X = MARGIN;
    const USE_X = W - MARGIN - BAR_W;
    const usableH = H - TOP - BOTTOM;
    // One scale for both columns, sized to whichever column has more nodes
    // (and so less room per kWh) -- a ribbon's width has to match at both
    // ends, so the two columns can't each pick their own scale. The shorter
    // column's stack is then centered in the remaining space.
    const maxGapCount = Math.max(sourceKeys.length, useKeys.length) - 1;
    const pxPerKwh = (usableH - maxGapCount * NODE_GAP) / total;

    function layoutColumn(keys, totals) {
      const contentH = keys.reduce((sum, k) => sum + totals[k] * pxPerKwh, 0) + (keys.length - 1) * NODE_GAP;
      let y = TOP + (usableH - contentH) / 2;
      const pos = {};
      for (const k of keys) {
        const h = totals[k] * pxPerKwh;
        pos[k] = { y, h };
        y += h + NODE_GAP;
      }
      return pos;
    }

    const srcPos = layoutColumn(sourceKeys, sourceFlowTotal);
    const usePos = layoutColumn(useKeys, useFlowTotal);
    // Running offset within each node's own band, so multiple ribbons
    // touching the same node stack without overlapping -- iterated in the
    // same fixed column order on both ends so ribbons cross only where they
    // genuinely must, matching the reference image's look.
    const srcCursor = {};
    sourceKeys.forEach((k) => (srcCursor[k] = srcPos[k].y));
    const useCursor = {};
    useKeys.forEach((k) => (useCursor[k] = usePos[k].y));

    let defs = "";
    let ribbons = "";
    let gradIndex = 0;
    for (const srcKey of sourceKeys) {
      for (const useKey of useKeys) {
        // A pair can carry BOTH a confidently-attributed portion and a
        // mopped-up (fallback) portion within the same period -- draw each
        // as its own ribbon segment, stacked in the node's band, rather
        // than blending them into one number the way a single .find() used
        // to. Confident portion first so the uncertain part reads as an
        // addition on top, not the whole ribbon.
        const matches = flows
          .filter((fl) => fl.from === srcKey && fl.to === useKey && fl.kwh > 1e-9)
          .sort((a, b) => Number(a.fallback) - Number(b.fallback));
        for (const f of matches) {
          const width = f.kwh * pxPerKwh;
          const y0 = srcCursor[srcKey];
          const y1 = useCursor[useKey];
          // Gradient id prefixed with elId -- SVG <defs> ids are document-
          // global, and this function re-renders on every range change.
          const gradId = `${elId}-grad-${gradIndex++}`;
          defs +=
            `<linearGradient id="${gradId}" x1="0" x2="1" y1="0" y2="0">` +
            `<stop offset="0" style="stop-color:var(${SANKEY_COLOR_VAR[srcKey]})" stop-opacity="0.55"></stop>` +
            `<stop offset="1" style="stop-color:var(${SANKEY_COLOR_VAR[useKey]})" stop-opacity="0.55"></stop>` +
            `</linearGradient>`;
          const ribbonCls = f.fallback ? "sankey-ribbon sankey-ribbon--fallback" : "sankey-ribbon";
          const tooltip = f.fallback
            ? `${sankeyLabel(srcKey)} → ${sankeyLabel(useKey)}: ${f.kwh.toFixed(2)} kWh (attribution uncertain -- see note below)`
            : `${sankeyLabel(srcKey)} → ${sankeyLabel(useKey)}: ${f.kwh.toFixed(2)} kWh`;
          ribbons +=
            `<path class="${ribbonCls}" d="${sankeyRibbonPath(SRC_X + BAR_W, y0, USE_X, y1, width)}" fill="url(#${gradId})">` +
            `<title>${escHtml(tooltip)}</title></path>`;
          srcCursor[srcKey] += width;
          useCursor[useKey] += width;
        }
      }
    }

    let nodes = "";
    for (const k of sourceKeys) {
      const { y, h } = srcPos[k];
      const value = (data.sources && data.sources[k]) || 0;
      nodes +=
        `<rect class="sankey-node" x="${SRC_X}" y="${y.toFixed(1)}" width="${BAR_W}" height="${h.toFixed(1)}" style="fill:var(${SANKEY_COLOR_VAR[k]})"></rect>` +
        `<text class="sankey-label" x="${SRC_X - 8}" y="${(y + h / 2 - 6).toFixed(1)}" text-anchor="end">${escHtml(sankeyLabel(k))}</text>` +
        `<text class="sankey-value" x="${SRC_X - 8}" y="${(y + h / 2 + 10).toFixed(1)}" text-anchor="end">${value.toFixed(2)} kWh</text>`;
    }
    for (const k of useKeys) {
      const { y, h } = usePos[k];
      const value = (data.uses && data.uses[k]) || 0;
      nodes +=
        `<rect class="sankey-node" x="${USE_X}" y="${y.toFixed(1)}" width="${BAR_W}" height="${h.toFixed(1)}" style="fill:var(${SANKEY_COLOR_VAR[k]})"></rect>` +
        `<text class="sankey-label" x="${USE_X + BAR_W + 8}" y="${(y + h / 2 - 6).toFixed(1)}" text-anchor="start">${escHtml(sankeyLabel(k))}</text>` +
        `<text class="sankey-value" x="${USE_X + BAR_W + 8}" y="${(y + h / 2 + 10).toFixed(1)}" text-anchor="start">${value.toFixed(2)} kWh</text>`;
    }

    let notes = "";
    if (data.pv_configured === false) {
      notes += `<p class="chart-hint">No solar production shown — configure a PV system under Settings &rarr; System to include it.</p>`;
    }
    const totalUses = useKeys.reduce((sum, k) => sum + ((data.uses && data.uses[k]) || 0), 0);
    if (data.fallback_kwh > 0.05 && totalUses > 0) {
      const pct = Math.round((data.fallback_kwh / totalUses) * 100);
      notes +=
        `<p class="chart-hint"><span class="sankey-fallback-swatch"></span>Dashed ribbons above (${escHtml(String(pct))}% ` +
        `of this period's flows, ${data.fallback_kwh.toFixed(2)} kWh) couldn't be attributed to a specific source — ` +
        `some days had both grid import and export (or both battery charge and discharge), which day-level data ` +
        `can't fully separate. Hover a ribbon for its exact source/use pairing.</p>`;
    }
    if (data.unbalanced_kwh > 0.05) {
      notes +=
        `<p class="chart-hint">${data.unbalanced_kwh.toFixed(2)} kWh of battery/grid activity this period couldn't ` +
        `be matched to household use at all — a short-range edge effect (e.g. the battery charged from grid before ` +
        `this window and discharged inside it).</p>`;
    }

    el.innerHTML = `
      <svg class="sankey-flow" viewBox="0 0 ${W} ${H}" xmlns="http://www.w3.org/2000/svg" role="img">
        <title>Energy flow: sources to uses, ${total.toFixed(2)} kWh total this period</title>
        <defs>${defs}</defs>
        ${ribbons}
        ${nodes}
      </svg>
      ${notes}`;
  }

  // ---- Theme + font size (persisted; theme defaults to OS preference via
  // the CSS media query until the user picks one explicitly) ----
  const FONT_STEP_PX = 2;
  const FONT_MIN_PX = 12;
  const FONT_MAX_PX = 24;

  function applyTheme(theme) {
    if (theme) {
      document.documentElement.dataset.theme = theme;
    } else {
      delete document.documentElement.dataset.theme;
    }
  }

  function applyFontSize(px) {
    document.documentElement.style.fontSize = `${px}px`;
  }

  function initThemeControls() {
    const savedTheme = localStorage.getItem("hw-theme");
    if (savedTheme) applyTheme(savedTheme);

    const savedFont = Number(localStorage.getItem("hw-font-px"));
    if (savedFont) applyFontSize(savedFont);

    document.getElementById("theme-toggle").addEventListener("click", () => {
      const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      const current = document.documentElement.dataset.theme || (prefersDark ? "dark" : "light");
      const next = current === "dark" ? "light" : "dark";
      applyTheme(next);
      localStorage.setItem("hw-theme", next);
    });

    document.getElementById("font-increase").addEventListener("click", () => {
      const current = parseFloat(getComputedStyle(document.documentElement).fontSize);
      const next = Math.min(FONT_MAX_PX, current + FONT_STEP_PX);
      applyFontSize(next);
      localStorage.setItem("hw-font-px", String(next));
    });

    document.getElementById("font-decrease").addEventListener("click", () => {
      const current = parseFloat(getComputedStyle(document.documentElement).fontSize);
      const next = Math.max(FONT_MIN_PX, current - FONT_STEP_PX);
      applyFontSize(next);
      localStorage.setItem("hw-font-px", String(next));
    });
  }

  // ---- Tabs ----
  function initTabs() {
    const buttons = document.querySelectorAll("nav.tabs button");
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        buttons.forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
        document.getElementById(`panel-${btn.dataset.tab}`).classList.add("active");
        loadPanel(btn.dataset.tab);
      });
    });
  }

  // Second-level tabs within a panel (Import, Settings) -- same click/active
  // mechanism as initTabs above, scoped per nav.subtabs so a click in one
  // panel's sub-nav never touches another panel's. Deliberately does NOT
  // call any loader on switch: unlike the top-level tabs, no .subpanel here
  // contains a <canvas> (every chart lives on Overview/Power/Gas/Water/
  // Battery/Sufficiency/Costs), and both loadImport() and loadSettings()
  // already fetch and populate everything for their whole tab in one pass
  // regardless of which sub-tab is showing -- so there is nothing a switch
  // would need to (re)load.
  function initSubTabs() {
    document.querySelectorAll("nav.subtabs").forEach((nav) => {
      const buttons = nav.querySelectorAll("button");
      const panels = nav.parentElement.querySelectorAll(".subpanel");
      buttons.forEach((btn) => {
        btn.addEventListener("click", () => {
          buttons.forEach((b) => b.classList.remove("active"));
          btn.classList.add("active");
          panels.forEach((p) => p.classList.remove("active"));
          document.getElementById(`subpanel-${btn.dataset.subtab}`).classList.add("active");
        });
      });
    });
  }

  // Keeps every tab's range controls (preset highlight, custom date-input
  // values, next-period button) showing the one shared `state` -- all
  // .range-controls blocks exist in the DOM from page load (one per tab,
  // just hidden via CSS until that tab is active), so a single global sweep
  // here reaches every tab at once regardless of which one is visible.
  // Called after every range-changing action; a tab picks up the current
  // range the instant it's switched to without needing its own sync call.
  function syncRangeControlsUI() {
    const { to } = rangeParams();
    const isAtToday = to === fmtDate(new Date());
    document.querySelectorAll(".range-controls").forEach((container) => {
      container.querySelectorAll(".preset-btn").forEach((b) => {
        const btnDays = b.dataset.days === "null" ? null : Number(b.dataset.days);
        b.classList.toggle("active", !state.customFrom && btnDays === state.rangeDays);
      });
      const inputs = container.querySelectorAll("input[type=date]");
      if (inputs.length === 2) {
        inputs[0].value = state.customFrom || "";
        inputs[1].value = state.customTo || "";
      }
      const nextBtn = container.querySelector(".range-nav-next");
      if (nextBtn) nextBtn.disabled = isAtToday;
    });
  }

  // Shifts the active range backward/forward by its own span (the gap
  // between its from/to dates) so the new window abuts the old one with no
  // overlap or gap -- e.g. a 7d range steps to the previous/next 7 days.
  // Always becomes a custom range afterward, even if it started as a preset.
  function shiftRange(direction) {
    const { from, to } = rangeParams();
    const fromDate = new Date(`${from}T00:00:00`);
    const toDate = new Date(`${to}T00:00:00`);
    const spanDays = Math.round((toDate - fromDate) / 86400000) || 1;

    let newFromDate, newToDate;
    if (direction === "prev") {
      newToDate = fromDate;
      newFromDate = new Date(fromDate);
      newFromDate.setDate(newFromDate.getDate() - spanDays);
    } else {
      newFromDate = toDate;
      newToDate = new Date(toDate);
      newToDate.setDate(newToDate.getDate() + spanDays);
    }

    const newTo = fmtDate(newToDate);
    // The next-period button is disabled once `to` is today, so this is a
    // defensive no-op for the normal click path, not the primary guard.
    if (newTo > fmtDate(new Date())) return;

    state.customFrom = fmtDate(newFromDate);
    state.customTo = newTo;
    syncRangeControlsUI();
    loadPanel(document.querySelector("nav.tabs button.active").dataset.tab);
  }

  function initRangeControls() {
    document.querySelectorAll(".range-controls").forEach((container) => {
      const presetsRow = document.createElement("div");
      presetsRow.className = "range-presets";
      RANGE_PRESETS.forEach((preset) => {
        const btn = document.createElement("button");
        btn.className = "preset-btn";
        btn.textContent = preset.label;
        btn.dataset.days = String(preset.days);
        btn.addEventListener("click", () => {
          const activeTab = document.querySelector("nav.tabs button.active").dataset.tab;
          if (preset.label === "1y") {
            // "1y" means the configured fiscal year for whichever
            // utility's tab is active, not a fixed 365-day trailing window.
            // Resolves to a concrete date range immediately and becomes a
            // custom range (same "always becomes custom" pattern shiftRange
            // already uses below) -- so switching tabs afterward keeps
            // showing that resolved window rather than re-deriving a
            // different one from a different tab's boundary.
            const cat = FISCAL_CATEGORY_BY_TAB[activeTab] || "power";
            const fy = state.fiscalYears[cat];
            const { from, to } = currentFiscalYearRange(fy.month, fy.day);
            state.customFrom = from;
            state.customTo = to;
          } else {
            state.rangeDays = preset.days;
            state.customFrom = null;
            state.customTo = null;
          }
          syncRangeControlsUI();
          loadPanel(activeTab);
        });
        presetsRow.appendChild(btn);
      });
      container.appendChild(presetsRow);

      const customRow = document.createElement("div");
      customRow.className = "range-custom";

      const prevBtn = document.createElement("button");
      prevBtn.type = "button";
      prevBtn.className = "range-nav-btn range-nav-prev";
      prevBtn.textContent = "<";
      prevBtn.title = "Previous period";
      prevBtn.addEventListener("click", () => shiftRange("prev"));

      const fromInput = document.createElement("input");
      fromInput.type = "date";
      const toInput = document.createElement("input");
      toInput.type = "date";

      const nextBtn = document.createElement("button");
      nextBtn.type = "button";
      nextBtn.className = "range-nav-btn range-nav-next";
      nextBtn.textContent = ">";
      nextBtn.title = "Next period";
      nextBtn.addEventListener("click", () => shiftRange("next"));

      const applyBtn = document.createElement("button");
      applyBtn.textContent = "Zoom";
      applyBtn.addEventListener("click", () => {
        if (!fromInput.value || !toInput.value) return;
        state.customFrom = fromInput.value;
        state.customTo = toInput.value;
        syncRangeControlsUI();
        loadPanel(document.querySelector("nav.tabs button.active").dataset.tab);
      });
      customRow.appendChild(prevBtn);
      customRow.appendChild(fromInput);
      customRow.appendChild(document.createTextNode(" to "));
      customRow.appendChild(toInput);
      customRow.appendChild(nextBtn);
      customRow.appendChild(applyBtn);
      container.appendChild(customRow);

      // Drag-to-select does the same job as the From/To + Zoom controls
      // beside it, just faster -- so it is introduced here rather than
      // repeated as a note under every chart.
      const dragHint = document.createElement("span");
      dragHint.className = "range-drag-hint";
      dragHint.textContent = "or drag across a chart to zoom into that period";
      container.appendChild(dragHint);
    });
    syncRangeControlsUI();
  }

  // ---- Overview ----
  async function loadOverview() {
    const el = document.getElementById("overview-tiles");
    el.innerHTML = "";
    try {
      const { from, to } = rangeParams();
      const data = await fetchJson(`/api/overview?from=${from}&to=${to}`);
      state.lastRefreshedIso = data.last_refreshed;
      renderLastRefreshed();
      const period = rangeLabel();
      // "End of period" is only informative when the window's end isn't today —
      // every quick-select preset anchors `to` at today, so it would otherwise
      // always equal "current SoC" regardless of which period is selected.
      const isHistoricalWindow = to !== fmtDate(new Date());
      // current_soc_date lets the tile show its own freshness --
      // without it, a silently-stopped battery poller leaves the last real
      // reading on screen forever with nothing to flag it's frozen. Stale
      // threshold is "not today" (local calendar date, same fmtDate() used
      // for the isHistoricalWindow check above) -- battery_daily is rebuilt
      // roughly every minute while the poller is healthy (see
      // homewizard_api_ingest_cli.py's REBUILD_EVERY_N_POLLS), so a date
      // behind today already means at least several hours with no fresh data.
      const socDate = data.battery && data.battery.current_soc_date;
      const socStale = socDate != null && socDate !== fmtDate(new Date());
      const socNote = socDate == null ? null : { text: `as of ${socDate}`, stale: socStale };

      // Power has no visibility toggle (every install has a P1
      // meter) so its tiles are unconditional; Gas/Water/Battery tiles are
      // skipped entirely when their tab is hidden, rather than shown with a
      // "—" placeholder that would just look broken/empty for a category
      // the user doesn't have.
      const tiles = [
        ["Power import", period, data.power ? `${data.power.import_kwh.toFixed(2)} kWh` : "—"],
        ["Power export", period, data.power ? `${data.power.export_kwh.toFixed(2)} kWh` : "—"],
      ];
      if (state.visibility.gas) {
        tiles.push(["Gas", period, data.gas ? `${data.gas.usage_m3.toFixed(2)} m3` : "—"]);
      }
      if (state.visibility.water) {
        tiles.push(["Water", period, data.water ? `${data.water.usage_l.toFixed(0)} L` : "—"]);
      }
      if (state.visibility.battery) {
        tiles.push(
          ["Battery SoC (avg)", period, data.battery && data.battery.avg_soc_pct != null ? `${data.battery.avg_soc_pct.toFixed(0)}%` : "—"],
          ["Current battery SoC", null, data.battery && data.battery.current_soc_pct != null ? `${data.battery.current_soc_pct.toFixed(0)}%` : "—", socNote]
        );
        if (isHistoricalWindow) {
          tiles.push([
            "Battery SoC (end of period)",
            period,
            data.battery && data.battery.eod_soc_pct != null ? `${data.battery.eod_soc_pct.toFixed(0)}%` : "—",
          ]);
        }
      }
      for (const [label, period_, value, note] of tiles) {
        const tile = document.createElement("div");
        tile.className = "stat-tile";
        const labelHtml = period_ ? `${escHtml(label)} (${escHtml(period_)})` : escHtml(label);
        const noteHtml = note
          ? `<div class="note${note.stale ? " stale" : ""}">${escHtml(note.text)}</div>`
          : "";
        tile.innerHTML = `<div class="label">${labelHtml}</div><div class="value">${escHtml(value)}</div>${noteHtml}`;
        el.appendChild(tile);
      }

      const [powerRows, gasRows, batteryRows, waterRows, energyFlow] = await Promise.all([
        fetchJson(`/api/power/daily?from=${from}&to=${to}`),
        fetchJson(`/api/gas/daily?from=${from}&to=${to}`),
        fetchJson(`/api/battery/daily?from=${from}&to=${to}`),
        fetchJson(`/api/water/daily?from=${from}&to=${to}`),
        fetchJson(`/api/energy-flow?from=${from}&to=${to}`),
      ]);
      divergingBarChart(
        "overview-chart-power",
        powerRows.map((r) => r.date),
        "Import",
        powerRows.map((r) => r.import_kwh),
        "--series-power-import",
        "Export",
        powerRows.map((r) => r.export_kwh),
        "--series-power-export"
      );
      lineChart(
        "overview-chart-gas",
        gasRows.map((r) => r.date),
        [seriesDataset("Gas (m3)", gasRows.map((r) => r.usage_m3), "--series-gas")]
      );
      renderChartAvg("overview-power-avg", [
        { label: "Avg import", value: fmtAvg(avg(powerRows, "import_kwh"), 1, "kWh/day"), colorVar: "--series-power-import" },
        { label: "Avg export", value: fmtAvg(avg(powerRows, "export_kwh"), 1, "kWh/day"), colorVar: "--series-power-export" },
      ]);
      renderChartAvg("overview-gas-avg", [
        { label: "Avg", value: fmtAvg(avg(gasRows, "usage_m3"), 2, "m3/day") },
      ]);

      renderEnergyFlow("overview-flow", data);
      // Same visibility filtering as the tiles above -- energy-flow
      // diagram and the Highs & Lows table further down are deliberately
      // left unfiltered this pass (fixed-position SVG layout / lower
      // priority), tracked as a known follow-up.
      renderSankeyFlow("overview-sankey", energyFlow);
      const sourceRows = [
        { label: "Power import", value: data.power ? `${data.power.import_kwh.toFixed(2)} kWh` : "—", colorVar: "--series-power-import" },
        { label: "Power export", value: data.power ? `${data.power.export_kwh.toFixed(2)} kWh` : "—", colorVar: "--series-power-export" },
      ];
      if (state.visibility.battery) {
        sourceRows.push(
          { label: "Battery charge", value: data.battery ? `${data.battery.charge_kwh.toFixed(1)} kWh` : "—", colorVar: "--series-battery-charge" },
          { label: "Battery discharge", value: data.battery ? `${data.battery.discharge_kwh.toFixed(1)} kWh` : "—", colorVar: "--series-battery-discharge" }
        );
      }
      if (state.visibility.gas) {
        sourceRows.push({ label: "Gas", value: data.gas ? `${data.gas.usage_m3.toFixed(2)} m3` : "—", colorVar: "--series-gas" });
      }
      if (state.visibility.water) {
        sourceRows.push({ label: "Water", value: data.water ? `${data.water.usage_l.toFixed(0)} L` : "—", colorVar: "--series-water" });
      }
      renderSourcesTable("overview-sources", sourceRows);
      renderHighLowTable("overview-highlow", [
        { label: "Power import", colorVar: "--series-power-import", rows: powerRows, key: "import_kwh", decimals: 1, unit: "kWh" },
        { label: "Power export", colorVar: "--series-power-export", rows: powerRows, key: "export_kwh", decimals: 1, unit: "kWh" },
        { label: "Battery charge", colorVar: "--series-battery-charge", rows: batteryRows, key: "charge_kwh", decimals: 1, unit: "kWh" },
        { label: "Battery discharge", colorVar: "--series-battery-discharge", rows: batteryRows, key: "discharge_kwh", decimals: 1, unit: "kWh" },
        { label: "Gas", colorVar: "--series-gas", rows: gasRows, key: "usage_m3", decimals: 2, unit: "m3" },
        { label: "Water", colorVar: "--series-water", rows: waterRows, key: "usage_l", decimals: 0, unit: "L" },
      ]);
    } catch (e) {
      el.innerHTML = `<div class="empty-state">Could not load overview data.</div>`;
    }
  }

  // ---- Power ----
  async function loadPower() {
    const { from, to } = rangeParams();
    const [rows, occupancy, sun] = await Promise.all([
      fetchJson(`/api/power/daily?from=${from}&to=${to}`),
      fetchJson("/api/settings/occupancy"),
      fetchWeatherDays(),
    ]);
    const labels = rows.map((r) => r.date);
    divergingBarChart(
      "chart-power",
      labels,
      "Import",
      rows.map((r) => r.import_kwh),
      "--series-power-import",
      "Export",
      rows.map((r) => r.export_kwh),
      "--series-power-export",
      sun ? [occupancyOverlayPlugin(labels, occupancy), sunRailPlugin(labels, sun.days)] : [occupancyOverlayPlugin(labels, occupancy)]
    );
    setSunRailHint("power-sun-hint", sun);
    lineChart("chart-power-phase", labels, [
      seriesDataset("L1 max W", rows.map((r) => r.l1_max_w), "--series-power-import"),
      seriesDataset("L2 max W", rows.map((r) => r.l2_max_w), "--series-power-export"),
      seriesDataset("L3 max W", rows.map((r) => r.l3_max_w), "--series-gas"),
    ]);
    renderChartAvg("power-import-export-avg", [
      { label: "Avg import", value: fmtAvg(avg(rows, "import_kwh"), 1, "kWh/day"), colorVar: "--series-power-import" },
      { label: "Avg export", value: fmtAvg(avg(rows, "export_kwh"), 1, "kWh/day"), colorVar: "--series-power-export" },
    ]);
    renderChartAvg("power-phase-avg", [
      { label: "Avg L1", value: fmtAvg(avg(rows, "l1_max_w"), 0, "W"), colorVar: "--series-power-import" },
      { label: "Avg L2", value: fmtAvg(avg(rows, "l2_max_w"), 0, "W"), colorVar: "--series-power-export" },
      { label: "Avg L3", value: fmtAvg(avg(rows, "l3_max_w"), 0, "W"), colorVar: "--series-gas" },
    ]);
    const totalImport = rows.reduce((s, r) => s + (r.import_kwh || 0), 0);
    const totalExport = rows.reduce((s, r) => s + (r.export_kwh || 0), 0);
    renderSourcesTable("power-sources", [
      { label: "Grid import", value: `${totalImport.toFixed(1)} kWh`, colorVar: "--series-power-import" },
      { label: "Grid export", value: `${totalExport.toFixed(1)} kWh`, colorVar: "--series-power-export" },
    ]);
    renderHighLowTable("power-highlow", [
      { label: "Grid import", colorVar: "--series-power-import", rows, key: "import_kwh", decimals: 1, unit: "kWh" },
      { label: "Grid export", colorVar: "--series-power-export", rows, key: "export_kwh", decimals: 1, unit: "kWh" },
    ]);
  }

  // ---- Gas ----
  async function loadGas() {
    const { from, to } = rangeParams();
    const [rows, occupancy, gasWeather] = await Promise.all([
      fetchJson(`/api/gas/daily?from=${from}&to=${to}`),
      fetchJson("/api/settings/occupancy"),
      fetchGasWeatherDays(),
    ]);
    const labels = rows.map((r) => r.date);
    const extraPlugins = [occupancyOverlayPlugin(labels, occupancy)];
    if (gasWeather) extraPlugins.push(gasHeatingRailPlugin(labels, gasWeather.days));
    lineChart(
      "chart-gas",
      labels,
      [seriesDataset("Gas (m3)", rows.map((r) => r.usage_m3), "--series-gas")],
      extraPlugins
    );
    setGasHeatingRailHint("gas-weather-hint", gasWeather);
    renderChartAvg("gas-avg", [{ label: "Avg", value: fmtAvg(avg(rows, "usage_m3"), 2, "m3/day") }]);
    const totalGas = rows.reduce((s, r) => s + (r.usage_m3 || 0), 0);
    renderSourcesTable("gas-sources", [{ label: "Gas", value: `${totalGas.toFixed(2)} m3`, colorVar: "--series-gas" }]);
    renderHighLowTable("gas-highlow", [
      { label: "Gas", colorVar: "--series-gas", rows, key: "usage_m3", decimals: 2, unit: "m3" },
    ]);
  }

  // ---- Water ----
  async function loadWater() {
    const { from, to } = rangeParams();
    const [rows, occupancy] = await Promise.all([
      fetchJson(`/api/water/daily?from=${from}&to=${to}`),
      fetchJson("/api/settings/occupancy"),
    ]);
    const labels = rows.map((r) => r.date);
    lineChart(
      "chart-water",
      labels,
      [seriesDataset("Water (L)", rows.map((r) => r.usage_l), "--series-water")],
      [occupancyOverlayPlugin(labels, occupancy)]
    );
    renderChartAvg("water-avg", [{ label: "Avg", value: fmtAvg(avg(rows, "usage_l"), 0, "L/day") }]);
    const totalWater = rows.reduce((s, r) => s + (r.usage_l || 0), 0);
    renderSourcesTable("water-sources", [{ label: "Water", value: `${totalWater.toFixed(0)} L`, colorVar: "--series-water" }]);
    renderHighLowTable("water-highlow", [
      { label: "Water", colorVar: "--series-water", rows, key: "usage_l", decimals: 0, unit: "L" },
    ]);
  }

  // ---- Battery ----
  async function loadBattery() {
    const { from, to } = rangeParams();
    const rows = await fetchJson(`/api/battery/daily?from=${from}&to=${to}`);
    const labels = rows.map((r) => r.date);
    const sun = await fetchWeatherDays();
    divergingBarChart(
      "chart-battery-flow",
      labels,
      "Charge",
      rows.map((r) => r.charge_kwh),
      "--series-battery-charge",
      "Discharge",
      rows.map((r) => r.discharge_kwh),
      "--series-battery-discharge",
      sun ? [sunRailPlugin(labels, sun.days)] : []
    );
    setSunRailHint("battery-flow-hint", sun);
    lineChart(
      "chart-battery-soc",
      labels,
      [
        seriesDataset("Avg SoC %", rows.map((r) => r.avg_soc_pct), "--series-battery-soc"),
        seriesDataset("Max SoC %", rows.map((r) => r.max_soc_pct), "--series-battery-charge", { borderDash: [4, 3] }),
      ],
      sun ? [sunRailPlugin(labels, sun.days)] : []
    );
    setSunRailHint("battery-soc-hint", sun);
    renderChartAvg("battery-flow-avg", [
      { label: "Avg charge", value: fmtAvg(avg(rows, "charge_kwh"), 1, "kWh/day"), colorVar: "--series-battery-charge" },
      { label: "Avg discharge", value: fmtAvg(avg(rows, "discharge_kwh"), 1, "kWh/day"), colorVar: "--series-battery-discharge" },
    ]);
    renderChartAvg("battery-soc-avg", [
      { label: "Avg", value: fmtAvg(avg(rows, "avg_soc_pct"), 0, "%"), colorVar: "--series-battery-soc" },
      { label: "Avg of daily max", value: fmtAvg(avg(rows, "max_soc_pct"), 0, "%"), colorVar: "--series-battery-charge" },
    ]);
    const totalCharge = rows.reduce((s, r) => s + (r.charge_kwh || 0), 0);
    const totalDischarge = rows.reduce((s, r) => s + (r.discharge_kwh || 0), 0);
    renderSourcesTable("battery-sources", [
      { label: "Charge (from grid/solar)", value: `${totalCharge.toFixed(1)} kWh`, colorVar: "--series-battery-charge" },
      { label: "Discharge (to house)", value: `${totalDischarge.toFixed(1)} kWh`, colorVar: "--series-battery-discharge" },
    ]);
    renderHighLowTable("battery-highlow", [
      { label: "Charge (from grid/solar)", colorVar: "--series-battery-charge", rows, key: "charge_kwh", decimals: 1, unit: "kWh" },
      { label: "Discharge (to house)", colorVar: "--series-battery-discharge", rows, key: "discharge_kwh", decimals: 1, unit: "kWh" },
    ]);
  }

  // ---- Self-sufficiency ----
  async function loadSufficiency() {
    const { from, to } = rangeParams();
    const el = document.getElementById("sufficiency-body");
    const data = await fetchJson(`/api/self-sufficiency?from=${from}&to=${to}`);
    if (!data.available) {
      el.innerHTML = `<div class="empty-state">${escHtml(data.reason || "Not available yet.")} Add your PV kWp rating under Settings.</div>`;
      return;
    }
    // Which model produced these numbers is stated, never implied: a silently
    // weather-adjusted estimate is less trustworthy than a visibly labelled
    // one, because the reader cannot sanity-check a number whose basis they
    // cannot see. Degrades per day, so a range straddling the start of weather
    // coverage still uses real radiation for the days that have it.
    const weatherDays = data.weather_days || 0;
    const basisNote =
      weatherDays === data.days.length
        ? "weather-adjusted from measured solar radiation"
        : weatherDays > 0
          ? `weather-adjusted for ${weatherDays} of ${data.days.length} days; the rest use the seasonal average`
          : "seasonal average — no weather data for this range";
    el.innerHTML = `
      <div class="chart-card">
        <div class="chart-card-header">
          <h3>Estimated self-sufficiency (%)</h3>
          <div class="chart-avg" id="sufficiency-avg"></div>
        </div>
        <p class="chart-hint">Basis: ${escHtml(basisNote)}.</p>
        ${
          data.sun_pct_of_typical != null
            ? `<p class="sun-readout">Sun this period ${sunMeter(data.sun_pct_of_typical)} ` +
              `<strong>${escHtml(data.sun_pct_of_typical)}%</strong> of typical for these dates</p>`
            : ""
        }
        <canvas id="chart-sufficiency"></canvas>
        <div class="weather-credit" id="sufficiency-weather-credit"></div>
      </div>`;
    const sufficiencyLabels = data.days.map((d) => d.date);
    const sufficiencySun = await fetchWeatherDays();
    lineChart(
      "chart-sufficiency",
      sufficiencyLabels,
      [seriesDataset("Self-sufficiency %", data.days.map((d) => d.self_sufficiency_pct), "--series-estimate")],
      sufficiencySun ? [sunRailPlugin(sufficiencyLabels, sufficiencySun.days)] : []
    );
    renderChartAvg("sufficiency-avg", [
      { label: "Avg", value: fmtAvg(avg(data.days, "self_sufficiency_pct"), 1, "%"), colorVar: "--series-estimate" },
    ]);
    renderWeatherCredit("sufficiency-weather-credit", data.weather_attribution);
  }

  // ---- Costs ----
  async function loadCosts() {
    const { from, to } = rangeParams();
    const el = document.getElementById("costs-body");
    const data = await fetchJson(`/api/costs?from=${from}&to=${to}`);
    if (!data.available) {
      el.innerHTML = `<div class="empty-state">${escHtml(data.reason || "Not available yet.")} Add rate periods under Settings.</div>`;
      return;
    }
    const totalPower = data.days.reduce((sum, d) => sum + (d.power_cost_eur || 0), 0);
    const totalGas = data.days.reduce((sum, d) => sum + (d.gas_cost_eur || 0), 0);
    const avgPower = avg(data.days, "power_cost_eur");
    const avgGas = avg(data.days, "gas_cost_eur");
    const avgTotal = avg(data.days, "total_cost_eur");
    const staleNote =
      data.stale_count > 0
        ? `<p class="empty-state" style="text-align:left;padding:10px 14px;">` +
          `${data.stale_count} of ${data.days.length} day(s) in this range have no matching Vattenfall rate period yet ` +
          `&mdash; the most recent known rate was carried forward. Upload a new tariff PDF under Settings once your ` +
          `contract renews.</p>`
        : "";
    el.innerHTML = `
      <div class="stat-tiles">
        <div class="stat-tile"><div class="label">Power cost, selected period</div><div class="value">&euro;${totalPower.toFixed(2)}</div></div>
        <div class="stat-tile"><div class="label">Gas cost, selected period</div><div class="value">&euro;${totalGas.toFixed(2)}</div></div>
        <div class="stat-tile"><div class="label">Total, selected period</div><div class="value">&euro;${(totalPower + totalGas).toFixed(2)}</div></div>
        <div class="stat-tile"><div class="label">Power cost, avg/day</div><div class="value">${escHtml(fmtAvg(avgPower, 2, "€/day"))}</div></div>
        <div class="stat-tile"><div class="label">Gas cost, avg/day</div><div class="value">${escHtml(fmtAvg(avgGas, 2, "€/day"))}</div></div>
        <div class="stat-tile"><div class="label">Total, avg/day</div><div class="value">${escHtml(fmtAvg(avgTotal, 2, "€/day"))}</div></div>
      </div>
      ${staleNote}
      <div class="chart-card"><h3>Daily cost (&euro;)</h3><canvas id="chart-costs"></canvas></div>`;
    lineChart(
      "chart-costs",
      data.days.map((d) => d.date),
      [
        seriesDataset("Power (EUR)", data.days.map((d) => d.power_cost_eur), "--series-power-import"),
        seriesDataset("Gas (EUR)", data.days.map((d) => d.gas_cost_eur), "--series-gas"),
      ]
    );
  }

  // ---- Comparison ----
  // Deliberately local, tab-scoped state -- NOT the shared global `state`
  // object rangeParams() reads. That state is explicitly single-range and
  // kept in lockstep across every other tab (syncRangeControlsUI); a second
  // independent period has no home there without breaking that invariant
  // for every other tab. Comparison owns its own from/to pair per side.
  const comparisonState = { a: { from: null, to: null }, b: { from: null, to: null } };

  // Defaults to "this week vs last week" (abutting 7-day windows ending
  // today) so the tab shows something useful before the user touches a date
  // input, rather than an empty form. Either side becomes a single-day
  // comparison just by setting From = To -- no separate UI mode needed.
  function initComparisonDefaults() {
    const today = new Date();
    const bTo = fmtDate(today);
    const bFromDate = new Date(today);
    bFromDate.setDate(bFromDate.getDate() - 6);
    const bFrom = fmtDate(bFromDate);

    const aToDate = new Date(bFromDate);
    aToDate.setDate(aToDate.getDate() - 1);
    const aTo = fmtDate(aToDate);
    const aFromDate = new Date(aToDate);
    aFromDate.setDate(aFromDate.getDate() - 6);
    const aFrom = fmtDate(aFromDate);

    comparisonState.a = { from: aFrom, to: aTo };
    comparisonState.b = { from: bFrom, to: bTo };
  }

  function initComparisonControls() {
    initComparisonDefaults();
    document.getElementById("compare-a-from").value = comparisonState.a.from;
    document.getElementById("compare-a-to").value = comparisonState.a.to;
    document.getElementById("compare-b-from").value = comparisonState.b.from;
    document.getElementById("compare-b-to").value = comparisonState.b.to;

    const bind = (id, period, field) => {
      document.getElementById(id).addEventListener("change", (e) => {
        comparisonState[period][field] = e.target.value;
        loadComparison().catch((err) => console.error("failed to load comparison", err));
      });
    };
    bind("compare-a-from", "a", "from");
    bind("compare-a-to", "a", "to");
    bind("compare-b-from", "b", "from");
    bind("compare-b-to", "b", "to");
  }

  // Arrow direction only -- not a good/bad color judgment, see style.css.
  function fmtDelta(a, b, decimals) {
    if (a == null || b == null) return "—";
    const delta = b - a;
    if (delta === 0) return delta.toFixed(decimals);
    const cls = delta > 0 ? "delta-up" : "delta-down";
    const arrow = delta > 0 ? "▲" : "▼";
    return `<span class="${cls}">${arrow} ${Math.abs(delta).toFixed(decimals)}</span>`;
  }

  function fmtDeltaPct(a, b) {
    if (a == null || b == null || a === 0) return "—";
    const pct = ((b - a) / Math.abs(a)) * 100;
    if (pct === 0) return "0.0%";
    const cls = pct > 0 ? "delta-up" : "delta-down";
    const arrow = pct > 0 ? "▲" : "▼";
    return `<span class="${cls}">${arrow} ${Math.abs(pct).toFixed(1)}%</span>`;
  }

  function renderComparisonTable(el, data) {
    const items = [
      { label: "Power import (kWh)", colorVar: "--series-power-import", get: (p) => p.power.import_kwh, decimals: 2 },
      { label: "Power export (kWh)", colorVar: "--series-power-export", get: (p) => p.power.export_kwh, decimals: 2 },
      { label: "Gas (m³)", colorVar: "--series-gas", get: (p) => p.gas.usage_m3, decimals: 2 },
      { label: "Water (L)", colorVar: "--series-water", get: (p) => p.water.usage_l, decimals: 0 },
      { label: "Battery charge (kWh)", colorVar: "--series-battery-charge", get: (p) => p.battery.charge_kwh, decimals: 2 },
      { label: "Battery discharge (kWh)", colorVar: "--series-battery-discharge", get: (p) => p.battery.discharge_kwh, decimals: 2 },
      { label: "Battery avg SoC (%)", colorVar: "--series-battery-soc", get: (p) => p.battery.avg_soc_pct, decimals: 1 },
      { label: "Avg headcount (people)", colorVar: "--series-estimate", get: (p) => p.occupancy.avg_headcount, decimals: 1 },
    ];
    const body = items
      .map((it) => {
        const a = it.get(data.period_a);
        const b = it.get(data.period_b);
        return (
          `<tr><td class="swatch-cell"><span class="swatch" style="background:${cssVar(it.colorVar)}"></span></td>` +
          `<td>${escHtml(it.label)}</td>` +
          `<td class="value-cell">${escHtml(a == null ? "—" : a.toFixed(it.decimals))}</td>` +
          `<td class="value-cell">${escHtml(b == null ? "—" : b.toFixed(it.decimals))}</td>` +
          `<td class="value-cell">${fmtDelta(a, b, it.decimals)}</td>` +
          `<td class="value-cell">${fmtDeltaPct(a, b)}</td></tr>`
        );
      })
      .join("");
    el.innerHTML =
      `<table class="sources-table hl-table"><thead><tr><th></th><th>Metric</th>` +
      `<th>Period A</th><th>Period B</th><th>&Delta;</th><th>&Delta;%</th></tr></thead><tbody>${body}</tbody></table>`;
  }

  async function loadComparison() {
    const el = document.getElementById("comparison-body");
    if (!el) return;
    const { a, b } = comparisonState;
    if (!a.from || !a.to || !b.from || !b.to) return;
    if (a.to < a.from || b.to < b.from) {
      el.innerHTML = `<p class="empty-state">End date is before start date.</p>`;
      return;
    }
    const data = await fetchJson(`/api/compare?a_from=${a.from}&a_to=${a.to}&b_from=${b.from}&b_to=${b.to}`);
    renderComparisonTable(el, data);
    refreshConsumptionNotesIfShown();
  }

  // Consumption notes are range-scoped now, so changing the range
  // leaves an already-displayed result describing a period that is no longer
  // selected -- a count that silently means something else. Re-run it
  // instead. Guarded on the range line being visible, i.e. the user has
  // already asked for this check at least once: the card's "not run
  // automatically" promise (so a legitimate spike never ambushes
  // anyone) still holds for a fresh page load.
  function refreshConsumptionNotesIfShown() {
    const rangeEl = document.getElementById("consumption-notes-range");
    if (rangeEl && !rangeEl.hidden) document.getElementById("check-consumption-notes").click();
  }

  // ---- Settings ----
  // Split into one loader per section (rather than a single loadSettings()
  // that repopulates every field) so that saving one form only ever
  // refreshes its own fields. Before this split, submitting *any* Settings
  // form re-fetched and overwrote every other form's fields too -- editing
  // PV notes, then adding a rate period before saving the PV form, silently
  // discarded the unsaved PV edit with no warning. Found while testing the
  // fiscal-year/toggle forms below, but reproduced with the original
  // PV + rate forms too -- not new to this pair, just newly noticed.
  async function loadPvSection() {
    const pv = await fetchJson("/api/settings/pv");
    if (pv.kwp_rating != null) {
      document.getElementById("pv-kwp").value = pv.kwp_rating;
      document.getElementById("pv-installed-date").value = pv.installed_date || "";
      document.getElementById("pv-notes").value = pv.notes || "";
    }
  }

  // Mirrors db.OPEN_ENDED_SENTINEL -- a rate period with no
  // known end date yet. Kept as a real far-future date server-side so
  // existing string comparisons need no special-casing; only display needs
  // to translate it back to something readable.
  const OPEN_ENDED_SENTINEL = "9999-12-31";
  const fmtPeriodEnd = (v) => (v === OPEN_ENDED_SENTINEL ? "ongoing" : v);

  async function loadRateTable() {
    const rates = await fetchJson("/api/settings/rates");
    const tbody = document.querySelector("#rate-table tbody");
    tbody.innerHTML = rates
      .slice()
      .reverse()
      .map(
        (r) =>
          `<tr><td>${escHtml(r.period_start)}</td><td>${escHtml(fmtPeriodEnd(r.period_end))}</td>` +
          `<td>${escHtml(r.buy_ct_per_kwh.toFixed(4))}</td><td>${escHtml(r.sell_ct_per_kwh.toFixed(4))}</td>` +
          `<td>${escHtml(r.source || "")}</td></tr>`
      )
      .join("");
  }

  async function loadGasRateTable() {
    const gasRates = await fetchJson("/api/settings/gas-rates");
    const gasTbody = document.querySelector("#gas-rate-table tbody");
    gasTbody.innerHTML = gasRates
      .slice()
      .reverse()
      .map(
        (r) =>
          `<tr><td>${escHtml(r.period_start)}</td><td>${escHtml(fmtPeriodEnd(r.period_end))}</td>` +
          `<td>${escHtml(r.price_eur_per_m3.toFixed(4))}</td><td>${escHtml(r.source || "")}</td></tr>`
      )
      .join("");
  }

  async function loadOccupancyTable() {
    const entries = await fetchJson("/api/settings/occupancy");
    const tbody = document.querySelector("#occupancy-table tbody");
    tbody.innerHTML = entries
      .slice()
      .reverse()
      .map(
        (r) =>
          `<tr><td>${escHtml(fmtOccupancyDateTime(r.date_from))}</td><td>${escHtml(fmtOccupancyDateTime(r.date_to))}</td>` +
          `<td>${escHtml(r.occupant_count)}</td><td>${escHtml(r.notes || "")}</td>` +
          `<td><button type="button" class="btn-delete" data-id="${r.id}">Delete</button></td></tr>`
      )
      .join("");
    // Stats depend on occupancy_log's full content, so re-derive them every
    // time this table reloads (add, delete, or the tab's initial open) --
    // one call site to keep, rather than remembering to refresh both
    // wherever occupancy_log changes.
    await loadOccupancyStats();
    await loadOccupancySuggestions();
  }

  // Stretches the meters say the house was empty which the log does
  // not record as empty. The mechanism for fixing them already existed --
  // occupancy_log allows nested entries and resolves most-specific-first --
  // but logging one meant a hand-written API call, which is precisely why 12
  // days inside a three-month visit were never recorded.
  async function loadOccupancySuggestions() {
    const el = document.getElementById("occupancy-suggestions");
    if (!el) return;
    let items;
    try {
      items = await fetchJson("/api/occupancy/suggestions");
    } catch (err) {
      el.innerHTML = `<p class="empty-state">Could not check: ${escHtml(err)}</p>`;
      return;
    }
    if (items.length === 0) {
      el.innerHTML = `<p class="empty-state">No unlogged empty stretches — the log agrees with the meters.</p>`;
      return;
    }

    const describe = (counts) => {
      const unlogged = counts.includes(null);
      const people = counts.filter((c) => c !== null);
      if (!people.length) return "not logged";
      const label = people.map((c) => (c === 0 ? "empty" : `${c} home`)).join(" / ");
      return unlogged ? `${label}, part not logged` : label;
    };

    const rows = items
      .map((s, i) => {
        const span = s.start === s.end ? s.start : `${s.start} → ${s.end}`;
        return (
          `<tr><td>${escHtml(span)}</td><td>${s.days}</td>` +
          `<td>${escHtml(describe(s.logged_counts))}</td>` +
          `<td><button type="button" data-suggest="${i}">Log as away</button></td></tr>`
        );
      })
      .join("");

    el.innerHTML =
      `<p class="muted">${items.length} stretch${items.length === 1 ? "" : "es"} where gas and water ` +
      `both went quiet but the log doesn't say the house was empty. Logging one adds a nested entry — ` +
      `it overrides the surrounding period for those days only, and changes nothing else.</p>` +
      `<div class="table-scroll"><table class="data-table">` +
      `<thead><tr><th>Dates</th><th>Days</th><th>Currently logged as</th><th></th></tr></thead>` +
      `<tbody>${rows}</tbody></table></div>`;

    el.querySelectorAll("button[data-suggest]").forEach((btn) => {
      btn.addEventListener("click", () => logSuggestedAbsence(items[Number(btn.dataset.suggest)]));
    });
  }

  async function logSuggestedAbsence(s) {
    const span = s.start === s.end ? s.start : `${s.start} to ${s.end}`;
    if (!confirm(`Log ${span} (${s.days} day${s.days === 1 ? "" : "s"}) as nobody home?\n\n` +
        `This adds an occupancy entry with 0 people. Any surrounding entry stays as it is — ` +
        `the new one only applies to these dates. You can delete it again from the table above.`)) {
      return;
    }
    try {
      const res = await fetch("/api/settings/occupancy", {
        method: "POST",
        headers: writeHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          date_from: `${s.start} 00:00`,
          date_to: `${s.end} 23:59`,
          occupant_count: 0,
          notes: "Away (detected: no gas or water used)",
        }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        alert(body.error || "Could not add the entry.");
      }
    } catch (err) {
      alert(`Could not add the entry: ${err}`);
    }
    await loadOccupancyTable();
  }

  function fmtStat(value, decimals, unit) {
    return value == null ? "—" : `${value.toFixed(decimals)} ${unit}`;
  }

  // All-time, deliberately not tied to the shared global range -- this is a
  // settings-adjacent summary of the whole logged occupancy history, not a
  // per-tab chart. "2000-01-01" matches the existing "All" preset's bound
  // (rangeParams()) rather than inventing a second convention for the same
  // idea.
  async function loadOccupancyStats() {
    const el = document.getElementById("occupancy-stats");
    if (!el) return;
    const stats = await fetchJson(`/api/occupancy-stats?from=2000-01-01&to=${fmtDate(new Date())}`);
    if (!stats.available) {
      el.innerHTML = `<p class="empty-state">${escHtml(stats.reason)}</p>`;
      return;
    }
    const items = [
      { label: "Power (kWh/day)", colorVar: "--series-power-import", key: "power", decimals: 2, unit: "kWh" },
      { label: "Gas (m³/day)", colorVar: "--series-gas", key: "gas", decimals: 2, unit: "m³" },
      { label: "Water (L/day)", colorVar: "--series-water", key: "water", decimals: 0, unit: "L" },
    ];
    const body = items
      .map((it) => {
        const s = stats[it.key];
        return (
          `<tr><td class="swatch-cell"><span class="swatch" style="background:${cssVar(it.colorVar)}"></span></td>` +
          `<td>${escHtml(it.label)}</td>` +
          `<td class="value-cell">${escHtml(fmtStat(s.avg_away, it.decimals, it.unit))}</td>` +
          `<td class="value-cell">${escHtml(fmtStat(s.avg_alone, it.decimals, it.unit))}</td>` +
          `<td class="value-cell">${escHtml(fmtStat(s.avg_occupied, it.decimals, it.unit))}</td>` +
          `<td class="value-cell">${escHtml(fmtStat(s.per_person_day, it.decimals, it.unit))}</td></tr>`
        );
      })
      .join("");
    el.innerHTML =
      `<p>${stats.covered_days} logged day${stats.covered_days === 1 ? "" : "s"} with data ` +
      `(${stats.away_days} away, ${stats.alone_days} alone, ${stats.occupied_days} with guests).</p>` +
      `<table class="sources-table hl-table"><thead><tr><th></th><th>Metric</th>` +
      `<th>Avg away</th><th>Avg alone</th><th>Avg with guests</th><th>Per person/day</th></tr></thead><tbody>${body}</tbody></table>`;
  }

  // Delegated on the table body (rows are re-rendered wholesale on every
  // load, so binding to individual buttons would leak listeners) -- the
  // only delete action in this app, occupancy_log's the only table with one.
  function initOccupancyDelete() {
    document.querySelector("#occupancy-table tbody").addEventListener("click", async (e) => {
      const btn = e.target.closest(".btn-delete");
      if (!btn) return;
      const statusEl = document.getElementById("occupancy-status");
      statusEl.textContent = "";
      try {
        const res = await fetch(`/api/settings/occupancy/${btn.dataset.id}`, {
          method: "DELETE",
          headers: writeHeaders(),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          statusEl.textContent = `Not deleted: ${body.error || `HTTP ${res.status}`}`;
          statusEl.className = "form-status form-status-error";
          return;
        }
        await loadOccupancyTable();
      } catch (err) {
        statusEl.textContent = `Not deleted: ${err}`;
        statusEl.className = "form-status form-status-error";
      }
    });
  }

  async function loadFiscalYearsSection() {
    // refreshFiscalYears() also keeps state.fiscalYears current for the "1y"
    // button in case it was just edited below.
    const fy = await refreshFiscalYears();
    for (const field of [
      "power_fy_start_month",
      "power_fy_start_day",
      "gas_fy_start_month",
      "gas_fy_start_day",
      "water_fy_start_month",
      "water_fy_start_day",
    ]) {
      const input = document.querySelector(`#form-fiscal-years [name="${field}"]`);
      if (input && fy[field] != null) input.value = fy[field];
    }
  }

  // Derived from whatever the endpoint returns, NOT a hardcoded list.
  //
  // REAL BUG, found live 2026-08-03: the list here had drifted from
  // _TOGGLE_FIELDS in app.py -- weather_enabled was saved by the server but
  // absent here, so after `e.target.reset()` (which clears every checkbox to
  // its raw HTML state) nothing re-ticked it and the setting looked like it
  // had failed to save. It hadn't; only the display was wrong.
  //
  // The dangerous half was quieter: this form is a full replace, so a
  // checkbox rendering unticked when the stored value is 1 means the next
  // save silently writes 0 and turns the feature off. Iterating the response
  // keeps the two ends in step by construction, so adding a toggle server-side
  // can never again leave a stale, silently-destructive checkbox here.
  async function loadTogglesSection() {
    const toggles = await fetchJson("/api/settings/toggles");
    for (const [field, value] of Object.entries(toggles)) {
      const input = document.querySelector(`#form-toggles [name="${field}"]`);
      if (input) input.checked = Boolean(value);
    }
  }

  // Hides a category's nav tab button when its device isn't
  // present -- Power has no toggle (every install has a P1 meter) so it's
  // never hidden. If the currently active tab was just hidden (its own
  // button, not one that was already hidden), falls back to Overview
  // rather than leaving the user stranded on a panel with no way back to
  // it via the nav.
  function applyTabVisibility() {
    const tabToKey = { gas: "gas", water: "water", battery: "battery", sufficiency: "sufficiency" };
    let activeTabHidden = false;
    for (const [tab, key] of Object.entries(tabToKey)) {
      const btn = document.querySelector(`nav.tabs button[data-tab="${tab}"]`);
      if (!btn) continue;
      const visible = state.visibility[key];
      btn.style.display = visible ? "" : "none";
      if (!visible && btn.classList.contains("active")) activeTabHidden = true;
    }
    if (activeTabHidden) {
      document.querySelector('nav.tabs button[data-tab="overview"]').click();
    }
  }

  async function loadVisibilitySection() {
    const v = await fetchJson("/api/settings/visibility");
    state.visibility = {
      gas: Boolean(v.show_gas_tab),
      water: Boolean(v.show_water_tab),
      battery: Boolean(v.show_battery_tab),
      sufficiency: Boolean(v.show_sufficiency_tab),
    };
    for (const field of ["show_gas_tab", "show_water_tab", "show_battery_tab", "show_sufficiency_tab"]) {
      const input = document.querySelector(`#form-visibility [name="${field}"]`);
      if (input) input.checked = Boolean(v[field]);
    }
    applyTabVisibility();
  }

  // Only used when the Settings tab is first opened (loadPanel dispatch
  // table below) -- nothing is "in progress" yet at that point, so loading
  // every section is correct and safe here, unlike inside a form's own
  // submit handler (see initSettingsForm).
  async function loadSettings() {
    await loadPvSection();
    await loadRateTable();
    await loadGasRateTable();
    await loadFiscalYearsSection();
    await loadTogglesSection();
    await loadVisibilitySection();
    await loadOccupancyTable();
  }

  // A failed save must say so — before this, all settings forms ignored the
  // response, so a 400/500 reset the form and looked saved. reloadFn is
  // scoped to just this form's own section (not the whole tab) so saving
  // one form can never clobber an unsaved edit sitting in another.
  function initSettingsForm(formId, url, statusId, reloadFn) {
    document.getElementById(formId).addEventListener("submit", async (e) => {
      e.preventDefault();
      const statusEl = document.getElementById(statusId);
      const form = new FormData(e.target);
      statusEl.textContent = "";
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: writeHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify(Object.fromEntries(form)),
        });
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          statusEl.textContent = `Not saved: ${body.error || `HTTP ${res.status}`}`;
          statusEl.className = "form-status form-status-error";
          return;
        }
        statusEl.textContent = "Saved.";
        statusEl.className = "form-status";
        e.target.reset();
        reloadFn();
      } catch (err) {
        statusEl.textContent = `Not saved: ${err}`;
        statusEl.className = "form-status form-status-error";
      }
    });
  }

  // An "ongoing" rate period has no end date. Disabling the
  // date input (rather than just clearing it) is what keeps it out of
  // FormData entirely, so the JSON POST omits period_end and app.py treats
  // that as open-ended -- see the OPEN_ENDED_SENTINEL comment there.
  function initOngoingCheckbox(formId, checkboxId, dateInputId) {
    const checkbox = document.getElementById(checkboxId);
    const dateInput = document.getElementById(dateInputId);
    const sync = () => {
      dateInput.disabled = checkbox.checked;
      dateInput.required = !checkbox.checked;
      if (checkbox.checked) dateInput.value = "";
    };
    checkbox.addEventListener("change", sync);
    // form.reset() resets the checkbox's checked state but not the disabled/
    // required attributes this handler set on dateInput, so without this
    // listener a submitted "ongoing" period leaves the date field stuck
    // disabled for the next entry.
    document.getElementById(formId).addEventListener("reset", () => setTimeout(sync, 0));
  }

  function initSettingsForms() {
    initSettingsForm("form-pv", "/api/settings/pv", "pv-status", loadPvSection);
    initOngoingCheckbox("form-rate", "rate-ongoing", "rate-period-end");
    initSettingsForm("form-rate", "/api/settings/rates", "rate-status", loadRateTable);
    initOngoingCheckbox("form-gas-rate", "gas-rate-ongoing", "gas-rate-period-end");
    initSettingsForm("form-gas-rate", "/api/settings/gas-rates", "gas-rate-status", loadGasRateTable);
    // Both are singleton-row configs (like form-pv above, not an append-a-row
    // form like form-rate) -- initSettingsForm's generic FormData->JSON
    // submit already does the right thing for checkboxes: a checked box
    // serializes as a truthy "on" string, an unchecked one is simply absent
    // from FormData, and app.py's `1 if data.get(f) else 0` treats missing
    // the same as unchecked.
    initSettingsForm("form-fiscal-years", "/api/settings/fiscal-years", "fiscal-years-status", loadFiscalYearsSection);
    initSettingsForm("form-toggles", "/api/settings/toggles", "toggles-status", loadTogglesSection);
    initSettingsForm("form-visibility", "/api/settings/visibility", "visibility-status", loadVisibilitySection);
    initSettingsForm("form-occupancy", "/api/settings/occupancy", "occupancy-status", loadOccupancyTable);
    initOccupancyDelete();
  }

  // ---- Import ----
  async function loadImport() {
    const rows = await fetchJson("/api/ingest-status");
    const tbody = document.querySelector("#import-history-table tbody");
    tbody.innerHTML = rows
      .slice(0, 10)
      .map(
        (r) =>
          `<tr><td>${escHtml(r.filename)}</td><td>${escHtml(r.category)}</td>` +
          `<td>${escHtml(r.row_count)}</td><td>${escHtml(fmtDateTime(r.ingested_at))}</td></tr>`
      )
      .join("");
  }

  // Data freshness moved off the Import tab with the rest of the diagnostics
  // -- Import is now purely about getting data in. Freshness still
  // auto-loads on tab open (it's a single cheap MAX(date) query per
  // category); the integrity scan below it does not, being a full-history
  // pass.
  async function loadDataHealth() {
    const freshness = await fetchJson("/api/data-freshness");
    const fmtFreshness = (d) => d || "No data yet";
    renderSourcesTable("data-freshness", [
      { label: "Power", value: fmtFreshness(freshness.power), colorVar: "--series-power-import" },
      { label: "Gas", value: fmtFreshness(freshness.gas), colorVar: "--series-gas" },
      { label: "Water", value: fmtFreshness(freshness.water), colorVar: "--series-water" },
      { label: "Battery", value: fmtFreshness(freshness.battery), colorVar: "--series-battery-charge" },
      { label: "Costs Power (rate coverage)", value: fmtFreshness(fmtPeriodEnd(freshness.costs_power)), colorVar: "--series-power-import" },
      { label: "Costs Gas (rate coverage)", value: fmtFreshness(fmtPeriodEnd(freshness.costs_gas)), colorVar: "--series-gas" },
    ]);
  }

  function initImportForms() {
    document.getElementById("form-import-csv").addEventListener("submit", async (e) => {
      e.preventDefault();
      const resultEl = document.getElementById("import-csv-result");
      const form = new FormData(e.target);
      resultEl.innerHTML = `<p>Uploading&hellip;</p>`;
      try {
        const res = await fetch("/api/import/csv", { method: "POST", headers: writeHeaders(), body: form });
        const body = await res.json();
        if (!res.ok) {
          resultEl.innerHTML = `<p class="empty-state">${escHtml(body.error || "Upload failed.")}</p>`;
          return;
        }
        resultEl.innerHTML = `<p>${escHtml(body.filename)} (${escHtml(body.category)}): ${escHtml(body.rows_ingested)} rows ingested.</p>`;
        e.target.reset();
        loadImport();
      } catch (err) {
        resultEl.innerHTML = `<p class="empty-state">Upload failed: ${escHtml(err)}</p>`;
      }
    });

    document.getElementById("form-import-pdf").addEventListener("submit", async (e) => {
      e.preventDefault();
      const resultEl = document.getElementById("import-pdf-result");
      const form = new FormData(e.target);
      resultEl.innerHTML = `<p>Uploading &amp; parsing&hellip;</p>`;
      try {
        const res = await fetch("/api/import/tariff-pdf", { method: "POST", headers: writeHeaders(), body: form });
        const body = await res.json();
        if (!res.ok) {
          resultEl.innerHTML = `<p class="empty-state">${escHtml(body.error || "Upload failed.")}</p>`;
          return;
        }
        const skipNote = body.skipped_overlaps
          ? ` ${escHtml(body.skipped_overlaps)} period(s) skipped — they overlap existing entries. If the PDF should win, give the clashing rate an end date (or remove it) under Settings → Rates, then re-import. Note a rate with no end date runs indefinitely, so it clashes with anything starting after it.`
          : "";
        resultEl.innerHTML =
          `<p>${escHtml(body.filename)} recognized as <strong>${escHtml(body.parser)}</strong>: ` +
          `${escHtml(body.power_periods)} power period(s), ` +
          `${escHtml(body.gas_periods)} gas period(s) processed.${skipNote}</p>`;
        e.target.reset();
      } catch (err) {
        resultEl.innerHTML = `<p class="empty-state">Upload failed: ${escHtml(err)}</p>`;
      }
    });

    document.getElementById("form-import-tariff-csv").addEventListener("submit", async (e) => {
      e.preventDefault();
      const resultEl = document.getElementById("import-tariff-csv-result");
      const form = new FormData(e.target);
      resultEl.innerHTML = `<p>Uploading &amp; parsing&hellip;</p>`;
      try {
        const res = await fetch("/api/import/tariff-csv", { method: "POST", headers: writeHeaders(), body: form });
        const body = await res.json();
        if (!res.ok) {
          resultEl.innerHTML = `<p class="empty-state">${escHtml(body.error || "Upload failed.")}</p>`;
          return;
        }
        const skipNote = body.skipped_overlaps
          ? ` ${escHtml(body.skipped_overlaps)} period(s) skipped — they overlap existing entries. If the CSV should win, give the clashing rate an end date (or remove it) under Settings → Rates, then re-import. Note a rate with no end date runs indefinitely, so it clashes with anything starting after it.`
          : "";
        resultEl.innerHTML =
          `<p>${escHtml(body.filename)}: ${escHtml(body.power_periods)} power period(s), ` +
          `${escHtml(body.gas_periods)} gas period(s) processed.${skipNote}</p>`;
        e.target.reset();
      } catch (err) {
        resultEl.innerHTML = `<p class="empty-state">Upload failed: ${escHtml(err)}</p>`;
      }
    });

  }

  // Shared by both diagnostic cards: collapse a category's findings into one
  // summary line. Acknowledged findings are counted separately rather than
  // silently dropped, so "N acknowledged" is always an honest total.
  function summariseFindings(allFindings, emptyText, partsFor) {
    const unacked = allFindings.filter((f) => !f.acknowledged);
    const ackedCount = allFindings.length - unacked.length;
    if (allFindings.length === 0) return { text: emptyText, warn: false };
    if (unacked.length === 0) return { text: `All ${ackedCount} acknowledged`, warn: false };
    const suffix = ackedCount > 0 ? ` (+${ackedCount} acknowledged)` : "";
    return { text: partsFor(unacked).join("; ") + suffix, warn: true };
  }

  // ---- Data health tab: gaps + integrity findings ----
  //
  // Outlier days used to be summarised here too. They now have their own
  // card on the Comparison tab (initConsumptionNotes below): a day of
  // near-zero water because nobody was home is correct data, and counting it
  // as a health finding buried the real instrument faults -- 25 genuine
  // findings under 689 on the real database.
  function initDataHealthCard() {
    // Last-fetched reports, kept so the "Show details" toggles can render
    // without a re-fetch, and so acknowledge/delete actions (which re-click
    // the button to refresh) can re-open a detail panel the user already had
    // expanded rather than silently collapsing it.
    let lastHealth = null;
    let lastQuality = null;
    let healthDetailsOpen = false;
    let qualityDetailsOpen = false;

    function renderHealthDetail() {
      renderFindingsDetail("data-health-detail", flattenGaps(lastHealth), (f) => ({
        label: "Fix",
        onClick: () => fixGapHint(f),
      }));
    }

    function renderQualityDetail() {
      renderFindingsDetail("data-quality-detail", flattenQuality(lastQuality), (f) =>
        f.issue_type === "negative_delta" ? { label: "Delete reading", className: "delete-reading", onClick: () => deleteReading(f) } : null
      );
    }

    document.getElementById("toggle-health-details").addEventListener("click", () => {
      healthDetailsOpen = !healthDetailsOpen;
      const btn = document.getElementById("toggle-health-details");
      if (healthDetailsOpen) {
        renderHealthDetail();
        btn.textContent = "Hide details";
      } else {
        document.getElementById("data-health-detail").innerHTML = "";
        btn.textContent = "Show details";
      }
    });

    document.getElementById("toggle-quality-details").addEventListener("click", () => {
      qualityDetailsOpen = !qualityDetailsOpen;
      const btn = document.getElementById("toggle-quality-details");
      if (qualityDetailsOpen) {
        renderQualityDetail();
        btn.textContent = "Hide details";
      } else {
        document.getElementById("data-quality-detail").innerHTML = "";
        btn.textContent = "Show details";
      }
    });

    // Deliberately not auto-run on tab load like Data freshness above -- a
    // full-history scan is a heavier, on-demand diagnostic. One click runs
    // both the gap check and the integrity check, since both answer the same
    // question ("can I trust this data?").
    document.getElementById("check-data-health").addEventListener("click", async () => {
      const btn = document.getElementById("check-data-health");
      const resultEl = document.getElementById("data-health-result");
      const qualityEl = document.getElementById("data-quality-result");
      btn.disabled = true;
      resultEl.innerHTML = `<p>Checking&hellip;</p>`;
      qualityEl.innerHTML = "";
      try {
        const [health, quality] = await Promise.all([fetchJson("/api/data-health"), fetchJson("/api/data-quality")]);
        lastHealth = health;
        lastQuality = quality;

        const gapsInfo = (entry) => {
          if (!entry.first_date) return { text: "No data yet", warn: false };
          if (entry.gaps.length === 0) return { text: "No gaps ✓", warn: false };
          const unacked = entry.gaps.filter((g) => !g.acknowledged);
          const ackedCount = entry.gaps.length - unacked.length;
          if (unacked.length === 0) return { text: `All ${ackedCount} acknowledged`, warn: false };
          const text = unacked.map((g) => (g.start === g.end ? g.start : `${g.start} to ${g.end}`)).join(", ");
          const suffix = ackedCount > 0 ? ` (+${ackedCount} acknowledged)` : "";
          return { text: text + suffix, warn: true };
        };
        const power = gapsInfo(health.power);
        const gas = gapsInfo(health.gas);
        const water = gapsInfo(health.water);
        const battery = gapsInfo(health.battery);
        renderSourcesTable("data-health-result", [
          { label: "Power", value: power.text, colorVar: "--series-power-import", warn: power.warn },
          { label: "Gas", value: gas.text, colorVar: "--series-gas", warn: gas.warn },
          { label: "Water", value: water.text, colorVar: "--series-water", warn: water.warn },
          { label: "Battery", value: battery.text, colorVar: "--series-battery-charge", warn: battery.warn },
        ]);
        document.getElementById("toggle-health-details").hidden = Object.values(health).every((h) => h.gaps.length === 0);

        const qualityInfo = (entry) =>
          summariseFindings(
            [
              ...entry.negative_deltas.items,
              ...entry.glitch_episodes.items,
              ...entry.granularity_disagreements,
              ...entry.implausible_values,
              ...entry.empty_runs,
            ],
            "No integrity issues ✓",
            () => {
              const parts = [];
              if (entry.negative_deltas.items.some((n) => !n.acknowledged)) parts.push("negative deltas");
              if (entry.glitch_episodes.items.some((g) => !g.acknowledged)) parts.push("glitch episodes");
              if (entry.granularity_disagreements.some((d) => !d.acknowledged)) parts.push("source disagreements");
              if (entry.implausible_values.some((v) => !v.acknowledged)) parts.push("implausible values");
              if (entry.empty_runs.some((r) => !r.acknowledged)) parts.push("never-recorded period");
              return parts;
            }
          );
        const qPower = qualityInfo(quality.power);
        const qGas = qualityInfo(quality.gas);
        const qWater = qualityInfo(quality.water);
        const qBattery = qualityInfo(quality.battery);
        renderSourcesTable("data-quality-result", [
          { label: "Power", value: qPower.text, colorVar: "--series-power-import", warn: qPower.warn },
          { label: "Gas", value: qGas.text, colorVar: "--series-gas", warn: qGas.warn },
          { label: "Water", value: qWater.text, colorVar: "--series-water", warn: qWater.warn },
          { label: "Battery", value: qBattery.text, colorVar: "--series-battery-charge", warn: qBattery.warn },
        ]);
        document.getElementById("toggle-quality-details").hidden = flattenQuality(quality).length === 0;

        if (healthDetailsOpen) renderHealthDetail();
        if (qualityDetailsOpen) renderQualityDetail();
      } catch (err) {
        resultEl.innerHTML = `<p class="empty-state">Check failed: ${escHtml(err)}</p>`;
      } finally {
        btn.disabled = false;
      }
    });
  }

  // ---- Data health tab: totals reconciliation ----
  //
  // Unlike the other two cards this one reports a proof, not a suspicion, so
  // its summary line always shows the verified count alongside any problems:
  // "1,647 verified" is the reassuring case, and "6 unverifiable" must stay
  // visible next to it rather than being rounded up into a pass.
  function initReconciliation() {
    let lastRec = null;
    let detailsOpen = false;

    function renderDetail() {
      renderFindingsDetail("reconciliation-detail", flattenReconciliation(lastRec), null);
    }

    document.getElementById("toggle-reconciliation-details").addEventListener("click", () => {
      detailsOpen = !detailsOpen;
      const btn = document.getElementById("toggle-reconciliation-details");
      if (detailsOpen) {
        renderDetail();
        btn.textContent = "Hide details";
      } else {
        document.getElementById("reconciliation-detail").innerHTML = "";
        btn.textContent = "Show details";
      }
    });

    document.getElementById("check-reconciliation").addEventListener("click", async () => {
      const btn = document.getElementById("check-reconciliation");
      const resultEl = document.getElementById("reconciliation-result");
      btn.disabled = true;
      resultEl.innerHTML = `<p>Checking&hellip;</p>`;
      try {
        const rec = await fetchJson("/api/reconciliation");
        lastRec = rec;

        const info = (entry) => {
          const unacked = entry.mismatches.filter((m) => !m.acknowledged);
          const parts = [`${entry.verified.toLocaleString()} verified`];
          if (unacked.length) parts.push(`${unacked.length} mismatch${unacked.length === 1 ? "" : "es"}`);
          const ackedMismatches = entry.mismatches.length - unacked.length;
          if (ackedMismatches) parts.push(`${ackedMismatches} acknowledged`);
          if (entry.unverifiable.length) parts.push(`${entry.unverifiable.length} unverifiable`);
          return { text: parts.join(", "), warn: unacked.length > 0 };
        };
        const rPower = info(rec.power);
        const rGas = info(rec.gas);
        const rWater = info(rec.water);
        const rBattery = info(rec.battery);
        renderSourcesTable("reconciliation-result", [
          { label: "Power", value: rPower.text, colorVar: "--series-power-import", warn: rPower.warn },
          { label: "Gas", value: rGas.text, colorVar: "--series-gas", warn: rGas.warn },
          { label: "Water", value: rWater.text, colorVar: "--series-water", warn: rWater.warn },
          { label: "Battery", value: rBattery.text, colorVar: "--series-battery-charge", warn: rBattery.warn },
        ]);
        document.getElementById("toggle-reconciliation-details").hidden = flattenReconciliation(rec).length === 0;

        if (detailsOpen) renderDetail();
      } catch (err) {
        resultEl.innerHTML = `<p class="empty-state">Check failed: ${escHtml(err)}</p>`;
      } finally {
        btn.disabled = false;
      }
    });
  }

  // ---- Comparison tab: consumption notes ----
  //
  // Same acknowledge mechanism as the integrity card, but deliberately no
  // Delete affordance: every finding here describes correctly-recorded
  // usage, so there is nothing to repair, and offering a delete would invite
  // destroying accurate readings because a day looked unusual.
  function initConsumptionNotes() {
    let lastNotes = null;
    let detailsOpen = false;

    function renderDetail() {
      renderFindingsDetail("consumption-notes-detail", flattenConsumptionNotes(lastNotes), null, true);
    }

    document.getElementById("toggle-consumption-details").addEventListener("click", () => {
      detailsOpen = !detailsOpen;
      const btn = document.getElementById("toggle-consumption-details");
      if (detailsOpen) {
        renderDetail();
        btn.textContent = "Hide details";
      } else {
        document.getElementById("consumption-notes-detail").innerHTML = "";
        btn.textContent = "Show details";
      }
    });

    document.getElementById("check-consumption-notes").addEventListener("click", async () => {
      const btn = document.getElementById("check-consumption-notes");
      const resultEl = document.getElementById("consumption-notes-result");
      btn.disabled = true;
      resultEl.innerHTML = `<p>Checking&hellip;</p>`;
      try {
        // Scoped to the selected range like every other view. The
        // range controls and drag-to-zoom are therefore how you narrow these
        // notes -- no separate concept to learn.
        const { from, to } = rangeParams();
        const notes = await fetchJson(`/api/consumption-notes?from=${from}&to=${to}`);
        lastNotes = notes;

        // State the range the result actually covers. Without this, changing
        // the range and not re-running leaves a count on screen that silently
        // refers to a different period.
        const rangeEl = document.getElementById("consumption-notes-range");
        rangeEl.textContent = `Covering ${from} → ${to}`;
        rangeEl.hidden = false;

        const info = (entry) =>
          summariseFindings(entry.outlier_days, "Nothing unusual ✓", (unacked) => {
            const high = unacked.filter((o) => o.direction === "high").length;
            const low = unacked.filter((o) => o.direction === "low").length;
            const parts = [];
            if (high) parts.push(`${high} unusually high`);
            if (low) parts.push(`${low} unusually low`);
            return parts;
          });
        // The per-utility summary stays per-category -- "which meter is this
        // about" is exactly the question it answers. Only the detail list
        // below groups into events. Battery is absent entirely now.
        const COLOR_VARS = {
          power: "--series-power-import",
          gas: "--series-gas",
          water: "--series-water",
          battery: "--series-battery-charge",
        };
        renderSourcesTable(
          "consumption-notes-result",
          Object.keys(notes.categories).map((cat) => {
            const n = info(notes.categories[cat]);
            return { label: CATEGORY_LABELS[cat], value: n.text, colorVar: COLOR_VARS[cat], warn: n.warn };
          })
        );
        document.getElementById("toggle-consumption-details").hidden = flattenConsumptionNotes(notes).length === 0;

        if (detailsOpen) renderDetail();
      } catch (err) {
        resultEl.innerHTML = `<p class="empty-state">Check failed: ${escHtml(err)}</p>`;
      } finally {
        btn.disabled = false;
      }
    });
  }

  // ---- Contextual help: "i" icons (Import/Settings only, first pass) +
  // the Help tab, both rendered from this single source so the two never
  // drift out of sync with each other. ----
  const HELP_TOPICS = {
    // Listed first: these apply to every chart on every tab, unlike the
    // rest which are tied to one card.
    "charts-range": {
      section: "Charts",
      title: "Choosing and zooming a period",
      text: "Every chart tab shares one date range, shown in the controls above the charts. Pick a preset (7d/30d/90d/1y), or type exact From/To dates and press Zoom. The fastest way is to drag straight across a chart: release, and the range narrows to the span you dragged over. Because that sets the real range rather than a chart-only zoom, everything stays in step — the From/To boxes fill in, the preset deselects, every chart on the tab redraws to match, and the < and > buttons then step backwards and forwards by exactly the span you selected, which makes walking week by week through a period easy. To go back out, click any preset. A plain click does nothing, so hovering for tooltips still works normally. Dragging works on touchscreens too, including in the Android app.",
    },
    "charts-toggles": {
      section: "Charts",
      title: "Showing and hiding series",
      text: "The pills under each chart switch individual series on and off — Import, Export, Charge, Discharge, and so on. Anything drawn on a chart gets a pill, including the Sunshine rail, so you can compare just the two things you care about: Import against Export, or Import against sunshine alone. Switching a series off only hides it; nothing is recalculated and no data is lost, so switching it back on restores exactly what was there.",
    },
    "overview-energy-flow": {
      section: "Overview",
      title: "Energy flow: sources → uses",
      text: "A proportional view alongside the simpler diagram above it: ribbon width is scaled to kWh, so it shows how much of each source went where, not just the totals. Solar is an estimate (see Self-Sufficiency), and OmniMeter only measures each side's totals, not which kWh went to which use — so ribbons follow a documented assumption (solar and battery cover the house first, then the battery, then export; grid import covers whatever's left) rather than a direct measurement. When a period has days that mix grid import and export, or battery charge and discharge, in ways that assumption can't fully separate, a note below the diagram says how much and why.",
    },
    "import-csv": {
      section: "Import",
      title: "Meter export files (CSV)",
      text: "The filename prefix routes each file automatically (P1e- power, P1g- gas, Water-, Bat-) — renaming a file before upload breaks that routing. Check Recent imports below to confirm a file actually landed before re-uploading it.",
    },
    "import-pdf": {
      section: "Import",
      title: "Tariff rate document (PDF)",
      text: "Nine Dutch supplier formats are recognized today. Downloadable PDFs from the supplier's own website — Vattenfall's \"Tarievenspecificatie\" (personal, closed periods) and public \"Tarievenblad\", Greenchoice's, Pure Energie's, Innova Energie's, Mega Energie's, and Clean Energy's Modelcontract tariff sheets. Live webpages, saved as PDF by you rather than fetched by this app: Eneco's Modelcontract page (no extra steps — just Print → Save as PDF) and Budget Thuis's Modelcontract page (accept its cookie banner and click open \"Tarievenblad Modelcontract voor onbepaalde tijd met variabele tarieven\" first, or printing captures an empty/garbled page instead of the rate table). All nine are open-ended except the Vattenfall Tarievenspecificatie — a later upload of the same supplier's next sheet automatically supersedes the current one. Eneco and Budget Thuis don't state an effective date on the page itself, so their imported period starts on the day you upload it, not a date the supplier claims — re-uploading an old saved copy later would misdate it, so use a fresh save; every other supplier's document states its own date. A successful parse adds or updates rows directly in the Electricity/Gas rate schedule tables in Settings — nothing else changes. Any other document is rejected outright rather than partially misread; use the CSV template or manual rate entry under Settings → Rates instead.",
    },
    "import-csv": {
      section: "Import",
      title: "Tariff rate CSV",
      text: "The generic fallback for any supplier without a recognized PDF format. Columns: category (power/gas), period_start (YYYY-MM-DD), period_end (YYYY-MM-DD, or blank for an ongoing/open-ended rate), rate (EUR per kWh or per m³ — not cents). The whole file is rejected if any row is malformed, with the line number, rather than silently skipping bad rows.",
    },
    "health-freshness": {
      section: "Data health",
      title: "Data freshness",
      text: "Shows the most recent date each category actually has data for — useful for spotting a data source that's quietly stopped (a CSV export that's no longer being uploaded, or a device that's dropped off the local API). Costs shows the latest rate period's end date instead, since that's the real boundary past which cost figures fall back to a carried-forward stale rate.",
    },
    "health-integrity": {
      section: "Data health",
      title: "Data integrity",
      text: "An on-demand scan for signs the stored data itself is wrong. Missing days: a gap in the middle of otherwise-continuous data, usually an ingest path failing silently for a while. Integrity findings: meter resets or bad rows (negative deltas), corrected sensor glitches, two sources disagreeing about the same day, and physically impossible values such as a battery charge outside 0–100%. Everything here points at an instrument or ingest fault, so each finding is worth explaining. Click \"Show details\" for a table of individual findings, filterable by category, type, acknowledged status and date range — the line above the table always states how many of the total are currently shown, so a filtered view is never mistaken for the whole picture. Acknowledge marks a finding as reviewed so it stops standing out (never edits any data); a gap's Fix button switches to the Import tab and highlights the CSV upload with the missing range noted; a negative-delta finding also offers Delete reading, which clears just that one bad value — destructive, confirmed before it runs, and only reversible by re-importing source data. Days where your consumption merely looked unusual are not faults and are reported separately under Consumption notes on the Comparison tab. Not run automatically; use it after changing device config, or if something looks off.",
    },
    "health-reconciliation": {
      section: "Data health",
      title: "Totals reconciliation",
      text: "Every other check on this page hunts for something that looks odd. This one proves the arithmetic instead. On a cumulative meter a day's usage is just the closing reading minus the opening one, so each stored daily total is re-derived that way — a completely different route to the same number — and the two are compared. A bug in the summation cannot hide in both, which is exactly the class of fault that once made power import read double for five days without any other check noticing. \"Verified\" means the two agree. \"Unverifiable\" means the day contains a meter reset or a gap longer than 26 hours, where the rollup deliberately drops an interval and the two are not expected to match — those are listed rather than counted as passes, so a clean result never hides its own exemptions. Today is skipped because it is still in progress and the rollup is always slightly behind the live meter. A mismatch is worth investigating: it means a stored total disagrees with the meter itself.",
    },
    "comparison-notes": {
      section: "Comparison",
      title: "Consumption notes",
      text: "Stretches whose usage sat far outside that category's own recent baseline, in either direction. This is not a fault report — the data is correct in every case it lists. A near-zero water or gas day usually means nobody was home, or that the day simply broke routine (being ill and staying in bed will do it); a high day might be EV charging, guests staying, or a cold snap. Consecutive days are grouped into a single note showing the date range, how many days it ran, and its most extreme day, so a fortnight away appears once rather than fourteen times. Only the selected date range is listed — it's the same range the charts use, so the presets on this card, or dragging across a chart on another tab, narrow this too. Where you've logged a headcount under Occupancy in Settings it's shown alongside as a hint, but only when every day of the stretch agreed — bear in mind the log covers part of the history, and a quiet day spent at home looks like normal occupancy. Click \"Show details\" for a table filterable by category, acknowledged status and date, sortable newest- or oldest-first. The line above the table always states how many of the total are currently shown. Acknowledge marks a note as reviewed; \"Acknowledge all N shown\" does the same for exactly what the current filters and range show, and can be undone the same way. Nothing here can be deleted, because there is nothing wrong to repair. Not run automatically.",
    },
    "settings-pv": {
      section: "Settings",
      title: "PV system",
      text: "Rated capacity (kWp) is the only field that actually feeds anything — it drives the Self-Sufficiency tab's estimated production model (nameplate capacity × a seasonal yield curve, reconciled against your real grid export). Installed date and Notes are for your own reference only; nothing reads them back.",
    },
    "settings-rate-power": {
      section: "Settings",
      title: "Electricity rate schedule",
      text: "Buy is what you pay per kWh imported from the grid; Sell is what you're paid per kWh exported. With net metering (salderen) these are typically the same figure — that's what the badge above is noting. Source is a free-text note (e.g. which letter/document a period came from) purely for your own bookkeeping.",
    },
    "settings-rate-gas": {
      section: "Settings",
      title: "Gas rate schedule",
      text: "Same period-based model as the electricity schedule, one price per m³, no buy/sell distinction since gas isn't exported. Source is a free-text note for your own reference.",
    },
    "settings-fiscal-year": {
      section: "Settings",
      title: "Fiscal year start (\"1y\" button)",
      text: "Only changes what date range each tab's \"1y\" quick-range button resolves to — it doesn't affect any stored data. Set it to match your actual utility's billing-year start (e.g. month=5 day=1) if you want \"1y\" to line up with your own bill instead of the Dutch-default 1 May / calendar-year split.",
    },
    "settings-toggles": {
      section: "Settings",
      title: "Feature toggles",
      text: "Each toggle independently disables one data path without deleting anything already stored: Local device API pauses live polling; the three CSV import toggles block that file type's uploads on the Import tab; Tariff import (PDF/CSV) blocks both rate-schedule uploads on the Import tab; Nightly DB backup pauses the scheduled backup (useful while testing, so you're not filling your backups directory with throwaway snapshots). Weather data and Check GitHub for a newer version are OmniMeter's only two features that contact the internet at all, so both default off — turning either on is a deliberate opt-in, not just a data-path pause.",
    },
    "settings-visibility": {
      section: "Settings",
      title: "Dashboard visibility",
      text: "Purely cosmetic — hides a tab and its Overview tiles for a category you don't have a device for. Turning a tab back on doesn't re-fetch or lose anything; the underlying data (if any) was never touched.",
    },
    "settings-occupancy": {
      section: "Settings",
      title: "Occupancy",
      text: "A log of how many people were home over a date range, used to see whether usage patterns change with occupancy (e.g. guests staying over). Entries are the full headcount for their dates, not additions to each other, so overlapping date ranges aren't allowed — log a new entry each time the total changes instead.",
    },
  };

  function renderHelpTab() {
    const el = document.getElementById("help-body");
    const sections = {};
    for (const topic of Object.values(HELP_TOPICS)) {
      (sections[topic.section] ||= []).push(topic);
    }
    el.innerHTML = Object.entries(sections)
      .map(
        ([section, topics]) =>
          `<p class="help-section-title">${escHtml(section)}</p>` +
          topics
            .map(
              (t) =>
                `<div class="help-topic"><p class="help-topic-title">${escHtml(t.title)}</p>` +
                `<p class="help-topic-text">${escHtml(t.text)}</p></div>`
            )
            .join("")
      )
      .join("");
  }

  function initHelpIcons() {
    const popover = document.getElementById("help-popover");

    function closePopover() {
      popover.classList.remove("is-open");
      document.querySelectorAll(".info-icon.is-open").forEach((b) => b.classList.remove("is-open"));
    }

    document.querySelectorAll(".info-icon[data-help]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const wasOpen = btn.classList.contains("is-open");
        closePopover();
        if (wasOpen) return;
        const topic = HELP_TOPICS[btn.dataset.help];
        if (!topic) return;
        popover.innerHTML =
          `<p class="help-popover-title">${escHtml(topic.title)}</p>` + `<p class="help-popover-text">${escHtml(topic.text)}</p>`;
        // Positioned from the icon's own rect (fixed positioning, so no
        // reflow/scroll accounting needed) -- flips left of the icon near
        // the right edge of the viewport rather than overflowing off-screen.
        const rect = btn.getBoundingClientRect();
        popover.classList.add("is-open");
        const popRect = popover.getBoundingClientRect();
        const overflowsRight = rect.left + popRect.width > window.innerWidth - 12;
        popover.style.top = `${rect.bottom + 6}px`;
        popover.style.left = overflowsRight ? `${Math.max(12, rect.right - popRect.width)}px` : `${rect.left}px`;
        btn.classList.add("is-open");
      });
    });

    document.addEventListener("click", (e) => {
      if (!popover.contains(e.target)) closePopover();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closePopover();
    });
  }

  function loadPanel(tab) {
    const loaders = {
      overview: loadOverview,
      power: loadPower,
      gas: loadGas,
      water: loadWater,
      battery: loadBattery,
      sufficiency: loadSufficiency,
      costs: loadCosts,
      comparison: loadComparison,
      import: loadImport,
      datahealth: loadDataHealth,
      settings: loadSettings,
    };
    const fn = loaders[tab];
    if (fn) fn().catch((err) => console.error(`failed to load ${tab}`, err));
  }

  document.addEventListener("DOMContentLoaded", async () => {
    initThemeControls();
    initTabs();
    initSubTabs();
    // Awaited before initRangeControls() so a "1y" click is never resolved
    // against the hardcoded fallback in state.fiscalYears -- the fetch is a
    // same-host SQLite-backed call, effectively instant in practice.
    await refreshFiscalYears().catch((err) => console.error("failed to load fiscal-year config", err));
    // Awaited before loadPanel() for the same reason as fiscal years above --
    // nav tabs must be hidden/shown correctly before the first render, not
    // flash visible-then-hidden a moment later.
    await loadVisibilitySection().catch((err) => console.error("failed to load visibility settings", err));
    initRangeControls();
    initSettingsForms();
    initImportForms();
    initDataHealthCard();
    initReconciliation();
    initConsumptionNotes();
    initComparisonControls();
    renderHelpTab();
    initHelpIcons();
    loadPanel("overview");
    // Keeps the "X min ago" text counting up between overview fetches;
    // the absolute timestamp alongside it doesn't need re-rendering.
    setInterval(renderLastRefreshed, 60000);
    checkForUpdate();
  });
})();
