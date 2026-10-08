"use strict";

const MODE_LABELS = {
  quiet: "Quiet", standard: "Standard", high: "High Speed", full: "Full Speed",
  manual: "Fixed", curve: "Custom Curve", mb_sync: "MB PWM Sync", bios: "BIOS control",
};
const T_MIN = 20, T_MAX = 100;
const $ = (id) => document.getElementById(id);

let cfg = null, meta = null, state = null;
let sel = { kind: "hub", i: 2 }, lightPort = 0;  // lightPort 4 = all ports
let saveTimer = null, dragging = false, editSeq = 0;

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "PUT", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-UniHub": "1" },
  };
  const r = await fetch(path, opts);
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 5000);
}

// Coalesce rapid edits (sliders, dragging) into one PUT.
// Only adopt the server's (validated) copy if nothing was edited meanwhile,
// so a slow response can't roll back a newer local change.
function save(delay = 250) {
  const seq = ++editSeq;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const saved = await api("/api/config", cfg);
      if (seq === editSeq) cfg = saved;
    } catch (e) {
      toast("Not saved: " + e.message);
      if (seq === editSeq) { cfg = await api("/api/config"); renderAll(); }
    }
  }, delay);
}

function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const k of kids) e.append(k);
  return e;
}

// ---------------- navigation ----------------
document.querySelectorAll(".nav-item").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".nav-item").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("hidden", v.id !== "view-" + b.dataset.view));
}));

// ---------------- fan speed ----------------
// The selected card is a hub port (cfg.hub.groups[i]) or a motherboard header
// (cfg.motherboard.headers entry, created on first selection, BIOS by default).
function mbEntry(index) {
  let h = cfg.motherboard.headers.find((x) => x.index === index);
  if (!h) {
    h = { index, name: `Header ${index}`, mode: "bios", manual_percent: 50, sensor: "cpu", curve: meta.presets.standard.map((p) => [...p]) };
    cfg.motherboard.headers.push(h);
  }
  return h;
}
const selCfg = () => (sel.kind === "hub" ? cfg.hub.groups[sel.i] : mbEntry(sel.i));
const selState = () => (sel.kind === "hub" ? state?.hub.groups[sel.i] : state?.motherboard.headers.find((h) => h.index === sel.i));

// Hub ports with 0 fans are hidden unless Settings says otherwise.
const visiblePorts = () => cfg.hub.groups.map((g, i) => [g, i]).filter(([g]) => g.fans > 0 || cfg.ui.show_empty_ports);
function ensureVisibleSelection() {
  const vis = visiblePorts().map(([, i]) => i);
  if (sel.kind === "hub" && !vis.includes(sel.i) && vis.length) sel = { kind: "hub", i: vis[0] };
  if (lightPort !== 4 && !vis.includes(lightPort) && vis.length) lightPort = vis[0];
  if (!vis.includes(lab.group) && vis.length) lab.group = vis[0];
}

function portCard(g, s, selected, onclick, sub) {
  const pct = s.percent;
  const card = el("button", { class: "port" + (selected ? " sel" : ""), onclick },
    el("div", { class: "pname" }, g.name),
    el("div", { class: "pmode" }, MODE_LABELS[g.mode] + sub),
    el("div", { class: "rpm" }, String(s.rpm ?? 0), el("small", {}, " RPM")),
    el("div", { class: "meta" },
      el("span", {}, pct == null ? (g.mode === "bios" ? "BIOS" : "MB") : pct + "%"),
      el("span", {}, s.temp == null ? "—" : s.temp.toFixed(1) + " °C")),
    el("div", { class: "bar" }, el("i")),
    ...(s.failsafe ? [el("span", { class: "fs" }, "NO TEMP")]
      : s.missing?.length ? [el("span", { class: "fs warn", title: "Unreadable: " + s.missing.join(", ") }, s.missing.join("+").toUpperCase() + " MISSING")] : []),
  );
  card.querySelector(".bar i").style.width = (pct ?? 0) + "%";
  return card;
}

