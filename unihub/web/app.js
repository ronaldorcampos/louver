"use strict";

const MODE_LABELS = {
  quiet: "Quiet", standard: "Standard", high: "High Speed", full: "Full Speed",
  manual: "Fixed", curve: "Custom Curve", mb_sync: "MB PWM Sync",
};
const T_MIN = 20, T_MAX = 100;
const $ = (id) => document.getElementById(id);

let cfg = null, meta = null, state = null;
let selPort = 0, lightPort = 0;  // lightPort 4 = all ports
let saveTimer = null, dragging = false;

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
function save(delay = 250) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    try { cfg = await api("/api/config", cfg); }
    catch (e) { toast("Not saved: " + e.message); cfg = await api("/api/config"); renderAll(); }
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
function renderPorts() {
  const box = $("ports");
  box.replaceChildren();
  cfg.hub.groups.forEach((g, i) => {
    const s = state?.hub.groups[i] || {};
    const pct = s.percent;
    box.append(el("button", { class: "port" + (i === selPort ? " sel" : ""), onclick: () => { selPort = i; renderFans(); } },
      el("div", { class: "pname" }, g.name),
      el("div", { class: "pmode" }, MODE_LABELS[g.mode] + " · " + g.fans + " fan" + (g.fans === 1 ? "" : "s")),
      el("div", { class: "rpm" }, String(s.rpm ?? 0), el("small", {}, " RPM")),
      el("div", { class: "meta" },
        el("span", {}, pct == null ? "MB" : pct + "%"),
        el("span", {}, s.temp == null ? "—" : s.temp.toFixed(1) + " °C")),
      el("div", { class: "bar" }, el("i")),
      ...(s.failsafe ? [el("span", { class: "fs" }, "NO TEMP")] : []),
    ));
    box.lastChild.querySelector(".bar i").style.width = (pct ?? 0) + "%";
  });
}

function renderFans() {
  renderPorts();
  const g = cfg.hub.groups[selPort];
  $("port-name").value = g.name;

  const sens = $("port-sensor");
  sens.replaceChildren(...meta.sensors.map((s) =>
    el("option", { value: s.id }, `${s.label}${s.value == null ? "" : ` (${s.value.toFixed(0)}°C)`}`)));
  if (![...sens.options].some((o) => o.value === g.sensor)) sens.append(el("option", { value: g.sensor }, g.sensor + " (missing)"));
  sens.value = g.sensor;

  const fans = $("port-fans");
  if (!fans.options.length) for (let n = 0; n <= 6; n++) fans.append(el("option", { value: n }, String(n)));
  fans.value = g.fans;

  const chips = $("mode-chips");
  chips.replaceChildren(...meta.fan_modes.map((m) => el("button", {
    class: "chip" + (m === g.mode ? " on" : ""),
    onclick: () => {
      if (m === "curve" && g.mode in meta.presets) g.curve = meta.presets[g.mode].map((p) => [...p]);
      g.mode = m; save(0); renderFans();
    },
  }, MODE_LABELS[m])));

  $("manual-box").classList.toggle("hidden", g.mode !== "manual");
  $("manual-range").value = g.manual_percent;
  $("manual-val").textContent = g.manual_percent + "%";
  $("curve-box").classList.toggle("hidden", g.mode === "manual" || g.mode === "mb_sync");
  $("curve-hint").textContent = g.mode === "curve"
    ? "Drag points to shape the curve. Double-click to add a point, right-click a point to remove it."
    : "Preset curve. Pick Custom Curve to start editing from this shape.";
  drawCurve();
}

$("port-name").addEventListener("change", (e) => { cfg.hub.groups[selPort].name = e.target.value || `Port ${selPort + 1}`; save(0); renderPorts(); });
$("port-sensor").addEventListener("change", (e) => { cfg.hub.groups[selPort].sensor = e.target.value; save(0); });
$("port-fans").addEventListener("change", (e) => { cfg.hub.groups[selPort].fans = +e.target.value; save(0); renderPorts(); });
$("manual-range").addEventListener("input", (e) => {
  cfg.hub.groups[selPort].manual_percent = +e.target.value;
  $("manual-val").textContent = e.target.value + "%";
  save();
});
$("apply-all").addEventListener("click", () => {
  const src = cfg.hub.groups[selPort];
  cfg.hub.groups.forEach((g, i) => {
    if (i === selPort) return;
    Object.assign(g, { mode: src.mode, manual_percent: src.manual_percent, sensor: src.sensor, curve: src.curve.map((p) => [...p]) });
  });
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
  const g = cfg.hub.groups[selPort];
  return g.mode === "curve" ? g.curve : (meta.presets[g.mode] || g.curve);
}

function drawCurve() {
  const svg = $("curve");
  const g = cfg.hub.groups[selPort];
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

  const s = state?.hub.groups[selPort];
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
        if (g.curve.length > 2) { g.curve.splice(i, 1); save(0); drawCurve(); }
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
  const svg = $("curve"), curve = cfg.hub.groups[selPort].curve;
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
  const g = cfg.hub.groups[selPort];
  if (g.mode !== "curve") return;
  const [x, y] = svgPoint(e);
  g.curve.push([Math.round(clamp(fx(x), T_MIN, T_MAX)), Math.round(clamp(fy(y), 0, 100))]);
  g.curve.sort((a, b) => a[0] - b[0]);
  save(0); drawCurve();
});

