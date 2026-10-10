"use strict";

const MODE_LABELS = {
  quiet: "Quiet", standard: "Standard", high: "High Speed", full: "Full Speed",
  manual: "Fixed", curve: "Custom Curve", mb_sync: "MB PWM Sync", bios: "BIOS control",
};
const T_MIN = 20, T_MAX = 100;
const $ = (id) => document.getElementById(id);

let cfg = null, meta = null, state = null;
// The selected zone: a hub port (i = 0..3), a motherboard header (i = its
// number) or the Strimer. view is "zone" or "settings".
let sel = { kind: "hub", i: 2 }, view = "zone";
let saveTimer = null, dragging = false, editSeq = 0;
// savePending: an edit of ours hasn't reached the daemon yet. cfgRev: the
// daemon's config revision our copy matches (see adoptOutsideChanges); null
// until the first poll checks, since cfg and state load in parallel.
let savePending = false, cfgRev = null;

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "PUT", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-Louver": "1" },
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
  savePending = true;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try {
      const saved = await api("/api/config", cfg);
      if (seq === editSeq) { cfg = saved; savePending = false; }
    } catch (e) {
      toast("Not saved: " + e.message);
      if (seq === editSeq) {
        try { cfg = await api("/api/config"); renderAll(); } finally { savePending = false; }
      }
    }
  }, delay);
}