function renderMbPorts() {
  const m = state?.motherboard;
  $("mb-section").classList.toggle("hidden", !m?.present);
  if (!m?.present) return;
  $("mb-chip").textContent = m.chip;
  const warn = m.error || (!m.writable ? "Read-only: install the hwmon udev rule (README) to control these headers." : "");
  $("mb-banner").textContent = warn;
  $("mb-banner").classList.toggle("hidden", !warn);
  const shown = m.headers.filter((h) => (h.rpm ?? 0) > 0 || cfg.motherboard.headers.some((c) => c.index === h.index && c.mode !== "bios"));
  $("mb-ports").replaceChildren(...shown.map((h) => {
    const c = cfg.motherboard.headers.find((x) => x.index === h.index) || { name: `Header ${h.index}`, mode: "bios" };
    return portCard(c, h, sel.kind === "mb" && sel.i === h.index, () => { sel = { kind: "mb", i: h.index }; renderFans(); }, ` · header ${h.index}`);
  }));
}

function renderPorts() {
  const box = $("ports");
  box.replaceChildren(...visiblePorts().map(([g, i]) => portCard(g, state?.hub.groups[i] || {}, sel.kind === "hub" && sel.i === i,
    () => { sel = { kind: "hub", i }; renderFans(); }, " · " + g.fans + " fan" + (g.fans === 1 ? "" : "s"))));
  renderMbPorts();
}

function renderFans() {
  ensureVisibleSelection();
  renderPorts();
  const g = selCfg();
  const isMb = sel.kind === "mb";
  $("port-name").value = g.name;
  $("port-fans-label").classList.toggle("hidden", isMb);
  $("mb-hint").classList.toggle("hidden", !isMb);
  // No bulk apply for motherboard headers: one of them may be a pump.
  $("apply-all").classList.toggle("hidden", isMb);

  const sens = $("port-sensor");
  sens.replaceChildren(...meta.sensors.map((s) =>
    el("option", { value: s.id }, `${s.label}${s.value == null ? "" : ` (${s.value.toFixed(0)}°C)`}`)));
  if (![...sens.options].some((o) => o.value === g.sensor)) sens.append(el("option", { value: g.sensor }, g.sensor + " (missing)"));
  sens.value = g.sensor;

  const fans = $("port-fans");
  if (!fans.options.length) for (let n = 0; n <= 6; n++) fans.append(el("option", { value: n }, String(n)));
  fans.value = g.fans;

  const chips = $("mode-chips");
  chips.replaceChildren(...(isMb ? meta.mb_fan_modes : meta.fan_modes).map((m) => el("button", {
    class: "chip" + (m === g.mode ? " on" : ""),
    onclick: () => {
      const cur = selCfg();
      if (m === "curve" && cur.mode in meta.presets) cur.curve = meta.presets[cur.mode].map((p) => [...p]);
      cur.mode = m; save(0); renderFans();
    },
  }, MODE_LABELS[m])));

  $("manual-box").classList.toggle("hidden", g.mode !== "manual");
  $("manual-range").value = g.manual_percent;
  $("manual-val").textContent = g.manual_percent + "%";
  $("curve-box").classList.toggle("hidden", g.mode === "manual" || g.mode === "mb_sync" || g.mode === "bios");
  $("curve-hint").textContent = g.mode === "curve"
    ? "Drag points to shape the curve. Double-click to add a point, right-click a point to remove it."
    : "Preset curve. Pick Custom Curve to start editing from this shape.";
  drawCurve();
}

$("port-name").addEventListener("change", (e) => {
  selCfg().name = e.target.value || (sel.kind === "hub" ? `Port ${sel.i + 1}` : `Header ${sel.i}`); save(0); renderPorts();
});
$("port-sensor").addEventListener("change", (e) => { selCfg().sensor = e.target.value; save(0); });
$("port-fans").addEventListener("change", (e) => { selCfg().fans = +e.target.value; save(0); renderPorts(); });
$("manual-range").addEventListener("input", (e) => {
  selCfg().manual_percent = +e.target.value;
  $("manual-val").textContent = e.target.value + "%";
  save();
});
$("apply-all").addEventListener("click", () => {
  if (sel.kind !== "hub") return;
  const src = selCfg();
  for (const g of cfg.hub.groups) {
    if (g === src) continue;
    Object.assign(g, { mode: src.mode, manual_percent: src.manual_percent, sensor: src.sensor, curve: src.curve.map((p) => [...p]) });
  }
  save(0); renderFans();
});

