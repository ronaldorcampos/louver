"use strict";

const MODE_LABELS = {
  quiet: "Quiet", standard: "Standard", high: "High", full: "Full",
  manual: "Manual", curve: "Custom curve", mb_sync: "MB sync", bios: "BIOS",
};
const T_MIN = 20, T_MAX = 100;
const $ = (id) => document.getElementById(id);

let cfg = null, meta = null, state = null;
// The selected zone: a hub port (i = 0..3), a motherboard header (i = its
// number), the Strimer or the motherboard's RGB (kind "aura"). view is
// "zone" or "settings".
let sel = { kind: "hub", i: 2 }, view = "zone";
let saveTimer = null, dragging = false, editSeq = 0;
// savePending: an edit of ours hasn't reached the daemon yet. cfgRev: the
// daemon's config revision our copy matches (see adoptOutsideChanges); null
// until the first poll checks, since cfg and state load in parallel.
let savePending = false, cfgRev = null;

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "PUT", body: JSON.stringify(body), headers: { "Content-Type": "application/json" },
  };
  const r = await fetch(path, opts);
  const j = await r.json();
  // e.g. 503 "Louver isn't running" from the desktop app, when the daemon is down
  if (!r.ok) throw Object.assign(new Error(j.error || r.statusText), { status: r.status });
  return j;
}

function toast(msg) {
  const t = $("toast");
  t.firstElementChild.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.add("hidden"), 5000);
}
$("toast-close").addEventListener("click", () => { clearTimeout(toast.timer); $("toast").classList.add("hidden"); });

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
  if (z.kind === "aura") return "Motherboard RGB";
  return "Strimer Plus";
}
function zoneState(z) {
  if (z.kind === "hub") return state?.hub.groups[z.i];
  if (z.kind === "mb") return state?.motherboard.headers.find((h) => h.index === z.i);
  if (z.kind === "aura") return state?.motherboard_rgb;
  return state?.strimer;
}
// The Strimer and motherboard RGB are lighting only: no speed, no duty.
const lightOnly = (z) => z.kind === "strimer" || z.kind === "aura";
// The channels Louver drives (skipped ones carry a note: see auraSkipped).
const auraChannels = () => M.auraUsable(state?.motherboard_rgb?.channels);

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
// motherboard header, the Strimer's or the motherboard RGB's bar.
function zonePreview(z) {
  if (z.kind === "hub") return M.hubFanPreview(cfg, meta, z.i, 0);
  if (z.kind === "mb") return "mb";
  if (z.kind === "aura") return barCss(M.auraBar(cfg, meta, auraChannels()));
  return strimerBar();
}

