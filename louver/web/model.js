"use strict";
// Pure helpers for the UI: which zones exist, routing, and what a zone's
// lighting looks like. No DOM and no globals, so tests/web can run them in Node.
// Previews mirror what the daemon sends (rgbfx.py for software modes,
// devices.py for hardware ones); animated effects show only their colours.
const LouverModel = (() => {

const OFF = "#2a2f3c";             // LEDs dark
// A ring a hardware effect doesn't write: it keeps whatever it last ran, which
// we can't know, so it gets its own hatched look rather than claiming "dark".
const UNSET = "repeating-conic-gradient(var(--line) 0 15deg, var(--panel) 0 30deg)";
const NO_READING = "#ff00ff";       // rgbfx.thermal: magenta = no sensor reading
const RAINBOW = ["#ff3b3b", "#ffd400", "#3ecf8e", "#33c3f0", "#7b61ff", "#ff4dd2"];
const THERMAL = [[0, 90, 255], [0, 220, 120], [255, 210, 0], [255, 40, 0]];
const HUB_FANS = 6, HUB_INNER = 8, HUB_OUTER = 12;
const PER_FAN = new Set(["Static", "Breathing"]);
const CORNER = new Set(["Static Colorful", "Breathing Colorful"]);
// Motherboard headers stay listed this many polls after they stop spinning,
// so a fan that dips to 0 rpm for a moment doesn't make its zone flicker.
const MB_ZERO_GRACE = 5;

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

// "#rrggbb", "rrggbb" or [r, g, b], like devices.parse_color. null if unreadable.
function normColor(c) {
  if (Array.isArray(c) && c.length >= 3) return toHex(c.slice(0, 3).map((v) => (v | 0) & 255));
  if (typeof c === "string") {
    const s = c.replace(/^#+/, "");  // parse_color lstrips every '#'
    if (/^[0-9a-f]{6}$/i.test(s)) return "#" + s.toLowerCase();
  }
  return null;
}
const toHex = (rgb) => "#" + rgb.map((v) => Math.round(v).toString(16).padStart(2, "0")).join("");
const toRgb = (hex) => [1, 3, 5].map((k) => parseInt(hex.slice(k, k + 2), 16));

// rgbfx.gradient: t in 0..1 across evenly spaced stops ([r,g,b] each).
function gradientAt(stops, t) {
  if (!stops.length) return [0, 0, 0];
  if (stops.length === 1) return stops[0];
  t = clamp(t, 0, 1) * (stops.length - 1);
  const i = Math.min(Math.floor(t), stops.length - 2), f = t - i;
  return stops[i].map((x, k) => Math.round(x + (stops[i + 1][k] - x) * f));
}

function colorsOf(L) {
  const c = (L.colors || []).map(normColor).filter(Boolean);
  return c.length ? c : ["#ffffff"];
}

function thermalColor(L, sensors) {
  const s = (sensors || []).find((x) => x.id === L.sensor);
  if (!s || s.value == null) return NO_READING;
  const [lo, hi] = L.temp_range || [35, 85];
  return toHex(gradientAt(THERMAL, (s.value - lo) / Math.max(1e-6, hi - lo)));
}

const lit = (leds) => {
  const on = leds.map(normColor).filter((x) => x && x !== "#000000");
  return on.length ? on : [OFF];
};

// One hub fan: { inner: [colours], outer: [colours] }, or null when the
// motherboard's ARGB header drives the hub.
// port is the hub port (0..3); fan is the fan on it (0..5).
function hubFanPreview(cfg, meta, port, fan) {
  if (cfg.hub.mb_rgb_sync) return null;
  const L = cfg.hub.lighting[port];
  if (!L || L.mode === "Off" || L.brightness === -1) return { inner: [OFF], outer: [OFF] };
  if (L.mode === "Custom") {
    const cu = L.custom || {};
    return {
      inner: lit((cu.inner || []).slice(fan * HUB_INNER, fan * HUB_INNER + HUB_INNER)),
      outer: lit((cu.outer || []).slice(fan * HUB_OUTER, fan * HUB_OUTER + HUB_OUTER)),
    };
  }
  const c = colorsOf(L);
  const spec = (meta.hub_lighting_modes || []).find((m) => m.name === L.mode);
  let inner, outer;
  if (L.mode === "Gradient") {
    const counts = cfg.hub.groups.map((g) => g.fans | 0);
    const [total, offset] = L.span_all
      ? [Math.max(1, counts.reduce((a, b) => a + b, 0)), counts.slice(0, port).reduce((a, b) => a + b, 0)]
      : [Math.max(1, counts[port]), 0];
    inner = outer = [toHex(gradientAt(c.map(toRgb), (offset + fan) / Math.max(1, total - 1)))];
  } else if (L.mode === "Temperature") {
    inner = outer = [thermalColor(L, meta.sensors)];
  } else if (PER_FAN.has(L.mode)) {
    inner = outer = [c[fan % c.length]];
  } else if (CORNER.has(L.mode)) {
    outer = [0, 1, 2, 3].map((k) => c[k % c.length]);
    inner = [UNSET];  // the hub never writes the inner ring for these
  } else if (!spec || !spec.max_colors || /rainbow/i.test(L.mode)) {
    inner = outer = RAINBOW;  // built-in colours
  } else {
    inner = outer = c.slice(0, spec.max_colors);
  }
  // A ring the effect leaves out: software modes paint it black, hardware
  // ones don't touch it.
  const ring = L.ring || "both";
  const noInner = ring === "outer" || (spec && spec.inner === false);
  const noOuter = ring === "inner" || (spec && spec.outer === false);
  const left = spec && spec.software ? [OFF] : [UNSET];
  return { inner: noInner ? left : inner, outer: noOuter ? left : outer };
}

// The Strimer: one colour list per port, or null when the motherboard drives it.
function strimerPreview(cfg, meta, ports) {
  const s = cfg.strimer;
  if (s.mb_rgb_sync) return null;
  const n = ports || 12;
  return Array.from({ length: n }, (_, p) => strimerPortPreview(s.per_port ? s.ports[p] : s.lighting, meta, p, n));
}

function strimerPortPreview(L, meta, port, count) {
  if (!L || L.mode === "Off" || L.brightness === -1) return [OFF];
  if (L.mode === "Custom") return lit((L.custom || {}).leds || []);
  const c = colorsOf(L);
  if (L.mode === "Gradient") {
    const stops = c.map(toRgb);
    if (L.span_all ?? true) return [toHex(gradientAt(stops, port / Math.max(1, count - 1)))];
    return c;  // along each port
  }
  if (L.mode === "Temperature") return [thermalColor(L, meta.sensors)];
  if (PER_FAN.has(L.mode)) return [c[0]];
  const spec = (meta.strimer_lighting_modes || []).find((m) => m.name === L.mode);
  if (!spec || !spec.max_colors || /rainbow/i.test(L.mode)) return RAINBOW;
  return c.slice(0, spec.max_colors);
}

// Motherboard RGB (the ASUS Aura controller): a channel's look is its own
// entry under per-channel control, else the shared one (daemon._aura_look).
function auraLook(cfg, key) {
  const m = cfg.motherboard_rgb;
  return (m.per_channel && m.channels[key]) || m.lighting;
}
// Aura effects take one colour or none; Red Pulse's built-in colour is known.
const AURA_BUILTIN = { "Red Pulse": ["#ff0000"] };
function auraPreview(L, meta) {
  if (!L || L.mode === "Off") return [OFF];
  const spec = (meta.aura_lighting_modes || []).find((m) => m.name === L.mode);
  if (spec && spec.max_colors) return colorsOf(L).slice(0, 1);
  return AURA_BUILTIN[L.mode] || RAINBOW;
}
// The sidebar's one bar: the shared look if every channel has it, else each
// channel's first colour. channels: state.motherboard_rgb.channels.
function auraBar(cfg, meta, channels) {
  if (!channels.length) return [OFF];
  const looks = channels.map((ch) => auraLook(cfg, ch.key));
  if (looks.every((L) => JSON.stringify(L) === JSON.stringify(looks[0]))) return auraPreview(looks[0], meta);
  return looks.map((L) => auraPreview(L, meta)[0]);
}
// A channel's subtitle. The config table counts the onboard LEDs, never the
// LEDs on an ARGB header.
function auraDetail(ch) {
  if (ch.kind !== "onboard") return "LED count not reported";
  const n = `${ch.leds} LED${ch.leds === 1 ? "" : "s"}`;
  return ch.rgb_headers ? `${n} · ${ch.rgb_headers} of them 12 V RGB header${ch.rgb_headers === 1 ? "" : "s"}` : n;
}
// Channels Louver drives: the daemon notes, and skips, any whose effect
// colour doesn't fit one report.
const auraUsable = (channels) => (channels || []).filter((ch) => !ch.note);
// "ARGB header 4 (why)" for each skipped channel, or "" if none.
const auraSkipped = (channels) => (channels || []).filter((ch) => ch.note).map((ch) => `${ch.name} (${ch.note})`).join(", ");
const auraShown = (state) => !!(state && state.motherboard_rgb && state.motherboard_rgb.enabled && state.motherboard_rgb.connected);

// RAM RGB (Corsair DDR5 sticks, through the louver-ram helper): Louver draws
// one static frame per stick, LED 0 first, like ramrgb.render.
function ramFrame(L, leds) {
  const n = Math.max(1, leds | 0);
  if (!L || L.mode === "Off") return Array(n).fill(OFF);
  const c = colorsOf(L);
  if (L.mode !== "Gradient" || c.length === 1) return Array(n).fill(c[0]);
  const stops = c.map(toRgb);
  return Array.from({ length: n }, (_, i) => toHex(gradientAt(stops, i / Math.max(1, n - 1))));
}
// The sidebar's one bar: the look along a stick (every stick gets the same).
// sticks: state.ram_rgb.sticks.
function ramBar(cfg, sticks) {
  const L = cfg.ram_rgb.lighting;
  if (!sticks || !sticks.length || L.mode === "Off") return [OFF];
  if (L.mode !== "Gradient") return colorsOf(L).slice(0, 1);
  return ramFrame(L, sticks[0].leds);
}
const ramDetail = (st) => `${st.leds} LED${st.leds === 1 ? "" : "s"} · firmware ${st.firmware} · address 0x${st.addr.toString(16)}`;
const ramShown = (state) => !!(state && state.ram_rgb && state.ram_rgb.enabled && state.ram_rgb.connected);

// The motherboard's own ARGB effect: we can't see it, so show a neutral pattern.
const MB_LIGHT = "repeating-conic-gradient(var(--muted) 0 22.5deg, var(--panel-2) 0 45deg)";
const ringCss = (cols) => (cols == null ? MB_LIGHT : cols.length === 1 ? cols[0] : `conic-gradient(${[...cols, cols[0]].join(", ")})`);
const barCss = (cols) => (cols == null ? MB_LIGHT : cols.length === 1 ? cols[0] : `linear-gradient(90deg, ${cols.join(", ")})`);

// ---------------- zones ----------------
const visiblePorts = (cfg) => cfg.hub.groups.map((g, i) => [g, i]).filter(([g]) => g.fans > 0 || cfg.ui.show_empty_ports);
const mbCfg = (cfg, index) => cfg.motherboard.headers.find((x) => x.index === index);
const sameZone = (a, b) => !!a && !!b && a.kind === b.kind && a.i === b.i;
const SINGLE_ZONES = new Set(["strimer", "aura", "ram"]);  // one zone each: named without a number
const zoneKey = (z) => (SINGLE_ZONES.has(z.kind) ? z.kind : `${z.kind}-${z.i}`);

// Motherboard headers show up once something spins on them or you've set them
// up. zeroRuns[index] counts polls in a row at 0 rpm (missing = never seen
// spinning); keep is the selected zone, which never vanishes from under you.
function zones(cfg, state, { zeroRuns = {}, keep = null } = {}) {
  const z = visiblePorts(cfg).map(([, i]) => ({ kind: "hub", i }));
  const m = state && state.motherboard;
  if (m && m.present) {
    for (const h of m.headers) {
      const c = mbCfg(cfg, h.index);
      const runs = zeroRuns[h.index] ?? Infinity;
      if ((h.rpm ?? 0) > 0 || runs < MB_ZERO_GRACE || (c && c.mode !== "bios") || sameZone(keep, { kind: "mb", i: h.index })) {
        z.push({ kind: "mb", i: h.index });
      }
    }
  }
  if (state && state.strimer && state.strimer.connected) z.push({ kind: "strimer", i: 0 });
  // Motherboard RGB: only once it's turned on in Settings and the board answers.
  if (auraShown(state)) z.push({ kind: "aura", i: 0 });
  // RAM RGB: likewise, once it's on and the helper found sticks.
  if (ramShown(state)) z.push({ kind: "ram", i: 0 });
  return z;
}

// Count consecutive 0-rpm polls per header, for zones().
function nextZeroRuns(prev, state) {
  const out = {};
  const m = state && state.motherboard;
  if (!m || !m.present) return out;
  for (const h of m.headers) out[h.index] = (h.rpm ?? 0) > 0 ? 0 : (prev[h.index] ?? Infinity) + 1;
  return out;
}

// The selection if it's still a zone, else the first zone, else null.
function validSelection(sel, zs) {
  if (zs.some((x) => sameZone(x, sel))) return sel;
  return zs.length ? { ...zs[0] } : null;
}

// #hub-2, #mb-7, #strimer, #aura, #ram, #settings: reloads keep your place, and the tray
// can open the window on a zone.
function parseHash(hash) {
  const h = String(hash || "").replace(/^#/, "");
  let m;
  if (h === "settings") return { view: "settings" };
  if (SINGLE_ZONES.has(h)) return { view: "zone", sel: { kind: h, i: 0 } };
  if ((m = /^hub-([0-3])$/.exec(h))) return { view: "zone", sel: { kind: "hub", i: +m[1] } };
  if ((m = /^mb-([1-9]|1[0-6])$/.exec(h))) return { view: "zone", sel: { kind: "mb", i: +m[1] } };
  return null;
}
// The hash for what's on screen; null when there's nothing to name.
function hashFor(view, sel) {
  if (view === "settings") return "#settings";
  return sel ? "#" + zoneKey(sel) : null;
}

// ---- labels ----
// Hub corner modes take one colour per side (devices.py CORNER_MODES); a
// name test would also catch Colorful City, which takes a 6-colour palette.
const isCorner = (kind, name) => kind === "hub" && CORNER.has(name);
const SOFT_TAGS = { Custom: "paint each LED", Gradient: "2–6 colour stops", Temperature: "colour follows a sensor" };
// What an effect does with colours, shown under its name on the effect card.
function effectTag(kind, m) {
  if (m.software) return SOFT_TAGS[m.name] || "software";
  if (kind === "ram" && m.name === "Gradient") return "colour stops along each stick";
  const ring = kind === "hub" && m.inner === false ? "outer ring only · " : kind === "hub" && m.outer === false ? "inner ring only · " : "";
  const c = m.name === "Off" ? "LEDs off" : !m.max_colors ? "built-in colours"
    : isCorner(kind, m.name) ? "a colour per side"
    : kind === "hub" && PER_FAN.has(m.name) ? "a colour per fan"
    : m.max_colors === 1 ? "one colour" : `up to ${m.max_colors} colours`;
  return ring + c;
}
// The Speed panel's footnote. Only temperature-driven modes have a sensor
// failsafe (daemon._target); MB sync and BIOS say their piece elsewhere.
function speedFoot(kind, mode, s) {
  if (mode === "manual" || mode === "mb_sync" || mode === "bios") return "";
  const now = s.temp == null || s.percent == null ? "" : `Now ${s.temp.toFixed(0)}° → ${s.percent}% · `;
  return now + `if the sensor drops out, this ${kind === "hub" ? "port" : "header"} runs at 100%`;
}

return {
  OFF, UNSET, NO_READING, RAINBOW, MB_LIGHT, MB_ZERO_GRACE, clamp, normColor, gradientAt, thermalColor,
  hubFanPreview, strimerPreview, strimerPortPreview, ringCss, barCss,
  auraLook, auraPreview, auraBar, auraDetail, auraUsable, auraSkipped, auraShown,
  ramFrame, ramBar, ramDetail, ramShown,
  visiblePorts, mbCfg, sameZone, zoneKey, zones, nextZeroRuns, validSelection, parseHash, hashFor,
  isCorner, effectTag, speedFoot,
};
})();
if (typeof module !== "undefined") module.exports = LouverModel;