// ---- curve editor ----
const W = 640, H = 300, PAD = { l: 40, r: 14, t: 14, b: 28 };
const tx = (t) => PAD.l + (t - T_MIN) / (T_MAX - T_MIN) * (W - PAD.l - PAD.r);
const ty = (p) => H - PAD.b - p / 100 * (H - PAD.t - PAD.b);
const fx = (x) => T_MIN + (x - PAD.l) / (W - PAD.l - PAD.r) * (T_MAX - T_MIN);
const fy = (y) => (H - PAD.b - y) / (H - PAD.t - PAD.b) * 100;
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const NS = "http://www.w3.org/2000/svg";
const sv = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };

function curvePoints() {
  const g = selCfg();
  return g.mode === "curve" ? g.curve : (meta.presets[g.mode] || g.curve);
}

function drawCurve() {
  const svg = $("curve");
  const g = selCfg();
  const editable = g.mode === "curve";
  svg.classList.toggle("readonly", !editable);
  svg.replaceChildren();
  for (let t = T_MIN; t <= T_MAX; t += 10) {
    svg.append(sv("line", { class: "grid", x1: tx(t), x2: tx(t), y1: PAD.t, y2: H - PAD.b }));
    const lab = sv("text", { class: "axis", x: tx(t), y: H - 8, "text-anchor": "middle" }); lab.textContent = t + "°"; svg.append(lab);
  }
  for (let p = 0; p <= 100; p += 20) {
    svg.append(sv("line", { class: "grid", x1: PAD.l, x2: W - PAD.r, y1: ty(p), y2: ty(p) }));
    const lab = sv("text", { class: "axis", x: PAD.l - 6, y: ty(p) + 4, "text-anchor": "end" }); lab.textContent = p + "%"; svg.append(lab);
  }
  const pts = curvePoints().map(([t, p]) => [clamp(t, T_MIN, T_MAX), p]);
  const first = pts[0], last = pts[pts.length - 1];
  const path = [[T_MIN, first[1]], ...pts, [T_MAX, last[1]]].map(([t, p]) => `${tx(t)},${ty(p)}`).join(" ");
  svg.append(sv("polygon", { class: "area", points: `${tx(T_MIN)},${ty(0)} ${path} ${tx(T_MAX)},${ty(0)}` }));
  svg.append(sv("polyline", { class: "line", points: path }));

  const s = selState();
  if (s && s.temp != null) {
    const t = clamp(s.temp, T_MIN, T_MAX);
    svg.append(sv("line", { class: "now", x1: tx(t), x2: tx(t), y1: PAD.t, y2: H - PAD.b }));
    if (s.percent != null) svg.append(sv("circle", { class: "nowpt", cx: tx(t), cy: ty(s.percent), r: 4 }));
  }
  pts.forEach(([t, p], i) => {
    const c = sv("circle", { class: "pt", cx: tx(t), cy: ty(p), r: 7 });
    if (editable) {
      c.addEventListener("pointerdown", (e) => startDrag(e, i));
      c.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        const cur = selCfg().curve;
        if (cur.length > 2) { cur.splice(i, 1); save(0); drawCurve(); }
      });
    }
    svg.append(c);
  });
}

function svgPoint(e) {
  const svg = $("curve"), r = svg.getBoundingClientRect();
  return [(e.clientX - r.left) / r.width * W, (e.clientY - r.top) / r.height * H];
}

function startDrag(e, i) {
  e.preventDefault();
  const svg = $("curve"), curve = selCfg().curve;
  svg.setPointerCapture(e.pointerId);
  dragging = true;
  const move = (ev) => {
    const [x, y] = svgPoint(ev);
    // Keep points ordered: a point can't pass its neighbours.
    const lo = i > 0 ? curve[i - 1][0] + 1 : T_MIN, hi = i < curve.length - 1 ? curve[i + 1][0] - 1 : T_MAX;
    curve[i] = [Math.round(clamp(fx(x), lo, hi)), Math.round(clamp(fy(y), 0, 100))];
    drawCurve();
  };
  const up = () => { dragging = false; svg.removeEventListener("pointermove", move); svg.removeEventListener("pointerup", up); save(0); };
  svg.addEventListener("pointermove", move);
  svg.addEventListener("pointerup", up);
}

$("curve").addEventListener("dblclick", (e) => {
  const g = selCfg();
  if (g.mode !== "curve") return;
  const [x, y] = svgPoint(e);
  g.curve.push([Math.round(clamp(fx(x), T_MIN, T_MAX)), Math.round(clamp(fy(y), 0, 100))]);
  g.curve.sort((a, b) => a[0] - b[0]);
  save(0); drawCurve();
});