// ---------------- sidebar ----------------
// The buttons are updated in place, keyed by zone, so the 1 s refresh can't
// swallow a click that lands mid-rebuild or drop keyboard focus.
function zoneButton(z) {
  const glyph = lightOnly(z) ? el("span", { class: "strimer-glyph" }) : fanGlyph(null);
  return el("button", { class: "zone", "data-zone": zoneKey(z) },
    el("span", { class: "zname" }, el("b"), el("small")), el("i", { class: "zwarn hidden" }), glyph);
}
function fillZoneButton(b, z) {
  const s = zoneState(z);
  let sub;
  if (z.kind === "strimer") sub = `${s.ports} ports`;
  else if (z.kind === "aura") {
    const n = auraChannels().length;
    sub = `${n} channel${n === 1 ? "" : "s"} · ASUS Aura`;
  }
  else {
    const c = z.kind === "hub" ? cfg.hub.groups[z.i] : mbCfg(z.i) || { mode: "bios" };
    sub = (z.kind === "hub" ? `${c.fans} fan${c.fans === 1 ? "" : "s"} · ` : "") + `${s?.rpm ?? 0} rpm`
      + (c.mode === "bios" ? " · BIOS" : c.mode === "mb_sync" ? " · MB" : "");
  }
  const [label, warnEl, glyph] = b.children;
  if (lightOnly(z)) glyph.style.background = zonePreview(z);
  else setFanGlyph(glyph, zonePreview(z));
  label.children[0].textContent = zoneName(z);
  label.children[1].textContent = sub;
  const warn = s?.failsafe ? ["bad", "No temperature: running at 100%"]
    : s?.missing?.length ? ["warn", "Unreadable: " + s.missing.join(", ")] : null;
  warnEl.className = "zwarn" + (warn ? " " + warn[0] : " hidden");
  if (warn) { warnEl.title = warn[1]; warnEl.setAttribute("aria-label", warn[1]); }
  b.classList.toggle("active", onZone() && sameZone(z, sel));
  if (b.classList.contains("active")) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
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

// Themes apply to every zone; editing any lighting makes the look custom.
function renderThemes() {
  $("themes").replaceChildren(...meta.themes.map((t) => el("button", {
    class: "theme" + (cfg.theme === t.name ? " on" : ""), "aria-pressed": String(cfg.theme === t.name),
    onclick: async () => {
      try { cfg = await api("/api/theme", { name: t.name }); renderAll(); }
      catch (e) { toast("Theme failed: " + e.message); }
    },
  }, el("span", { "aria-hidden": "true" }, ...t.swatch.slice(0, 4).map((c) => {
    const d = el("i"); d.style.background = c; return d;
  })), t.name)));
  $("theme-name").textContent = `· ${cfg.theme || "custom look"} · applies to every zone`;
}

function renderStatus() {
  const h = state.hub, s = state.strimer, m = state.motherboard, a = state.motherboard_rgb;
  const row = (name, detail, status, cls = "") => el("div", { class: "dev" },
    el("span", {}, el("b", {}, name), el("small", {}, detail)), el("i", { class: cls }, "● " + status));
  const linked = (d) => (d.connected ? ["firmware " + (d.firmware || "?"), "linked"] : ["not connected", "offline", "off"]);
  $("devices").replaceChildren(
    row("UNI HUB AL V2", ...linked(h)),
    row("Strimer Plus", ...linked(s)),
    ...(m?.present ? [row("Motherboard", m.chip, ...(m.error ? ["error", "bad"] : m.writable ? ["control"] : ["read-only", "off"]))] : []),
    ...(a?.enabled ? [row("Motherboard RGB", ...(a.error ? ["ASUS Aura", "error", "bad"] : linked(a)))] : []));
  renderAuraStatus();
  const b = $("banner");
  const msg = state.error || (!h.connected ? "UNI HUB not connected — check the USB cable and the udev rule (see README)." : "")
    || (onZone() && sel.kind === "mb" ? m?.error || (m && !m.writable ? "Read-only: install the hwmon udev rule (README) to control motherboard headers." : "") : "");
  if (b.textContent !== msg) b.textContent = msg;  // a live region: rewrites get re-announced
  b.classList.toggle("hidden", !msg);
}

function renderView() {
  $("view-zone").classList.toggle("hidden", !onZone());
  $("view-empty").classList.toggle("hidden", view !== "zone" || sel !== null);
  $("view-settings").classList.toggle("hidden", view !== "settings");
}
$("empty-settings").addEventListener("click", () => {
  cfg.ui.show_empty_ports = true;
  save(0);
  renderAll();
});
gillRow($("empty-gills"), 7, 72);  // resting gills, drawn once

// ---------------- zone page ----------------
function renderZoneHead() {
  const z = sel, name = $("zone-name");
  name.value = zoneName(z);
  name.disabled = lightOnly(z);
  if (z.kind === "aura") {
    const a = state?.motherboard_rgb, n = auraChannels().length;
    $("zone-sub").textContent = `${n} channel${n === 1 ? "" : "s"} · firmware ${a?.firmware || "?"} · ${a?.name || "ASUS Aura"}`;
  } else if (z.kind === "hub") {
    const n = cfg.hub.groups[z.i].fans;
    $("zone-sub").textContent = `${n} fan${n === 1 ? "" : "s"} · hub port ${z.i + 1} · UNI HUB AL V2`;
  } else if (z.kind === "mb") {
    $("zone-sub").textContent = `motherboard header ${z.i}` + (state?.motherboard.chip ? ` · ${state.motherboard.chip}` : "");
  } else {
    $("zone-sub").textContent = `${state?.strimer.ports ?? 12} ports · firmware ${state?.strimer.firmware || "?"}`;
  }
  renderZoneStats();
  fitName();  // after the stats: the duty meter sets how wide the title column is
}

function renderZoneStats() {
  const box = $("zone-stats");
  $("duty-meter").classList.toggle("hidden", lightOnly(sel));
  if (lightOnly(sel)) { box.replaceChildren(); return; }
  const s = zoneState(sel) || {};
  const c = selCfg();
  const sensor = meta.sensors.find((x) => x.id === c.sensor);
  const pill = (big, small, cls = "") => el("span", { class: "pill " + cls }, el("b", {}, big), small);
  // BIOS and MB PWM sync ignore Louver's sensor, so don't show one.
  const ownSensor = c.mode !== "bios" && c.mode !== "mb_sync";
  box.replaceChildren(
    pill(String(s.rpm ?? 0), "rpm"),
    ...(ownSensor ? [pill(s.temp == null ? "—" : s.temp.toFixed(0) + "°", sensor ? sensor.label : c.sensor, "temp")] : []),
    ...(s.failsafe ? [el("span", { class: "pill bad" }, "No temperature: running at 100%")]
      : s.missing?.length ? [el("span", { class: "pill warn" }, "Unreadable: " + s.missing.join(", "))] : []),
  );
  renderDuty(s, c);
  $("speed-foot").textContent = M.speedFoot(sel.kind, c.mode, s);
}

// The duty meter: 12 gills, one opens per ~8% of duty. Updated in place so
// the slats animate between polls instead of being rebuilt.
const GILLS = 12;
function gillRow(box, n, height) {
  if (box.childElementCount === n) return;
  box.replaceChildren(...Array.from({ length: n }, (_, i) => {
    const g = el("span", { class: "slat" });
    g.style.height = Math.round(height - Math.abs(i - (n - 1) / 2) * 4) + "px";
    g.style.animationDelay = (i * 0.12).toFixed(2) + "s";
    return g;
  }));
}
function renderDuty(s, c) {
  const box = $("duty-gills"), meter = $("duty-meter"), pct = s.percent;
  gillRow(box, GILLS, 58);
  const open = pct == null ? 0 : Math.round(clamp(pct, 0, 100) / 100 * GILLS);
  [...box.children].forEach((g, i) => g.classList.toggle("open", i < open));
  const label = pct == null ? (c.mode === "bios" ? "BIOS" : "MB") : pct + "%";
  $("duty-label").replaceChildren("Duty ", el("b", {}, label), pct == null ? "" : ` · ${open}/${GILLS} slats open`);
  meter.classList.toggle("alarm", !!s.failsafe);
  if (pct == null) meter.removeAttribute("aria-valuenow"); else meter.setAttribute("aria-valuenow", pct);
  meter.setAttribute("aria-valuetext", label);
}

// A fan cell: the outer ring's colours around spinning blades, the inner
// ring's at the hub. Blades turn faster the faster the port spins.
const BLADES = "M32 26.8 Q31.5 18.9 42.8 18.2 A17.5 17.5 0 0 1 46.6 22.4 Q45 26.9 34.9 27.7Z M37 30.4 Q44.3 27.5 48.4 38 A17.5 17.5 0 0 1 45.7 42.9 Q40.9 42.8 37 33.4Z M35.1 36.2 Q40.1 42.3 31.4 49.5 A17.5 17.5 0 0 1 25.8 48.4 Q24.5 43.8 32.2 37.2Z M28.9 36.2 Q24.7 42.9 15.2 36.8 A17.5 17.5 0 0 1 14.5 31.2 Q18.5 28.5 27.1 33.8Z M27.1 30.4 Q19.4 28.4 22.2 17.5 A17.5 17.5 0 0 1 27.4 15.1 Q31.2 18 28.8 27.9Z";
function fanCell(big) {
  const blades = sv("svg", { viewBox: "0 0 64 64", "aria-hidden": "true" });
  blades.append(sv("path", { d: BLADES }));
  return el("span", { class: "cell" + (big ? " big" : "") }, el("span", { class: "ring" }), blades, el("span", { class: "hub" }));
}
function setCell(cell, preview, rpm) {
  setFanGlyph(cell, preview);
  const blades = cell.children[1];
  // Rounded so small rpm jitter doesn't keep restarting the animation.
  const dur = rpm > 0 ? (Math.round(clamp(1800 / rpm, 0.3, 4) * 10) / 10) + "s" : "";
  blades.classList.toggle("spin", !!dur);
  if (dur && blades.style.getPropertyValue("--dur") !== dur) blades.style.setProperty("--dur", dur);
}
// Keep a list's children (and their running animations) across refreshes.
function syncList(box, n, make, fill) {
  if (box.childElementCount !== n) box.replaceChildren(...Array.from({ length: n }, make));
  [...box.children].forEach(fill);
}

function renderZoneFans() {
  const isHub = sel.kind === "hub";
  $("fans-panel").classList.toggle("hidden", !isHub);
  const prev = shownTab() === "light" ? $("light-preview") : null;
  const tn = $("temp-now");
  if (tn && prev) {
    const L = sel.kind === "hub" ? cfg.hub.lighting[sel.i] : cfg.strimer.per_port ? cfg.strimer.ports[strimerPort] : cfg.strimer.lighting;
    if (L?.mode === "Temperature") tn.textContent = tempNow(L);
  }
  if (isHub) {
    const n = cfg.hub.groups[sel.i].fans, rpm = zoneState(sel)?.rpm ?? 0;
    $("fans-empty").classList.toggle("hidden", n > 0);
    syncList($("zone-fans"), n, () => el("div", { class: "fan-row" }, fanCell(false), el("div", {}, el("span"), el("small"))), (row, f) => {
      setCell(row.children[0], M.hubFanPreview(cfg, meta, sel.i, f), rpm);
      row.children[1].children[0].textContent = `Fan ${f + 1}`;
      row.children[1].children[1].textContent = `port · ${rpm} rpm`;
    });
    if (prev) syncList(prev, Math.max(1, n), () => el("figure", {}, fanCell(true), el("figcaption")), (fig, f) => {
      setCell(fig.children[0], M.hubFanPreview(cfg, meta, sel.i, f), 0);
      fig.children[1].textContent = `Fan ${f + 1}`;
    });
  } else if (sel.kind === "strimer" && prev) {
    const ports = strimerPorts();
    syncList(prev, state?.strimer.ports || 12, () => el("span"), (b, k) => {
      b.style.background = ports === null ? MB_BAR : barCss(ports[k]);
    });
  }
}

// Hub ports have Speed and Lighting tabs; a header is speed only, the Strimer
// and motherboard RGB lighting only.
let tab = "speed";
const shownTab = () => (sel.kind === "mb" ? "speed" : lightOnly(sel) ? "light" : tab);
function renderZone() {
  renderZoneHead();
  const t = shownTab();
  $("zone-tabs").classList.toggle("hidden", sel.kind !== "hub");
  for (const b of $("zone-tabs").children) {
    b.setAttribute("aria-selected", String(b.dataset.tab === t));
    b.tabIndex = b.dataset.tab === t ? 0 : -1;
  }
  $("tab-speed").classList.toggle("hidden", t !== "speed");
  $("tab-light").classList.toggle("hidden", t !== "light");
  for (const id of ["tab-speed", "tab-light"]) {
    if (sel.kind === "hub") $(id).setAttribute("role", "tabpanel"); else $(id).removeAttribute("role");
  }
  if (t === "speed") renderSpeed(); else renderLighting();
  renderZoneFans();
}
$("zone-tabs").addEventListener("click", (e) => {
  const b = e.target.closest("[data-tab]");
  if (b) { tab = b.dataset.tab; renderZone(); }
});
$("zone-tabs").addEventListener("keydown", (e) => {
  const next = { ArrowLeft: tab === "speed" ? "light" : "speed", ArrowRight: tab === "speed" ? "light" : "speed", Home: "speed", End: "light" }[e.key];
  if (!next) return;
  e.preventDefault();
  tab = next;
  renderZone();
  $("tab-btn-" + tab).focus();
});

// The big zone name shrinks to fit (an input can't wrap): Unbounded 800 runs
// about 0.75em per character.
function fitName() {
  const name = $("zone-name"), w = name.clientWidth - 20;
  if (w <= 0) { name.style.fontSize = ""; return; }
  let size = clamp(Math.floor(w / (Math.max(name.value.length, 1) * 0.75)), 22, 46);
  name.style.fontSize = size + "px";
  // Wide glyphs (W, M) beat the estimate: step down until nothing scrolls.
  while (size > 22 && name.scrollWidth > name.clientWidth) name.style.fontSize = (size -= 2) + "px";
}
$("zone-name").addEventListener("input", fitName);
window.addEventListener("resize", () => { if (onZone()) { fitName(); if (hasSpeed() && !dragging) drawCurve(); } });
$("zone-name").addEventListener("change", (e) => {
  if (!sel || lightOnly(sel)) return;
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
    h = { index, name: `Header ${index}`, mode: "bios", manual_percent: 50, sensor: "cpu", curve: meta.presets.standard.map((p) => [...p]), pump: false, min_percent: 0 };
    cfg.motherboard.headers.push(h);
  }
  return h;
}
const selCfg = () => (sel.kind === "hub" ? cfg.hub.groups[sel.i] : mbEntry(sel.i));
const selState = () => zoneState(sel);
const hasSpeed = () => onZone() && !lightOnly(sel);

function renderSpeed() {
  const g = selCfg();
  const isMb = sel.kind === "mb";
  $("port-fans-label").classList.toggle("hidden", isMb);
  $("port-sensor-label").classList.toggle("hidden", g.mode === "bios" || g.mode === "mb_sync");
  $("mb-hint").classList.toggle("hidden", !isMb);
  $("sync-note").classList.toggle("hidden", g.mode !== "mb_sync");
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

  const readOnly = isMb && !!state?.motherboard && !state.motherboard.writable;
  $("mb-opts").classList.toggle("hidden", !isMb);
  if (isMb) {
    $("mb-pump").checked = !!g.pump;
    $("mb-min-label").classList.toggle("hidden", !M.showsFloor(sel.kind, g));
    $("mb-min").value = g.min_percent ?? 0;
    $("mb-pump").disabled = $("mb-min").disabled = readOnly;
  }
  $("mode-chips").replaceChildren(...M.speedModes(meta, sel.kind, g).map((m) => el("button", {
    class: "chip" + (m === g.mode ? " on" : ""),
    ...(readOnly ? { disabled: "", title: "Read-only: install the hwmon udev rule (README)" } : {}),
    onclick: () => {
      const cur = selCfg();
      if (m === "curve" && cur.mode in meta.presets) cur.curve = meta.presets[cur.mode].map((p) => [...p]);
      cur.mode = m; save(0); renderSpeed(); renderZoneStats(); renderZoneList();
    },
    "aria-pressed": String(m === g.mode),
  }, MODE_LABELS[m])));

  $("manual-box").classList.toggle("hidden", g.mode !== "manual");
  $("manual-range").min = isMb ? (g.min_percent ?? 0) : 0;  // a header's floor applies in manual mode too
  $("manual-range").value = g.manual_percent;
  $("manual-val").textContent = g.manual_percent + "%";
  $("curve-box").classList.toggle("hidden", g.mode === "manual" || g.mode === "mb_sync" || g.mode === "bios");
  $("curve-hint").textContent = g.mode === "curve"
    ? "Drag a point to move it · double-click the chart to add one · right-click a point to remove it"
    : "Preset curve. Pick Custom curve to start editing from this shape.";
  drawCurve();
}

$("port-sensor").addEventListener("change", (e) => { selCfg().sensor = e.target.value; save(0); renderZoneStats(); });
$("port-fans").addEventListener("change", (e) => { selCfg().fans = +e.target.value; save(0); renderAll(); });
$("mb-pump").addEventListener("change", (e) => {
  const g = selCfg();
  Object.assign(g, M.pumpChange(g, e.target.checked, meta.pump_modes));
  save(0); renderSpeed(); renderZoneStats(); renderZoneList();
});
$("mb-min").addEventListener("change", (e) => {
  const g = selCfg();
  Object.assign(g, M.floorChange(g, e.target.value));
  save(0); renderSpeed();
});
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
// W and H follow the chart's size on screen (see drawCurve), so text and
// points keep their pixel size and stay round at any width.
let W = 640, H = 300;
const PAD = { l: 40, r: 14, t: 14, b: 28 };
const tx = (t) => PAD.l + (t - T_MIN) / (T_MAX - T_MIN) * (W - PAD.l - PAD.r);
const ty = (p) => H - PAD.b - p / 100 * (H - PAD.t - PAD.b);
const fx = (x) => T_MIN + (x - PAD.l) / (W - PAD.l - PAD.r) * (T_MAX - T_MIN);
const fy = (y) => (H - PAD.b - y) / (H - PAD.t - PAD.b) * 100;
const NS = "http://www.w3.org/2000/svg";
const sv = (tag, attrs, ...kids) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); e.append(...kids); return e; };