// Someone else changed the config (the tray, another window, a theme): take
// their copy, unless an edit of ours is still on its way. Returns true if it
// re-rendered.
async function adoptOutsideChanges() {
  if (state.config_rev === cfgRev || savePending || dragging) return false;
  const rev = state.config_rev, fresh = await api("/api/config");
  if (savePending || dragging) return false;  // an edit started while we fetched
  cfgRev = rev;
  if (JSON.stringify(fresh) === JSON.stringify(cfg)) return false;  // e.g. our own save
  cfg = fresh;
  renderAll();
  return true;
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

// ---------------- zones ----------------
// The pure parts (which zones exist, routing, previews) live in model.js.
const M = LouverModel;
const { sameZone, zoneKey, ringCss, barCss, clamp } = M;
const visiblePorts = () => M.visiblePorts(cfg);
const mbCfg = (index) => M.mbCfg(cfg, index);
let mbZeroRuns = {};
// The current selection stays listed even if its header stops spinning.
const zones = () => M.zones(cfg, state, { zeroRuns: mbZeroRuns, keep: sel });

function zoneName(z) {
  if (z.kind === "hub") return cfg.hub.groups[z.i].name;
  if (z.kind === "mb") return mbCfg(z.i)?.name || `Header ${z.i}`;
  return "Strimer Plus";
}
function zoneState(z) {
  if (z.kind === "hub") return state?.hub.groups[z.i];
  if (z.kind === "mb") return state?.motherboard.headers.find((h) => h.index === z.i);
  return state?.strimer;
}

// Keep the selection on a zone that still exists (a port set to 0 fans, a
// Strimer that was unplugged, a hash naming something that isn't there).
// Any header the board reports counts, even an idle one, so a hash like #mb-1
// can open a fan-stop header to set it up. sel is null only when there are no
// zones at all.
function ensureValidSelection() {
  sel = M.validSelection(sel, zones());
  const vis = visiblePorts().map(([, i]) => i);
  if (!vis.includes(lab.group) && vis.length) lab.group = vis[0];
}
const onZone = () => view === "zone" && sel !== null;

function applyRoute(r) {
  if (!r) return;
  view = r.view;
  if (r.sel) sel = r.sel;
}
// The address always names what's on screen, even after the selection moved
// on its own (a zone went away, or the hash named one that doesn't exist).
function syncHash() {
  const h = M.hashFor(view, sel) || "";
  if (location.hash !== h) history.replaceState(null, "", h || location.pathname);
}
function go(r) {
  applyRoute(r);
  renderAll();
}
window.addEventListener("hashchange", () => { applyRoute(M.parseHash(location.hash)); renderAll(); });

// ---- what a zone's lighting looks like, for the sidebar and the fan row ----
// A fan glyph is a square frame (outer ring) around a round hub (inner ring).
function setFanGlyph(g, p) {
  g.style.setProperty("--outer", p === "mb" ? "var(--line)" : ringCss(p && p.outer));
  g.style.setProperty("--inner", p === "mb" ? "var(--line)" : ringCss(p && p.inner));
}
function fanGlyph(p, big = false) {
  const g = el("span", { class: "fan-glyph" + (big ? " big" : "") }, el("i"));
  setFanGlyph(g, p);
  return g;
}
const strimerPorts = () => M.strimerPreview(cfg, meta, state?.strimer.ports || 12);
// The sidebar's one bar: each port's first colour, or the whole look if all
// ports match.
function strimerBar() {
  const all = strimerPorts();
  if (all === null) return MB_BAR;
  const same = all.every((c) => c.join() === all[0].join());
  return barCss(same ? all[0] : all.map((c) => c[0]));
}
const MB_BAR = barCss(null);
// What a zone's sidebar glyph shows: a hub port's first fan, a plain fan for a
// motherboard header, the Strimer's bar.
function zonePreview(z) {
  if (z.kind === "hub") return M.hubFanPreview(cfg, meta, z.i, 0);
  if (z.kind === "mb") return "mb";
  return strimerBar();
}

// ---------------- sidebar ----------------
// The buttons are updated in place, keyed by zone, so the 1 s refresh can't
// swallow a click that lands mid-rebuild or drop keyboard focus.
function zoneButton(z) {
  const glyph = z.kind === "strimer" ? el("span", { class: "strimer-glyph" }) : fanGlyph(null);
  return el("button", { class: "zone", "data-zone": zoneKey(z) },
    glyph, el("span", { class: "zname" }, el("b"), el("small")), el("i", { class: "zwarn hidden" }));
}
function fillZoneButton(b, z) {
  const s = zoneState(z);
  let sub;
  if (z.kind === "strimer") sub = `${s.ports} ports`;
  else {
    const c = z.kind === "hub" ? cfg.hub.groups[z.i] : mbCfg(z.i) || { mode: "bios" };
    sub = `${s?.rpm ?? 0} rpm` + (c.mode === "bios" ? " · BIOS" : c.mode === "mb_sync" ? " · MB" : "");
  }
  const [glyph, label, warnEl] = b.children;
  if (z.kind === "strimer") glyph.style.background = zonePreview(z);
  else setFanGlyph(glyph, zonePreview(z));
  label.children[0].textContent = zoneName(z);
  label.children[1].textContent = sub;
  const warn = s?.failsafe ? ["bad", "No temperature: running at 100%"]
    : s?.missing?.length ? ["warn", "Unreadable: " + s.missing.join(", ")] : null;
  warnEl.className = "zwarn" + (warn ? " " + warn[0] : " hidden");
  if (warn) { warnEl.title = warn[1]; warnEl.setAttribute("aria-label", warn[1]); }
  b.classList.toggle("active", onZone() && sameZone(z, sel));
}
function renderZoneList() {
  const box = $("zones"), zs = zones();
  const keys = zs.map(zoneKey);
  if (keys.join() !== [...box.children].map((b) => b.dataset.zone).join()) {
    const have = new Map([...box.children].map((b) => [b.dataset.zone, b]));
    box.replaceChildren(...zs.map((z) => have.get(zoneKey(z)) || zoneButton(z)));
  }
  zs.forEach((z, n) => fillZoneButton(box.children[n], z));
  $("nav-settings").classList.toggle("active", view === "settings");
}
$("zones").addEventListener("click", (e) => {
  const b = e.target.closest("[data-zone]");
  if (b) go(M.parseHash(b.dataset.zone));
});

function renderThemes() {
  $("themes").replaceChildren(...meta.themes.map((t) => {
    const b = el("button", {
      class: "tdot" + (cfg.theme === t.name ? " on" : ""), title: t.name, "aria-label": "Theme: " + t.name,
      onclick: async () => {
        try { cfg = await api("/api/theme", { name: t.name }); renderAll(); }
        catch (e) { toast("Theme failed: " + e.message); }
      },
    });
    b.style.background = `linear-gradient(135deg, ${t.swatch.join(", ")})`;
    return b;
  }));
  $("theme-name").textContent = cfg.theme || "";
}

function renderStatus() {
  const h = state.hub, s = state.strimer, m = state.motherboard;
  const row = (on, name, detail) => el("div", { class: "dev" }, el("span", { class: "dot" + (on ? " on" : "") }), el("span", {}, name), el("small", {}, detail));
  $("devices").replaceChildren(
    row(h.connected, "UNI HUB AL V2", h.connected ? h.firmware : "not connected"),
    row(s.connected, "Strimer Plus", s.connected ? s.firmware : "not connected"),
    ...(m?.present ? [row(m.writable && !m.error, "Motherboard", m.chip)] : []));
  const b = $("banner");
  const msg = state.error || (!h.connected ? "UNI HUB not connected — check the USB cable and the udev rule (see README)." : "")
    || (onZone() && sel.kind === "mb" ? m?.error || (m && !m.writable ? "Read-only: install the hwmon udev rule (README) to control motherboard headers." : "") : "");
  b.textContent = msg;
  b.classList.toggle("hidden", !msg);
}

function renderView() {
  $("view-zone").classList.toggle("hidden", !onZone());
  $("view-empty").classList.toggle("hidden", view !== "zone" || sel !== null);
  $("view-settings").classList.toggle("hidden", view !== "settings");
}

// ---------------- zone page ----------------
function renderZoneHead() {
  const z = sel, name = $("zone-name");
  name.value = zoneName(z);
  name.disabled = z.kind === "strimer";
  if (z.kind === "hub") {
    const n = cfg.hub.groups[z.i].fans;
    $("zone-sub").textContent = `${n} fan${n === 1 ? "" : "s"} · hub port ${z.i + 1}`;
  } else if (z.kind === "mb") {
    $("zone-sub").textContent = `motherboard header ${z.i}` + (state?.motherboard.chip ? ` · ${state.motherboard.chip}` : "");
  } else {
    $("zone-sub").textContent = `${state?.strimer.ports ?? 12} ports · firmware ${state?.strimer.firmware || "?"}`;
  }
  renderZoneStats();
}

function renderZoneStats() {
  const box = $("zone-stats");
  if (sel.kind === "strimer") { box.replaceChildren(); return; }
  const s = zoneState(sel) || {};
  const c = selCfg();
  const sensor = meta.sensors.find((x) => x.id === c.sensor);
  const stat = (big, small) => el("div", { class: "stat" }, el("b", {}, big), el("small", {}, small));
  // BIOS and MB PWM sync ignore Louver's sensor, so don't show one.
  const ownSensor = c.mode !== "bios" && c.mode !== "mb_sync";
  box.replaceChildren(
    stat(String(s.rpm ?? 0), "rpm"),
    stat(s.percent == null ? (c.mode === "bios" ? "BIOS" : "MB") : s.percent + "%", "duty"),
    ...(ownSensor ? [stat(s.temp == null ? "—" : s.temp.toFixed(0) + "°", sensor ? sensor.label : c.sensor)] : []),
    ...(s.failsafe ? [el("span", { class: "fs" }, "No temperature: running at 100%")]
      : s.missing?.length ? [el("span", { class: "fs warn" }, "Unreadable: " + s.missing.join(", "))] : []),
  );
}

function renderZoneFans() {
  const box = $("zone-fans");
  if (sel.kind === "hub") {
    const n = cfg.hub.groups[sel.i].fans;
    box.replaceChildren(...Array.from({ length: n }, (_, f) => fanGlyph(M.hubFanPreview(cfg, meta, sel.i, f), true)));
  } else if (sel.kind === "strimer") {
    const ports = strimerPorts();
    box.replaceChildren(...Array.from({ length: state?.strimer.ports || 12 }, (_, k) => {
      const b = el("span", { class: "strimer-glyph big" });
      b.style.background = ports === null ? MB_BAR : barCss(ports[k]);
      return b;
    }));
  } else box.replaceChildren();
  box.classList.toggle("hidden", !box.childElementCount);
}

function renderZone() {
  renderZoneHead();
  renderZoneFans();
  const hasSpeed = sel.kind !== "strimer", hasLight = sel.kind !== "mb";
  $("speed-panel").classList.toggle("hidden", !hasSpeed);
  $("light-panel").classList.toggle("hidden", !hasLight);
  if (hasSpeed) renderSpeed();
  if (hasLight) renderLighting();
}

$("zone-name").addEventListener("change", (e) => {
  if (!sel || sel.kind === "strimer") return;
  selCfg().name = e.target.value || (sel.kind === "hub" ? `Port ${sel.i + 1}` : `Header ${sel.i}`);
  save(0); renderZoneList();
});
$("nav-settings").addEventListener("click", () => go({ view: "settings" }));

// ---------------- speed ----------------
// A hub port (cfg.hub.groups[i]) or a motherboard header (cfg.motherboard.headers
// entry, created on first edit, BIOS by default).
function mbEntry(index) {
  let h = mbCfg(index);
  if (!h) {
    h = { index, name: `Header ${index}`, mode: "bios", manual_percent: 50, sensor: "cpu", curve: meta.presets.standard.map((p) => [...p]) };
    cfg.motherboard.headers.push(h);
  }
  return h;
}
const selCfg = () => (sel.kind === "hub" ? cfg.hub.groups[sel.i] : mbEntry(sel.i));
const selState = () => zoneState(sel);
const hasSpeed = () => onZone() && sel.kind !== "strimer";

function renderSpeed() {
  const g = selCfg();
  const isMb = sel.kind === "mb";
  $("port-fans-label").classList.toggle("hidden", isMb);
  $("port-sensor-label").classList.toggle("hidden", g.mode === "bios" || g.mode === "mb_sync");
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

  $("mode-chips").replaceChildren(...(isMb ? meta.mb_fan_modes : meta.fan_modes).map((m) => el("button", {
    class: "chip" + (m === g.mode ? " on" : ""),
    onclick: () => {
      const cur = selCfg();
      if (m === "curve" && cur.mode in meta.presets) cur.curve = meta.presets[cur.mode].map((p) => [...p]);
      cur.mode = m; save(0); renderSpeed(); renderZoneStats(); renderZoneList();
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

$("port-sensor").addEventListener("change", (e) => { selCfg().sensor = e.target.value; save(0); renderZoneStats(); });
$("port-fans").addEventListener("change", (e) => { selCfg().fans = +e.target.value; save(0); renderAll(); });
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
  save(0); renderAll();
});

// ---- curve editor ----
const W = 640, H = 300, PAD = { l: 40, r: 14, t: 14, b: 28 };
const tx = (t) => PAD.l + (t - T_MIN) / (T_MAX - T_MIN) * (W - PAD.l - PAD.r);
const ty = (p) => H - PAD.b - p / 100 * (H - PAD.t - PAD.b);
const fx = (x) => T_MIN + (x - PAD.l) / (W - PAD.l - PAD.r) * (T_MAX - T_MIN);
const fy = (y) => (H - PAD.b - y) / (H - PAD.t - PAD.b) * 100;
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


// ---------------- lighting editors ----------------
const HUB_INNER = 8, HUB_OUTER = 12, HUB_FANS = 6, STRIMER_LEDS = 27;
const BRIGHT_LABELS = { "-1": "Off", 0: "Lowest", 1: "Low", 2: "Normal", 3: "High", 4: "Highest" };
const SPEED_LABELS = ["Slowest", "Slow", "Normal", "Fast", "Fastest"];
let brush = "#ff00c8", paintTool = "led", painting = false, strimerPort = 0;

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
    el("h3", {}, "Software effects ", el("small", {}, "rendered per-LED by Louver")), el("div", { class: "effects" }, ...sw.map(effectBtn)),
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

function toggle(label, checked, onchange) {
  const inp = el("input", { type: "checkbox" });
  inp.checked = checked;
  inp.addEventListener("change", () => onchange(inp.checked));
  return el("label", { class: "toggle" }, inp, el("span"), label);
}

function editLight({ rerender = true, delay = 0 } = {}) {
  cfg.theme = null;
  save(delay);
  if (rerender) { renderLighting(); renderZoneFans(); renderZoneList(); renderThemes(); }
}

// ---------------- lighting ----------------
function renderLighting() {
  const tools = $("light-tools"), box = $("light-controls"), hint = $("mbsync-hint");
  if (sel.kind === "hub") {
    const i = sel.i, synced = cfg.hub.mb_rgb_sync;
    tools.replaceChildren(
      toggle("Follow motherboard ARGB (all hub ports)", synced, (v) => { cfg.hub.mb_rgb_sync = v; editLight(); }),
      ...(synced ? [] : [el("button", { class: "btn ghost", onclick: () => {
        for (let k = 0; k < cfg.hub.lighting.length; k++) if (k !== i) cfg.hub.lighting[k] = JSON.parse(JSON.stringify(cfg.hub.lighting[i]));
        editLight();
      } }, "Use this look on every hub port")]));
    hint.textContent = "The motherboard's ARGB header is driving every hub port's lighting. Turn that off to set it here.";
    hint.classList.toggle("hidden", !synced);
    box.classList.toggle("hidden", synced);
    if (synced) { box.replaceChildren(); return; }
    box.replaceChildren(lightingEditor("hub", {
      entry: cfg.hub.lighting[i],
      fans: cfg.hub.groups[i].fans,
      // Look the entry up at edit time: cfg may have been replaced by a save.
      set: (patch, opts) => { cfg.hub.lighting[i] = { ...cfg.hub.lighting[i], ...patch }; editLight(opts); },
    }));
    return;
  }
  // Strimer
  const s = cfg.strimer, ports = state?.strimer.ports || 12;
  if (strimerPort >= ports) strimerPort = 0;
  tools.replaceChildren(
    toggle("Follow motherboard ARGB", s.mb_rgb_sync, (v) => { cfg.strimer.mb_rgb_sync = v; editLight(); }),
    ...(s.mb_rgb_sync ? [] : [toggle("Per-port control", s.per_port, (v) => {
      const st = cfg.strimer;
      st.per_port = v;
      if (v) st.ports = st.ports.map(() => JSON.parse(JSON.stringify(st.lighting)));  // start from the current look
      editLight();
    })]));
  hint.textContent = "The motherboard's ARGB header is driving the Strimer. Turn that off to set it here.";
  hint.classList.toggle("hidden", !s.mb_rgb_sync);
  box.classList.toggle("hidden", s.mb_rgb_sync);
  if (s.mb_rgb_sync) { box.replaceChildren(); return; }
  box.replaceChildren(
    ...(s.per_port ? [field("Port", seg(Array.from({ length: ports }, (_, k) => [k, String(k + 1)]), strimerPort,
      (v) => { strimerPort = v; renderLighting(); }))] : []),
    lightingEditor("strimer", {
      entry: s.per_port ? s.ports[strimerPort] : s.lighting,
      set: (patch, opts) => {
        const st = cfg.strimer;
        if (st.per_port) st.ports[strimerPort] = { ...st.ports[strimerPort], ...patch };
        else st.lighting = { ...st.lighting, ...patch };
        editLight(opts);
      },
    }));
}

// ---------------- settings ----------------
function renderSettings() {
  $("set-poll").value = cfg.poll_interval;
  $("set-min").value = cfg.min_percent;
  $("set-exit").value = cfg.on_exit;
  $("set-exit-pct").value = cfg.on_exit_percent;
  $("exit-pct-row").classList.toggle("hidden", cfg.on_exit !== "fixed");
  $("set-show-empty").checked = cfg.ui.show_empty_ports;
  document.querySelectorAll("[data-app]").forEach((inp) => { inp.checked = !!cfg.app[inp.dataset.app]; });
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
function renderAll() {
  ensureValidSelection();
  syncHash();
  renderView();
  renderZoneList();
  renderThemes();
  renderStatus();
  if (onZone()) renderZone();
  renderSettings();
  renderLab();
}

async function poll() {
  try {
    state = await api("/api/state");
  } catch (e) {
    $("banner").textContent = "Louver daemon not reachable.";
    $("banner").classList.remove("hidden");
    return;
  }
  mbZeroRuns = M.nextZeroRuns(mbZeroRuns, state);
  try { if (await adoptOutsideChanges()) return; } catch (e) { /* daemon hiccup: try again next poll */ }
  const before = sel && zoneKey(sel);
  ensureValidSelection();
  if ((sel && zoneKey(sel)) !== before) { renderAll(); return; }  // the selected zone went away (or one appeared)
  renderStatus();
  renderZoneList();
  if (onZone()) { renderZoneStats(); renderZoneFans(); }
  if (hasSpeed() && !dragging) drawCurve();
}

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

(async function init() {
  [cfg, meta, state] = await Promise.all([api("/api/config"), api("/api/meta"), api("/api/state")]);
  mbZeroRuns = M.nextZeroRuns({}, state);
  applyRoute(M.parseHash(location.hash));
  go({ view });
  setInterval(poll, 1000);
  setInterval(async () => { meta.sensors = (await api("/api/meta")).sensors; }, 15000);
})();