// ---------------- lighting ----------------
const HUB_INNER = 8, HUB_OUTER = 12, HUB_FANS = 6, STRIMER_LEDS = 27;
const BRIGHT_LABELS = { "-1": "Off", 0: "Lowest", 1: "Low", 2: "Normal", 3: "High", 4: "Highest" };
const SPEED_LABELS = ["Slowest", "Slow", "Normal", "Fast", "Fastest"];
let brush = "#ff00c8", paintTool = "led", painting = false, strimerPort = 0;

function renderThemes(box) {
  box.replaceChildren(...meta.themes.map((t) => {
    const sw = el("div", { class: "swatch" });
    sw.style.background = `linear-gradient(90deg, ${t.swatch.join(", ")})`;
    return el("button", {
      class: "theme" + (cfg.theme === t.name ? " on" : ""),
      onclick: async () => {
        try { cfg = await api("/api/theme", { name: t.name }); renderAll(); }
        catch (e) { toast("Theme failed: " + e.message); }
      },
    }, sw, el("span", {}, t.name));
  }));
}

function slider(min, max, value, labels, onchange) {
  const out = el("b", {}, labels[value] ?? String(value));
  const inp = el("input", { type: "range", min, max, value });
  inp.addEventListener("input", () => { out.textContent = labels[inp.value] ?? inp.value; });
  inp.addEventListener("change", () => onchange(+inp.value));
  return el("div", { class: "slider-wrap" }, inp, out);
}

function field(label, ...kids) { return el("div", { class: "field" }, el("span", {}, label), el("div", {}, ...kids)); }

function seg(options, current, onpick) {
  return el("div", { class: "seg" }, ...options.map(([v, label]) =>
    el("button", { class: v === current ? "on" : "", onclick: () => onpick(v) }, label)));
}

function colorInputs(colors, count, labelFn, onchange, { addRemove = false } = {}) {
  const box = el("div", { class: "colors" });
  for (let i = 0; i < count; i++) {
    const v = colors[i] || colors[colors.length - 1] || "#ffffff";
    const inp = el("input", { type: "color", value: v, title: labelFn(i) });
    inp.addEventListener("change", () => {
      const next = [...colors];
      while (next.length < i) next.push(next[next.length - 1] || "#ffffff");
      next[i] = inp.value;
      onchange(next.slice(0, Math.max(count, next.length)));
    });
    box.append(el("label", { class: "cpick" }, inp, el("small", {}, labelFn(i))));
  }
  if (addRemove) {
    if (count < 6) box.append(el("button", { class: "btn small", onclick: () => onchange([...colors.slice(0, count), colors[count - 1] || "#ffffff"]) }, "+"));
    if (count > 2) box.append(el("button", { class: "btn small", onclick: () => onchange(colors.slice(0, count - 1)) }, "−"));
  }
  return box;
}