// ---------------- lighting (shared) ----------------
function lightingUI({ effectsBox, colorsBox, speed, bright, dir, getLight, setLight, modes, filterMode }) {
  const L = getLight();
  const spec = modes.find((m) => m.name === L.mode) || modes[0];
  effectsBox.replaceChildren(...modes.filter(filterMode).map((m) => el("button", {
    class: "effect" + (m.name === L.mode ? " on" : ""),
    onclick: () => setLight({ mode: m.name }),
  }, m.name, ...(m.inner === false ? [el("small", {}, "outer ring only")] : m.outer === false ? [el("small", {}, "inner ring only")] : []))));

  const n = spec.max_colors;
  colorsBox.replaceChildren();
  if (!n) colorsBox.append(el("span", { class: "none" }, "This effect uses built-in colours"));
  for (let i = 0; i < n; i++) {
    const v = L.colors[i] || L.colors[L.colors.length - 1] || "#ffffff";
    const inp = el("input", { type: "color", value: v });
    inp.addEventListener("change", () => {
      const colors = [...L.colors];
      while (colors.length < i) colors.push(colors[colors.length - 1] || "#ffffff");
      colors[i] = inp.value;
      setLight({ colors });
    });
    colorsBox.append(inp);
  }
  speed.value = L.speed;
  speed.onchange = () => setLight({ speed: +speed.value });
  bright.value = L.brightness;
  bright.onchange = () => setLight({ brightness: +bright.value });
  dir.replaceChildren(...[["cw", "Clockwise"], ["ccw", "Counter-clockwise"]].map(([v, label]) =>
    el("button", { class: L.direction === v ? "on" : "", onclick: () => setLight({ direction: v }) }, label)));
}

function renderHubLighting() {
  const lights = cfg.hub.lighting;
  $("light-ports").replaceChildren(...[...cfg.hub.groups.map((g) => g.name), "All ports"].map((name, i) =>
    el("button", { class: i === lightPort ? "on" : "", onclick: () => { lightPort = i; renderHubLighting(); } }, name)));
  $("hub-mbsync").checked = cfg.hub.mb_rgb_sync;
  $("hub-light-controls").classList.toggle("hidden", cfg.hub.mb_rgb_sync);
  const idx = lightPort === 4 ? 0 : lightPort;
  const L = lights[idx];
  $("ring-seg").replaceChildren(...[["both", "Both rings"], ["inner", "Inner"], ["outer", "Outer"]].map(([v, label]) =>
    el("button", { class: L.ring === v ? "on" : "", onclick: () => setHubLight({ ring: v }) }, label)));
  lightingUI({
    effectsBox: $("hub-effects"), colorsBox: $("hub-colors"), speed: $("hub-speed"), bright: $("hub-bright"), dir: $("hub-dir"),
    getLight: () => L, setLight: setHubLight, modes: meta.hub_lighting_modes,
    filterMode: (m) => L.ring === "both" || m[L.ring],
  });
}

function setHubLight(patch) {
  const targets = lightPort === 4 ? [0, 1, 2, 3] : [lightPort];
  const base = cfg.hub.lighting[lightPort === 4 ? 0 : lightPort];
  for (const i of targets) cfg.hub.lighting[i] = { ...base, ...patch };
  save(0); renderHubLighting();
}
$("hub-mbsync").addEventListener("change", (e) => { cfg.hub.mb_rgb_sync = e.target.checked; save(0); renderHubLighting(); });

function renderStrimer() {
  const L = cfg.strimer.lighting;
  $("strimer-mbsync").checked = cfg.strimer.mb_rgb_sync;
  $("strimer-light-controls").classList.toggle("hidden", cfg.strimer.mb_rgb_sync);
  lightingUI({
    effectsBox: $("strimer-effects"), colorsBox: $("strimer-colors"), speed: $("strimer-speed"), bright: $("strimer-bright"), dir: $("strimer-dir"),
    getLight: () => L, setLight: (patch) => { cfg.strimer.lighting = { ...L, ...patch }; save(0); renderStrimer(); },
    modes: meta.strimer_lighting_modes, filterMode: () => true,
  });
}
$("strimer-mbsync").addEventListener("change", (e) => { cfg.strimer.mb_rgb_sync = e.target.checked; save(0); renderStrimer(); });

// ---------------- settings ----------------
function renderSettings() {
  $("set-poll").value = cfg.poll_interval;
  $("set-min").value = cfg.min_percent;
  $("set-exit").value = cfg.on_exit;
  $("set-exit-pct").value = cfg.on_exit_percent;
  $("exit-pct-row").classList.toggle("hidden", cfg.on_exit !== "fixed");
}
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

function renderAll() { renderFans(); renderHubLighting(); renderStrimer(); renderSettings(); }

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