function curvePoints() {
  const g = selCfg();
  return g.mode === "curve" ? g.curve : (meta.presets[g.mode] || g.curve);
}

function drawCurve() {
  const svg = $("curve");
  const box = svg.getBoundingClientRect();
  if (box.width && box.height) {  // 0 while its tab is hidden: keep the last size
    W = Math.round(box.width); H = Math.round(box.height);
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  }
  const g = selCfg();
  const editable = g.mode === "curve";
  svg.classList.toggle("edit", editable);
  // In chart units: a flat curve (Full) has a zero-height box, and an
  // objectBoundingBox gradient on it isn't painted at all.
  const grad = sv("linearGradient", { id: "curve-grad", gradientUnits: "userSpaceOnUse", x1: tx(T_MIN), y1: 0, x2: tx(T_MAX), y2: 0 });
  grad.append(sv("stop", { offset: 0, style: "stop-color: var(--accent)" }), sv("stop", { offset: 1, style: "stop-color: var(--violet)" }));
  svg.replaceChildren(sv("defs", {}, grad));
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

  // "Now": a line at the sensor's temperature and a pulsing dot at the duty.
  // The dot lives outside the SVG so redrawing doesn't restart its pulse.
  const s = selState(), now = $("curve-now");
  const showNow = !!s && s.temp != null && s.percent != null;
  if (s && s.temp != null) {
    const t = clamp(s.temp, T_MIN, T_MAX);
    svg.append(sv("line", { class: "nowline", x1: tx(t), x2: tx(t), y1: PAD.t, y2: H - PAD.b }));
    if (showNow) { now.style.left = tx(t) / W * 100 + "%"; now.style.top = ty(clamp(s.percent, 0, 100)) / H * 100 + "%"; }
  }
  now.classList.toggle("hidden", !showNow);
  pts.forEach(([t, p], i) => {
    const c = sv("circle", { class: "pt", cx: tx(t), cy: ty(p), r: editable ? 6.5 : 5 });
    c.append(sv("title", {}));
    c.firstChild.textContent = `${t}° → ${p}%`;
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

// A row of gills to pick a level: every slat up to the value opens. Levels
// below 0 (brightness "Off") open nothing.
function slats(name, min, max, value, labels, onpick) {
  const row = el("div", { class: "slats", role: "group", "aria-label": name });
  for (let v = min; v <= max; v++) {
    const g = el("span", { class: "slat" + (v >= 0 && v <= value ? " open" : "") });
    g.style.height = 16 + (v - min) * 2 + "px";
    row.append(el("button", { "aria-label": labels[v] ?? String(v), "aria-pressed": String(v === value), title: labels[v] ?? String(v), onclick: () => onpick(v) }, g));
  }
  return el("div", { class: "field" }, el("span", {}, name, el("b", {}, labels[value] ?? String(value))), row);
}

function field(label, ...kids) { return el("div", { class: "field" }, el("span", {}, label), el("div", {}, ...kids)); }

function seg(options, current, onpick) {
  return el("div", { class: "seg", role: "group" }, ...options.map(([v, label]) =>
    el("button", { class: v === current ? "on" : "", "aria-pressed": String(v === current), onclick: () => onpick(v) }, label)));
}

// A round colour chip with the native picker laid over it.
function swatch(value, label, onchange) {
  const inp = el("input", { type: "color", value, "aria-label": label });
  const chip = el("i", {}, inp);
  chip.style.setProperty("--c", value);
  inp.addEventListener("input", () => chip.style.setProperty("--c", inp.value));
  inp.addEventListener("change", () => onchange(inp.value));
  return el("label", { class: "swatch" }, chip, el("small", {}, label));
}

function colorInputs(colors, count, labelFn, onchange, { addRemove = false } = {}) {
  const box = el("div", { class: "colors" });
  for (let i = 0; i < count; i++) {
    const v = colors[i] || colors[colors.length - 1] || "#ffffff";
    box.append(swatch(v, labelFn(i), (c) => {
      const next = [...colors];
      while (next.length < i) next.push(next[next.length - 1] || "#ffffff");
      next[i] = c;
      onchange(next.slice(0, Math.max(count, next.length)));
    }));
  }
  if (addRemove) {
    if (count < 6) box.append(el("button", { class: "btn round", "aria-label": "Add colour stop", onclick: () => onchange([...colors.slice(0, count), colors[count - 1] || "#ffffff"]) }, "+"));
    if (count > 2) box.append(el("button", { class: "btn round", "aria-label": "Remove last colour stop", onclick: () => onchange(colors.slice(0, count - 1)) }, "−"));
  }
  return box;
}

// Where the Temperature effect's sensor sits between its blue and red ends.
// Refreshed with the preview (renderZoneFans), since sensors keep moving.
function tempNow(L) {
  const now = meta.sensors.find((x) => x.id === L.sensor)?.value;
  if (now == null) return "No reading from this sensor: the LEDs show magenta";
  const [lo, hi] = L.temp_range;
  return `Now ${now.toFixed(0)}° → ${Math.round(clamp((now - lo) / Math.max(1e-6, hi - lo), 0, 1) * 100)}% of the way to red`;
}
const RING_NAMES = { both: "both rings", inner: "the inner ring", outer: "the outer ring" };

// kind: "hub" | "strimer". ctx: { entry, set(patch), fans (hub only) }
function lightingEditor(kind, ctx) {
  const L = ctx.entry;
  const modes = kind === "hub" ? meta.hub_lighting_modes : meta.strimer_lighting_modes;
  const spec = modes.find((m) => m.name === L.mode) || modes[0];
  const ring = L.ring ?? "both";
  const hw = modes.filter((m) => !m.software && (kind !== "hub" || ring === "both" || m[ring]));
  const sw = modes.filter((m) => m.software);
  const effectBtn = (m) => el("button", { class: "effect" + (m.name === L.mode ? " on" : ""), "aria-pressed": String(m.name === L.mode), onclick: () => ctx.set({ mode: m.name }) },
    m.name, el("small", {}, M.effectTag(kind, m)));

  // preview (or the LED painter, for Custom) and its parameters, side by side
  const head = el("div", { class: "panel-head" }, el("h2", {}, "Preview ", el("small", {}, "· " + L.mode)));
  if (kind === "hub" && L.mode !== "Custom") {
    head.append(seg([["both", "Both rings"], ["inner", "Inner"], ["outer", "Outer"]], L.ring, (v) => ctx.set({ ring: v })));
  }
  const preview = el("section", { class: "panel wide" }, head);
  const params = el("section", { class: "panel alt" }, el("h2", {}, "Parameters"));
  if (L.mode === "Custom") {
    const p = painter(kind, ctx);
    preview.append(p.area);
    params.append(p.tools);
  } else {
    preview.append(kind === "hub" ? el("div", { class: "preview", id: "light-preview" })
      : el("div", { class: "preview" }, el("div", { class: "port-bars", id: "light-preview" })));
    preview.append(el("p", { class: "hint" }, kind === "hub"
      ? "Outer ring around the blades, inner ring at the hub. Animated effects show their colours only."
      : "One bar per Strimer port. Animated effects show their colours only."));
  }
  // colours
  if (L.mode === "Gradient") {
    const n = Math.max(2, Math.min(6, L.colors.length));
    params.append(field("Colour stops", colorInputs(L.colors, n, (i) => `Stop ${i + 1}`, (c) => ctx.set({ colors: c }), { addRemove: true })));
    params.append(field("Spread", seg([[true, kind === "hub" ? "Across all ports" : "Across ports"], [false, kind === "hub" ? "Within this port" : "Along each port"]],
      !!L.span_all, (v) => ctx.set({ span_all: v }))));
  } else if (L.mode === "Temperature") {
    const sens = el("select", {}, ...meta.sensors.map((s) => el("option", { value: s.id }, s.label)));
    sens.value = L.sensor;
    sens.addEventListener("change", () => ctx.set({ sensor: sens.value }));
    const lo = el("input", { type: "number", value: L.temp_range[0], min: 0, max: 110, "aria-label": "Blue at (°C)" });
    const hi = el("input", { type: "number", value: L.temp_range[1], min: 1, max: 120, "aria-label": "Red at (°C)" });
    const upd = () => { if (+lo.value < +hi.value) ctx.set({ temp_range: [+lo.value, +hi.value] }); else toast("Cool temperature must be below hot"); };
    lo.addEventListener("change", upd); hi.addEventListener("change", upd);
    params.append(field("Sensor", sens));
    params.append(field("Range (°C)", el("div", { class: "row temp-range" }, el("span", { class: "thermo cold" }, "blue at"), lo, el("span", { class: "thermo hot" }, "red at"), hi)),
      el("div", { class: "thermo-bar", "aria-hidden": "true" }),
      el("span", { class: "hint", id: "temp-now" }, tempNow(L)));
  } else if (L.mode === "Custom") {
    // the painter's tools are already in
  } else if (spec.max_colors) {
    const perFan = kind === "hub" && (L.mode === "Static" || L.mode === "Breathing");
    const n = perFan ? Math.max(1, ctx.fans) : spec.max_colors;
    const lab = perFan ? (i) => `Fan ${i + 1}` : M.isCorner(kind, spec.name) ? (i) => ["Top", "Right", "Bottom", "Left"][i] : (i) => `#${i + 1}`;
    params.append(field(perFan ? "Fan colours" : "Colours", colorInputs(L.colors, n, lab, (c) => ctx.set({ colors: c }))));
  } else {
    params.append(field("Colours", el("span", { class: "none" }, L.mode === "Off" ? "LEDs off" : "This effect uses built-in colours")));
  }
  if (!spec.software && L.mode !== "Off") {
    params.append(slats("Speed", 0, 4, L.speed, SPEED_LABELS, (v) => ctx.set({ speed: v })));
    params.append(field("Direction", seg([["cw", "Clockwise"], ["ccw", "Counter-clockwise"]], L.direction, (v) => ctx.set({ direction: v }))));
  }
  if (L.mode !== "Off") params.append(slats("Brightness", -1, 4, L.brightness, BRIGHT_LABELS, (v) => ctx.set({ brightness: v })));

  const effects = el("section", { class: "panel" },
    el("h2", {}, "Hardware effects ", el("small", {}, `· ${hw.length}` + (kind === "hub" ? ` for ${RING_NAMES[ring] || ring}` : ""))),
    el("div", { class: "effects" }, ...hw.map(effectBtn)),
    el("h2", {}, "Software effects ", el("small", {}, "· rendered per-LED by Louver")),
    el("div", { class: "effects" }, ...sw.map(effectBtn)));
  return [el("div", { class: "split" }, preview, params), effects];
}

// ---- per-LED painter: tools go in Parameters, the LEDs in the preview ----
function painter(kind, ctx) {
  const L = ctx.entry;
  const custom = JSON.parse(JSON.stringify(L.custom || {}));
  const rings = kind === "hub" ? [["outer", HUB_OUTER], ["inner", HUB_INNER]] : [["leds", STRIMER_LEDS]];
  for (const [ring, n] of rings) {
    const total = (kind === "hub" ? HUB_FANS : 1) * n;
    custom[ring] = Array.from({ length: total }, (_, i) => (custom[ring] || [])[i] || "#000000");
  }
  const commit = () => ctx.set({ custom: JSON.parse(JSON.stringify(custom)) }, { rerender: false, delay: 300 });
  const area = el("div", { class: "painter" });
  const repaint = () => area.querySelectorAll("[data-ring]").forEach((d) => { d.setAttribute("fill", custom[d.dataset.ring][+d.dataset.idx]); });

  const paintLed = (ring, idx) => {
    const n = rings.find((r) => r[0] === ring)[1];
    const fan = Math.floor(idx / n);
    const targets = paintTool === "led" ? [idx]
      : paintTool === "ring" ? Array.from({ length: n }, (_, k) => fan * n + k)
      : null;
    if (targets) for (const t of targets) custom[ring][t] = brush;
    else for (const [r, rn] of rings) for (let k = 0; k < rn; k++) custom[r][fan * rn + k] = brush;  // whole fan
    repaint();
    commit();
  };

  const tools = kind === "hub" ? [["led", "LED"], ["ring", "Ring"], ["fan", "Whole fan"]] : [["led", "LED"], ["ring", "Whole port"]];
  const toolSeg = () => seg(tools, paintTool, (v) => { paintTool = v; toolBox.replaceChildren(toolSeg()); });
  const toolBox = el("div", {}, toolSeg());
  const fillAll = el("button", { class: "btn small", onclick: () => {
    for (const [r] of rings) custom[r] = custom[r].map(() => brush);
    repaint();
    commit();
  } }, "Fill all");
  const brushBox = swatch(brush, "Brush", () => {});
  brushBox.querySelector("input").addEventListener("input", (e) => { brush = e.target.value; });
  const toolsEl = el("div", { class: "field" },
    el("div", { class: "row between" }, brushBox, fillAll),
    el("span", {}, "Paint"), toolBox);

  const grid = el("div", { class: "fans-grid" });
  const dot = (ring, idx, cx, cy, r) => {
    const c = sv("circle", { cx, cy, r, fill: custom[ring][idx], class: "led", "data-ring": ring, "data-idx": idx });
    c.addEventListener("pointerdown", (e) => { e.preventDefault(); painting = true; paintLed(ring, idx); });
    c.addEventListener("pointerenter", () => { if (painting && paintTool === "led") paintLed(ring, idx); });
    return c;
  };
  if (kind === "hub") {
    const fans = Math.max(1, ctx.fans);
    for (let f = 0; f < fans; f++) {
      const s = sv("svg", { viewBox: "0 0 120 120", class: "fan" });
      s.append(sv("circle", { cx: 60, cy: 60, r: 57, class: "fan-body" }));
      s.append(sv("circle", { cx: 60, cy: 60, r: 16, class: "fan-hub" }));
      // Outer: 12 LEDs around the frame, 3 per side (corners on AL fans). Inner: 8 around the hub.
      for (let k = 0; k < HUB_OUTER; k++) {
        const a = -Math.PI / 2 + (k + 0.5) * 2 * Math.PI / HUB_OUTER;
        s.append(dot("outer", f * HUB_OUTER + k, 60 + 46 * Math.cos(a), 60 + 46 * Math.sin(a), 6.5));
      }
      for (let k = 0; k < HUB_INNER; k++) {
        const a = -Math.PI / 2 + (k + 0.5) * 2 * Math.PI / HUB_INNER;
        s.append(dot("inner", f * HUB_INNER + k, 60 + 27 * Math.cos(a), 60 + 27 * Math.sin(a), 5.5));
      }
      grid.append(el("div", { class: "fan-wrap" }, s, el("small", {}, `Fan ${f + 1}`)));
    }
  } else {
    const s = sv("svg", { viewBox: `0 0 ${STRIMER_LEDS * 22 + 8} 30`, class: "strip" });
    for (let k = 0; k < STRIMER_LEDS; k++) s.append(dot("leds", k, 15 + k * 22, 15, 8));
    grid.append(s);
  }
  area.append(grid, el("p", { class: "hint" }, kind === "hub"
    ? "Click or drag to paint. Outer ring = the 12 frame LEDs, inner ring = the 8 LEDs around the hub. LED 1 is drawn at the top; the physical start position may differ on your fans."
    : "Click or drag to paint the 27 LEDs of a port."));
  return { tools: toolsEl, area };
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
  const tools = $("light-tools"), box = $("light-controls"), fx = $("light-effects"), hint = $("mbsync-hint");
  if (sel.kind === "aura") { renderAuraLighting(tools, box, fx, hint); return; }
  if (sel.kind === "hub") {
    const i = sel.i, synced = cfg.hub.mb_rgb_sync;
    tools.replaceChildren(
      toggle(el("span", {}, "Follow motherboard ARGB ", el("small", { class: "hint" }, "· all hub ports")), synced, (v) => { cfg.hub.mb_rgb_sync = v; editLight(); }),
      ...(synced ? [] : [el("button", { class: "btn", onclick: () => {
        for (let k = 0; k < cfg.hub.lighting.length; k++) if (k !== i) cfg.hub.lighting[k] = JSON.parse(JSON.stringify(cfg.hub.lighting[i]));
        editLight();
      } }, "Use this look on every hub port")]));
    hint.textContent = "The motherboard's ARGB header is driving every hub port's lighting. Turn that off to set it here.";
    hint.classList.toggle("hidden", !synced);
    box.classList.toggle("hidden", synced);
    fx.classList.toggle("hidden", synced);
    if (synced) { box.replaceChildren(); fx.replaceChildren(); return; }
    const [editor, effects] = lightingEditor("hub", {
      entry: cfg.hub.lighting[i],
      fans: cfg.hub.groups[i].fans,
      // Look the entry up at edit time: cfg may have been replaced by a save.
      set: (patch, opts) => { cfg.hub.lighting[i] = { ...cfg.hub.lighting[i], ...patch }; editLight(opts); },
    });
    box.replaceChildren(editor);
    fx.replaceChildren(effects);
    renderZoneFans();  // fills the preview
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
  fx.classList.toggle("hidden", s.mb_rgb_sync);
  if (s.mb_rgb_sync) { box.replaceChildren(); fx.replaceChildren(); return; }
  const [editor, effects] = lightingEditor("strimer", {
    entry: s.per_port ? s.ports[strimerPort] : s.lighting,
    set: (patch, opts) => {
      const st = cfg.strimer;
      if (st.per_port) st.ports[strimerPort] = { ...st.ports[strimerPort], ...patch };
      else st.lighting = { ...st.lighting, ...patch };
      editLight(opts);
    },
  });
  fx.replaceChildren(effects);
  box.replaceChildren(
    ...(s.per_port ? [el("div", { class: "bar" }, field("Editing port", seg(Array.from({ length: ports }, (_, k) => [k, String(k + 1)]), strimerPort,
      (v) => { strimerPort = v; renderLighting(); })))] : []),
    editor);
  renderZoneFans();
}

// ---- motherboard RGB: the Aura controller's own effects, one look or one per channel ----
// One panel: a bar per channel it covers, the effect chips, the colour.
function auraPanel(title, sub, L, chans, set, alt) {
  const modes = meta.aura_lighting_modes;
  const spec = modes.find((m) => m.name === L.mode) || { name: L.mode, max_colors: 0 };
  const bars = el("div", { class: "port-bars" }, ...chans.map((ch) => {
    const b = el("span", { title: ch.name });
    b.style.background = barCss(M.auraPreview(M.auraLook(cfg, ch.key), meta));
    return b;
  }));
  return el("section", { class: "panel" + (alt ? " alt" : "") },
    el("div", { class: "panel-head" }, el("h2", {}, title, el("small", {}, " · " + sub))),
    el("div", { class: "preview" }, bars),
    el("div", { class: "chips", role: "group", "aria-label": title + " effect" }, ...modes.map((m) => el("button", {
      class: "chip" + (m.name === L.mode ? " on" : ""), "aria-pressed": String(m.name === L.mode), title: M.effectTag("aura", m),
      onclick: () => set({ mode: m.name }),
    }, m.name))),
    spec.max_colors
      ? field("Colour", colorInputs(L.colors, 1, () => "Colour", (c) => set({ colors: c.slice(0, 1) })))
      : field("Colour", el("span", { class: "none" }, L.mode === "Off" ? "LEDs off" : "This effect uses built-in colours")));
}

function renderAuraLighting(tools, box, fx, hint) {
  const m = cfg.motherboard_rgb, chans = auraChannels(), skipped = M.auraSkipped(state?.motherboard_rgb?.channels);
  hint.textContent = skipped ? "Louver doesn't drive: " + skipped + ". The board's config table describes more than one effect report holds." : "";
  hint.classList.toggle("hidden", !skipped);
  box.classList.remove("hidden");
  fx.classList.add("hidden");
  fx.replaceChildren();
  tools.replaceChildren(
    toggle("Per-channel control", m.per_channel, (v) => {
      const mr = cfg.motherboard_rgb;
      mr.per_channel = v;
      if (v) for (const ch of chans) mr.channels[ch.key] = JSON.parse(JSON.stringify(mr.lighting));  // start from the current look
      editLight();
    }),
    el("span", { class: "hint" }, "Effects run on the board's Aura controller. Not saved to the board: after a restart it shows its own until Louver starts."));
  if (!m.per_channel) {
    box.replaceChildren(auraPanel("Every channel", `${chans.length} on this board`, m.lighting, chans, (patch) => {
      cfg.motherboard_rgb.lighting = { ...cfg.motherboard_rgb.lighting, ...patch };
      editLight();
    }, false));
    return;
  }
  box.replaceChildren(el("div", { class: "split" }, ...chans.map((ch, k) => auraPanel(ch.name, M.auraDetail(ch), M.auraLook(cfg, ch.key), [ch], (patch) => {
    // Look the entry up at edit time: cfg may have been replaced by a save.
    const mr = cfg.motherboard_rgb;
    mr.channels[ch.key] = { ...M.auraLook(cfg, ch.key), ...patch };
    editLight();
  }, k % 2 === 1))));
}

// Settings: what the motherboard RGB switch is doing. Updated on every poll
// (renderStatus), since the board shows up or fails after the switch flips.
function renderAuraStatus() {
  const a = state?.motherboard_rgb, on = !!cfg?.motherboard_rgb.enabled;
  const msg = !on ? "For ASUS Aura USB boards: the ARGB headers and onboard LEDs, as the Motherboard RGB zone. While this is off, Louver never opens the board's RGB controller."
    : a?.error ? "Motherboard RGB: " + a.error
    : a?.connected ? `Aura controller linked · firmware ${a.firmware || "?"} · ${auraChannels().length} channels`
      + (M.auraSkipped(a.channels) ? ` (skipped: ${M.auraSkipped(a.channels)})` : "") + ". Effects aren't saved to the board."
    : "Looking for an ASUS Aura motherboard RGB controller… none found yet.";
  const p = $("aura-status");
  if (p.textContent !== msg) p.textContent = msg;
}

// ---------------- settings ----------------
function renderSettings() {
  $("set-poll").value = cfg.poll_interval;
  $("set-min").value = cfg.min_percent;
  $("set-exit").value = cfg.on_exit;
  $("set-exit-pct").value = cfg.on_exit_percent;
  $("exit-pct-row").classList.toggle("hidden", cfg.on_exit !== "fixed");
  $("set-show-empty").checked = cfg.ui.show_empty_ports;
  $("set-aura").checked = cfg.motherboard_rgb.enabled;
  renderAuraStatus();
  document.querySelectorAll("[data-app]").forEach((inp) => { inp.checked = !!cfg.app[inp.dataset.app]; });
}
$("set-aura").addEventListener("change", (e) => { cfg.motherboard_rgb.enabled = e.target.checked; save(0); renderAll(); });
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
    const msg = M.unreachable(e);
    if ($("banner").textContent !== msg) $("banner").textContent = msg;
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
// pick: the "this looks like" choice for the current number, kept across re-renders.
const lab = { group: 2, ring: "inner", mode: 1, speed: 2, palette: "rainbow", pick: "" };
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
  const setNum = (n) => { lab.mode = clamp(n, 0, 255); lab.pick = ""; labSend(); renderLab(); };
  num.addEventListener("change", () => setNum(+num.value || 0));
  const step = (d) => () => setNum(lab.mode + d);
  const names = [...new Set(meta.hub_lighting_modes.filter((m) => !m.software && m[lab.ring] !== false).map((m) => m.name))];
  const pick = el("select", { "aria-label": "This looks like" }, el("option", { value: "" }, "This looks like…"), ...names.map((n) => el("option", { value: n }, n)));
  pick.value = names.includes(lab.pick) ? lab.pick : "";
  pick.addEventListener("change", () => { lab.pick = pick.value; });
  const saveBtn = el("button", { class: "btn primary", onclick: () => {
    if (!pick.value) return toast("Pick which effect this looks like first");
    cfg.hub.mode_map[lab.ring][pick.value] = lab.mode;
    save(0); renderLab();
  } }, "Save label");
  p.replaceChildren(
    field("Port", seg(visiblePorts().map(([g, i]) => [i, g.name]), lab.group, (v) => { lab.group = v; labSend(); renderLab(); })),
    field("Ring", seg([["inner", "Inner (fan)"], ["outer", "Outer (edge)"]], lab.ring, (v) => { lab.ring = v; labSend(); renderLab(); })),
    field("Effect number", el("div", { class: "lab-num" },
      el("button", { class: "btn", "aria-label": "Previous effect number", onclick: step(-1) }, "−"), num,
      el("button", { class: "btn", "aria-label": "Next effect number", onclick: step(1) }, "+"))),
    el("div", { class: "lab-row" },
      field("Test colours", el("div", { class: "pals", role: "group", "aria-label": "Test colours" }, ...Object.entries(LAB_PALETTES).map(([k, cs]) => {
        const b = el("button", { class: k === lab.palette ? "on" : "", "aria-pressed": String(k === lab.palette), "aria-label": k, title: k,
          onclick: () => { lab.palette = k; labSend(); renderLab(); } });
        b.style.setProperty("--c", cs.length > 1 ? `conic-gradient(${[...cs, cs[0]].join(", ")})` : cs[0]);
        return b;
      }))),
      slats("Speed", 0, 4, lab.speed, SPEED_LABELS, (v) => { lab.speed = v; labSend(); renderLab(); })),
    el("div", { class: "lab-label" }, pick, saveBtn),
    el("div", { class: "row end" }, el("button", { class: "btn", onclick: async () => {
      await api("/api/lab", { action: "end" }); toast("Normal lighting restored");
    } }, "Done · restore my lighting")),
  );

  const rows = meta.hub_lighting_modes.filter((m) => !m.software).map((m) => {
    const cell = (ring) => {
      if (m[ring] === false) return el("td", { class: "g" }, "—");
      const v = cfg.hub.mode_map[ring][m.name];
      if (v === undefined) return m.verified?.[ring]
        ? el("td", { class: "v", title: "Known from an L-Connect capture" }, "✓ capture")
        : el("td", { class: "g" }, "unverified");
      return el("td", { class: "v" }, `#${v} ✓`, el("button", { class: "btn round", "aria-label": `Remove label #${v} from ${m.name}`,
        onclick: () => { delete cfg.hub.mode_map[ring][m.name]; save(0); renderLab(); } }, "×"));
    };
    return el("tr", {}, el("td", {}, m.name), cell("inner"), cell("outer"));
  });
  $("lab-map").replaceChildren(el("table", { class: "map-table" }, el("caption", {}, "Effect numbers"),
    el("thead", {}, el("tr", {}, el("th", { scope: "col" }, "Effect"), el("th", { scope: "col" }, "Inner ring"), el("th", { scope: "col" }, "Outer ring"))),
    el("tbody", {}, ...rows)));
}

(async function init() {
  try {
    [cfg, meta, state] = await Promise.all([api("/api/config"), api("/api/meta"), api("/api/state")]);
  } catch (e) {
    $("banner").textContent = M.unreachable(e);
    $("banner").classList.remove("hidden");
    setTimeout(init, 3000);  // the daemon may still be starting
    return;
  }
  mbZeroRuns = M.nextZeroRuns({}, state);
  applyRoute(M.parseHash(location.hash));
  go({ view });
  setInterval(poll, 1000);
  setInterval(async () => { meta.sensors = (await api("/api/meta")).sensors; }, 15000);
})();