// kind: "hub" | "strimer". ctx: { entry, set(patch), fans (hub only) }
function lightingEditor(kind, ctx) {
  const L = ctx.entry;
  const modes = kind === "hub" ? meta.hub_lighting_modes : meta.strimer_lighting_modes;
  const spec = modes.find((m) => m.name === L.mode) || modes[0];
  const hw = modes.filter((m) => !m.software && (kind !== "hub" || L.ring === "both" || L.ring === undefined || m[L.ring]));
  const sw = modes.filter((m) => m.software);
  const effectBtn = (m) => el("button", { class: "effect" + (m.name === L.mode ? " on" : ""), onclick: () => ctx.set({ mode: m.name }) },
    m.name, ...(m.inner === false ? [el("small", {}, "outer ring only")] : m.outer === false ? [el("small", {}, "inner ring only")] : []));

  const panel = el("div", { class: "panel light-panel" });
  if (kind === "hub" && L.mode !== "Custom") {
    panel.append(field("Ring", seg([["both", "Both rings"], ["inner", "Inner"], ["outer", "Outer"]], L.ring, (v) => ctx.set({ ring: v }))));
  }
  // colours
  if (L.mode === "Gradient") {
    const n = Math.max(2, Math.min(6, L.colors.length));
    panel.append(field("Colour stops", colorInputs(L.colors, n, (i) => `Stop ${i + 1}`, (c) => ctx.set({ colors: c }), { addRemove: true })));
    panel.append(field("Spread", seg([[true, kind === "hub" ? "Across all ports" : "Across ports"], [false, kind === "hub" ? "Within this port" : "Along each port"]],
      !!L.span_all, (v) => ctx.set({ span_all: v }))));
  } else if (L.mode === "Temperature") {
    const sens = el("select", {}, ...meta.sensors.map((s) => el("option", { value: s.id }, s.label)));
    sens.value = L.sensor;
    sens.addEventListener("change", () => ctx.set({ sensor: sens.value }));
    const lo = el("input", { type: "number", value: L.temp_range[0], min: 0, max: 110 });
    const hi = el("input", { type: "number", value: L.temp_range[1], min: 1, max: 120 });
    const upd = () => { if (+lo.value < +hi.value) ctx.set({ temp_range: [+lo.value, +hi.value] }); else toast("Cool temperature must be below hot"); };
    lo.addEventListener("change", upd); hi.addEventListener("change", upd);
    panel.append(field("Sensor", sens));
    panel.append(field("Range (°C)", el("div", { class: "row gap" }, el("span", { class: "thermo cold" }, "blue at"), lo, el("span", { class: "thermo hot" }, "red at"), hi)));
  } else if (L.mode === "Custom") {
    panel.append(field("Paint", painter(kind, ctx)));
  } else if (spec.max_colors) {
    const perFan = kind === "hub" && (L.mode === "Static" || L.mode === "Breathing");
    const n = perFan ? Math.max(1, ctx.fans) : spec.max_colors;
    const lab = perFan ? (i) => `Fan ${i + 1}` : spec.name.includes("Colorful") ? (i) => ["Top", "Right", "Bottom", "Left"][i] : (i) => `#${i + 1}`;
    panel.append(field(perFan ? "Fan colours" : "Colours", colorInputs(L.colors, n, lab, (c) => ctx.set({ colors: c }))));
  } else {
    panel.append(field("Colours", el("span", { class: "none" }, L.mode === "Off" ? "LEDs off" : "This effect uses built-in colours")));
  }
  if (!spec.software && L.mode !== "Off") {
    panel.append(field("Speed", slider(0, 4, L.speed, SPEED_LABELS, (v) => ctx.set({ speed: v }))));
    panel.append(field("Direction", seg([["cw", "Clockwise"], ["ccw", "Counter-clockwise"]], L.direction, (v) => ctx.set({ direction: v }))));
  }
  if (L.mode !== "Off") panel.append(field("Brightness", slider(-1, 4, L.brightness, BRIGHT_LABELS, (v) => ctx.set({ brightness: v }))));

  return el("div", {},
    el("h3", {}, "Hardware effects"), el("div", { class: "effects" }, ...hw.map(effectBtn)),
    el("h3", {}, "Software effects ", el("small", {}, "rendered per-LED by UniHub")), el("div", { class: "effects" }, ...sw.map(effectBtn)),
    panel);
}

// ---- per-LED painter ----
function painter(kind, ctx) {
  const L = ctx.entry;
  const custom = JSON.parse(JSON.stringify(L.custom || {}));
  const rings = kind === "hub" ? [["outer", HUB_OUTER], ["inner", HUB_INNER]] : [["leds", STRIMER_LEDS]];
  for (const [ring, n] of rings) {
    const total = (kind === "hub" ? HUB_FANS : 1) * n;
    custom[ring] = Array.from({ length: total }, (_, i) => (custom[ring] || [])[i] || "#000000");
  }
  const commit = () => ctx.set({ custom: JSON.parse(JSON.stringify(custom)) }, { rerender: false, delay: 300 });

  const paintLed = (ring, idx, dot) => {
    const n = rings.find((r) => r[0] === ring)[1];
    const fan = Math.floor(idx / n);
    const targets = paintTool === "led" ? [idx]
      : paintTool === "ring" ? Array.from({ length: n }, (_, k) => fan * n + k)
      : null;
    if (targets) for (const t of targets) custom[ring][t] = brush;
    else for (const [r, rn] of rings) for (let k = 0; k < rn; k++) custom[r][fan * rn + k] = brush;  // whole fan
    root.querySelectorAll("[data-ring]").forEach((d) => { d.setAttribute("fill", custom[d.dataset.ring][+d.dataset.idx]); });
    commit();
  };

  const root = el("div", { class: "painter" });
  const brushInp = el("input", { type: "color", value: brush });
  brushInp.addEventListener("input", () => { brush = brushInp.value; });
  const tools = kind === "hub" ? [["led", "LED"], ["ring", "Ring"], ["fan", "Whole fan"]] : [["led", "LED"], ["ring", "Whole port"]];
  const toolSeg = () => seg(tools, paintTool, (v) => { paintTool = v; toolBox.replaceChildren(toolSeg()); });
  const toolBox = el("div", {}, toolSeg());
  const fillAll = el("button", { class: "btn small", onclick: () => {
    for (const [r] of rings) custom[r] = custom[r].map(() => brush);
    root.querySelectorAll("[data-ring]").forEach((d) => d.setAttribute("fill", brush));
    commit();
  } }, "Fill all");
  root.append(el("div", { class: "row gap" }, el("label", {}, "Brush", brushInp), toolBox, fillAll));

  const area = el("div", { class: "fans-grid" });
  const dot = (ring, idx, cx, cy, r) => {
    const c = sv("circle", { cx, cy, r, fill: custom[ring][idx], class: "led", "data-ring": ring, "data-idx": idx });
    c.addEventListener("pointerdown", (e) => { e.preventDefault(); painting = true; paintLed(ring, idx, c); });
    c.addEventListener("pointerenter", () => { if (painting && paintTool === "led") paintLed(ring, idx, c); });
    return c;
  };
  if (kind === "hub") {
    const fans = Math.max(1, ctx.fans);
    for (let f = 0; f < fans; f++) {
      const s = sv("svg", { viewBox: "0 0 120 120", class: "fan" });
      s.append(sv("rect", { x: 4, y: 4, width: 112, height: 112, rx: 14, class: "fan-body" }));
      s.append(sv("circle", { cx: 60, cy: 60, r: 16, class: "fan-hub" }));
      // Outer: 12 LEDs around the frame, 3 per side (corners on AL fans). Inner: 8 around the hub.
      for (let k = 0; k < HUB_OUTER; k++) {
        const a = -Math.PI / 2 + (k + 0.5) * 2 * Math.PI / HUB_OUTER;
        s.append(dot("outer", f * HUB_OUTER + k, 60 + 46 * Math.cos(a), 60 + 46 * Math.sin(a), 6));
      }
      for (let k = 0; k < HUB_INNER; k++) {
        const a = -Math.PI / 2 + (k + 0.5) * 2 * Math.PI / HUB_INNER;
        s.append(dot("inner", f * HUB_INNER + k, 60 + 27 * Math.cos(a), 60 + 27 * Math.sin(a), 5));
      }
      area.append(el("div", { class: "fan-wrap" }, s, el("small", {}, `Fan ${f + 1}`)));
    }
  } else {
    const s = sv("svg", { viewBox: `0 0 ${STRIMER_LEDS * 22 + 8} 30`, class: "strip" });
    for (let k = 0; k < STRIMER_LEDS; k++) s.append(dot("leds", k, 15 + k * 22, 15, 8));
    area.append(s);
  }
  root.append(area, el("p", { class: "hint" }, "Click or drag to paint. Outer ring = the 12 frame LEDs, inner ring = the 8 LEDs around the hub. LED 1 is drawn at the top; the physical start position may differ on your fans."));
  return root;
}
window.addEventListener("pointerup", () => { painting = false; });

function editLight({ rerender = true, delay = 0 } = {}) {
  cfg.theme = null;
  save(delay);
  if (rerender) { renderHubLighting(); renderStrimer(); }
}

function renderHubLighting() {
  renderThemes($("hub-themes"));
  ensureVisibleSelection();
  $("light-ports").replaceChildren(...[...visiblePorts().map(([g, i]) => [g.name, i]), ["All ports", 4]].map(([name, i]) =>
    el("button", { class: i === lightPort ? "on" : "", onclick: () => { lightPort = i; renderHubLighting(); } }, name)));
  $("hub-mbsync").checked = cfg.hub.mb_rgb_sync;
  const box = $("hub-light-controls");
  box.classList.toggle("hidden", cfg.hub.mb_rgb_sync);
  const idx = lightPort === 4 ? 0 : lightPort;
  box.replaceChildren(lightingEditor("hub", {
    entry: cfg.hub.lighting[idx],
    fans: lightPort === 4 ? Math.max(...cfg.hub.groups.map((g) => g.fans)) : cfg.hub.groups[idx].fans,
    // Look the entry up at edit time: cfg may have been replaced by a save.
    set: (patch, opts) => {
      const targets = lightPort === 4 ? [0, 1, 2, 3] : [idx];
      const merged = { ...cfg.hub.lighting[idx], ...patch };
      for (const i of targets) cfg.hub.lighting[i] = JSON.parse(JSON.stringify(merged));
      editLight(opts);
    },
  }));
}
$("hub-mbsync").addEventListener("change", (e) => { cfg.hub.mb_rgb_sync = e.target.checked; cfg.theme = null; save(0); renderHubLighting(); });

function renderStrimer() {
  renderThemes($("strimer-themes"));
  const s = cfg.strimer, ports = state?.strimer.ports || 12;
  $("strimer-mbsync").checked = s.mb_rgb_sync;
  $("strimer-perport").checked = s.per_port;
  $("strimer-ports").classList.toggle("hidden", !s.per_port);
  $("strimer-ports").replaceChildren(...Array.from({ length: ports }, (_, i) =>
    el("button", { class: i === strimerPort ? "on" : "", onclick: () => { strimerPort = i; renderStrimer(); } }, String(i + 1))));
  const box = $("strimer-light-controls");
  box.classList.toggle("hidden", s.mb_rgb_sync);
  box.replaceChildren(lightingEditor("strimer", {
    entry: s.per_port ? s.ports[strimerPort] : s.lighting,
    set: (patch, opts) => {
      const st = cfg.strimer;
      if (st.per_port) st.ports[strimerPort] = { ...st.ports[strimerPort], ...patch };
      else st.lighting = { ...st.lighting, ...patch };
      editLight(opts);
    },
  }));
}
$("strimer-mbsync").addEventListener("change", (e) => { cfg.strimer.mb_rgb_sync = e.target.checked; cfg.theme = null; save(0); renderStrimer(); });
$("strimer-perport").addEventListener("change", (e) => {
  const s = cfg.strimer;
  s.per_port = e.target.checked;
  if (s.per_port) s.ports = s.ports.map(() => JSON.parse(JSON.stringify(s.lighting)));  // start from the current look
  cfg.theme = null; save(0); renderStrimer();
});

// ---------------- settings ----------------
function renderSettings() {
  $("set-poll").value = cfg.poll_interval;
  $("set-min").value = cfg.min_percent;
  $("set-exit").value = cfg.on_exit;
  $("set-exit-pct").value = cfg.on_exit_percent;
  $("exit-pct-row").classList.toggle("hidden", cfg.on_exit !== "fixed");
  $("set-show-empty").checked = cfg.ui.show_empty_ports;
  document.querySelectorAll("[data-app]").forEach((inp) => { inp.checked = !!cfg.app[inp.dataset.app]; });
  const trayOff = !cfg.app.show_tray;
  for (const k of ["start_minimized", "close_to_tray"]) {
    const inp = document.querySelector(`[data-app="${k}"]`);
    inp.disabled = trayOff;  // both need somewhere to minimize to
    inp.closest("label").style.opacity = trayOff ? 0.5 : 1;
  }
}
document.querySelectorAll("[data-app]").forEach((inp) => inp.addEventListener("change", () => {
  cfg.app[inp.dataset.app] = inp.checked; save(0); renderSettings();
}));
$("set-show-empty").addEventListener("change", (e) => { cfg.ui.show_empty_ports = e.target.checked; save(0); renderAll(); });
$("set-poll").addEventListener("change", (e) => { cfg.poll_interval = +e.target.value; save(0); });
$("set-min").addEventListener("change", (e) => { cfg.min_percent = +e.target.value; save(0); });
$("set-exit").addEventListener("change", (e) => { cfg.on_exit = e.target.value; save(0); renderSettings(); });
$("set-exit-pct").addEventListener("change", (e) => { cfg.on_exit_percent = +e.target.value; save(0); });

// ---------------- live state ----------------
function renderStatus() {
  const h = state.hub, s = state.strimer;
  $("hub-dot").classList.toggle("on", h.connected);
  $("strimer-dot").classList.toggle("on", s.connected);
  $("fw").textContent = [h.connected && `Hub firmware ${h.firmware}`, s.connected && `Strimer firmware ${s.firmware}`].filter(Boolean).join("\n");
  const b = $("banner");
  const msg = state.error || (!h.connected ? "UNI HUB not connected — check the USB cable and the udev rule (see README)." : "");
  b.textContent = msg;
  b.classList.toggle("hidden", !msg);
}

function renderAll() { renderFans(); renderHubLighting(); renderStrimer(); renderSettings(); renderLab(); }

async function poll() {
  try {
    state = await api("/api/state");
    renderStatus();
    renderPorts();
    if (!dragging) drawCurve();
  } catch (e) {
    $("banner").textContent = "UniHub daemon not reachable.";
    $("banner").classList.remove("hidden");
  }
}

(async function init() {
  [cfg, meta, state] = await Promise.all([api("/api/config"), api("/api/meta"), api("/api/state")]);
  renderAll();
  renderStatus();
  setInterval(poll, 1000);
  setInterval(async () => { meta.sensors = (await api("/api/meta")).sensors; }, 15000);
})();

// ---------------- lighting lab ----------------
const lab = { group: 2, ring: "inner", mode: 1, speed: 2, palette: "rainbow" };
const LAB_PALETTES = {
  rainbow: ["#ff0000", "#ffd400", "#00ff6a", "#0088ff", "#b000ff", "#ff00c8"],
  "red/blue": ["#ff0000", "#0000ff"],
  white: ["#ffffff"],
};

async function labSend() {
  try {
    await api("/api/lab", { action: "show", group: lab.group, ring: lab.ring, mode: lab.mode, speed: lab.speed, colors: LAB_PALETTES[lab.palette] });
  } catch (e) { toast("Lab: " + e.message); }
}

function renderLab() {
  ensureVisibleSelection();
  const p = $("lab-panel");
  const num = el("input", { type: "number", min: 0, max: 255, value: lab.mode });
  num.addEventListener("change", () => { lab.mode = clamp(+num.value || 0, 0, 255); labSend(); renderLab(); });
  const step = (d) => () => { lab.mode = clamp(lab.mode + d, 0, 255); labSend(); renderLab(); };
  const names = [...new Set(meta.hub_lighting_modes.filter((m) => !m.software && m[lab.ring] !== false).map((m) => m.name))];
  const pick = el("select", {}, el("option", { value: "" }, "— it looks like… —"), ...names.map((n) => el("option", { value: n }, n)));
  const saveBtn = el("button", { class: "btn", onclick: () => {
    if (!pick.value) return toast("Pick which effect this looks like first");
    cfg.hub.mode_map[lab.ring][pick.value] = lab.mode;
    save(0); renderLab();
  } }, "Save label");
  p.replaceChildren(
    field("Port", seg(visiblePorts().map(([g, i]) => [i, g.name]), lab.group, (v) => { lab.group = v; labSend(); renderLab(); })),
    field("Ring", seg([["inner", "Inner (fan)"], ["outer", "Outer (edge)"]], lab.ring, (v) => { lab.ring = v; labSend(); renderLab(); })),
    field("Effect #", el("div", { class: "lab-num" },
      el("button", { class: "btn", onclick: step(-1) }, "◀ Prev"), num, el("button", { class: "btn", onclick: step(1) }, "Next ▶"))),
    field("Colours", seg(Object.keys(LAB_PALETTES).map((k) => [k, k]), lab.palette, (v) => { lab.palette = v; labSend(); renderLab(); })),
    field("Speed", slider(0, 4, lab.speed, SPEED_LABELS, (v) => { lab.speed = v; labSend(); })),
    field("Label", el("div", { class: "row gap" }, pick, saveBtn)),
    el("div", { class: "row end" }, el("button", { class: "btn", onclick: async () => {
      await api("/api/lab", { action: "end" }); toast("Normal lighting restored");
    } }, "Done — restore my lighting")),
  );

  const rows = meta.hub_lighting_modes.filter((m) => !m.software).map((m) => {
    const cell = (ring) => {
      if (m[ring] === false) return el("td", { class: "g" }, "—");
      const v = cfg.hub.mode_map[ring][m.name];
      if (v === undefined) return m.verified?.[ring]
        ? el("td", { class: "v" }, "✓ from L-Connect capture")
        : el("td", { class: "g" }, "unverified");
      return el("td", { class: "v" }, `#${v} ✓ `, el("button", { class: "btn small", onclick: () => { delete cfg.hub.mode_map[ring][m.name]; save(0); renderLab(); } }, "×"));
    };
    return el("tr", {}, el("td", {}, m.name), cell("inner"), cell("outer"));
  });
  $("lab-map").replaceChildren(el("table", { class: "map-table" },
    el("tr", {}, el("th", {}, "Effect"), el("th", {}, "Inner ring"), el("th", {}, "Outer ring")), ...rows));
}
