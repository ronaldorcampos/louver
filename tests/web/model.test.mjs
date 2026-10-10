// Run: node --test tests/web/*.test.mjs   (tests/test_web.py runs it under pytest)
import { test } from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const M = createRequire(import.meta.url)("../../louver/web/model.js");

const META = {
  hub_lighting_modes: [
    { name: "Static", inner: true, outer: true, max_colors: 6 },
    { name: "Breathing", inner: true, outer: true, max_colors: 6 },
    { name: "Static Colorful", inner: false, outer: true, max_colors: 4 },
    { name: "Rainbow", inner: true, outer: true, max_colors: 0 },
    { name: "Meteor", inner: true, outer: true, max_colors: 6 },
    { name: "Custom", software: true }, { name: "Gradient", software: true }, { name: "Temperature", software: true },
  ],
  strimer_lighting_modes: [{ name: "Static", max_colors: 1 }, { name: "Twinkle", max_colors: 0 }, { name: "Gradient", software: true }],
  sensors: [{ id: "cpu", label: "CPU", value: 60 }, { id: "gpu", label: "GPU", value: null }],
};

function cfg({ lighting = {}, fans = [3, 3, 3, 0], strimer = {}, headers = [], showEmpty = false, sync = false } = {}) {
  const L = (i) => ({ mode: "Static", ring: "both", colors: ["#ff0000"], brightness: 4, ...lighting[i] });
  return {
    hub: { mb_rgb_sync: sync, groups: fans.map((f, i) => ({ name: `Port ${i + 1}`, fans: f })), lighting: [0, 1, 2, 3].map(L) },
    strimer: { mb_rgb_sync: false, per_port: false, lighting: { mode: "Static", colors: ["#00ff00"], brightness: 4 }, ports: [], ...strimer },
    motherboard: { headers },
    ui: { show_empty_ports: showEmpty },
  };
}
const fan = (c, port, f) => M.hubFanPreview(c, META, port, f);

test("normColor accepts #hex, bare hex and [r,g,b], rejects junk", () => {
  assert.equal(M.normColor("#FF0080"), "#ff0080");
  assert.equal(M.normColor("ff0080"), "#ff0080");
  assert.equal(M.normColor("##ff0080"), "#ff0080");
  assert.equal(M.normColor([255, 0, 128]), "#ff0080");
  assert.equal(M.normColor([256 + 1, 0, 0]), "#010000");  // & 0xFF, like parse_color
  assert.equal(M.normColor("#fff"), null);
  assert.equal(M.normColor(42), null);
});

test("Static and Breathing cycle colours per fan, like the hub does", () => {
  const c = cfg({ lighting: { 0: { colors: ["#ff0000", "#0000ff"] } } });
  assert.deepEqual([0, 1, 2, 3].map((f) => fan(c, 0, f).outer[0]), ["#ff0000", "#0000ff", "#ff0000", "#0000ff"]);
  const b = cfg({ lighting: { 0: { mode: "Breathing", colors: ["#111111", "#222222", "#333333"] } } });
  assert.equal(fan(b, 0, 4).inner[0], "#222222");
});

test("Custom shows each fan's own inner and outer LEDs", () => {
  const outer = Array(72).fill("#000000"), inner = Array(48).fill("#000000");
  outer[12] = "#ff0000";   // fan 1, first outer LED
  inner[8 + 3] = "00ff00"; // fan 1, an inner LED, written without '#'
  const c = cfg({ lighting: { 0: { mode: "Custom", custom: { outer, inner } } } });
  assert.deepEqual(fan(c, 0, 1), { inner: ["#00ff00"], outer: ["#ff0000"] });
  assert.deepEqual(fan(c, 0, 0), { inner: [M.OFF], outer: [M.OFF] });
});

test("Gradient gives one colour per fan along the stops (rgbfx.render_hub_port)", () => {
  const c = cfg({ lighting: { 0: { mode: "Gradient", colors: ["#000000", "#ffffff"] } }, fans: [3, 3, 3, 0] });
  assert.deepEqual([0, 1, 2].map((f) => fan(c, 0, f).outer[0]), ["#000000", "#808080", "#ffffff"]);
});

test("Gradient with span_all continues across ports", () => {
  const lighting = {};
  for (let i = 0; i < 4; i++) lighting[i] = { mode: "Gradient", span_all: true, colors: ["#000000", "#ffffff"] };
  const c = cfg({ lighting, fans: [2, 3, 0, 0] });  // 5 fans in all: t = 0, .25, .5, .75, 1
  assert.equal(fan(c, 0, 0).outer[0], "#000000");
  assert.equal(fan(c, 1, 0).outer[0], "#808080");  // the third fan overall
  assert.equal(fan(c, 1, 2).outer[0], "#ffffff");
});

test("Temperature follows the thermal ramp, magenta with no reading", () => {
  const hot = cfg({ lighting: { 0: { mode: "Temperature", sensor: "cpu", temp_range: [40, 60] } } });
  assert.equal(fan(hot, 0, 0).outer[0], "#ff2800");  // at the hot end: the last stop
  const cold = cfg({ lighting: { 0: { mode: "Temperature", sensor: "cpu", temp_range: [60, 90] } } });
  assert.equal(fan(cold, 0, 0).outer[0], "#005aff");
  const none = cfg({ lighting: { 0: { mode: "Temperature", sensor: "gpu", temp_range: [40, 60] } } });
  assert.equal(fan(none, 0, 0).outer[0], M.NO_READING);
  const missing = cfg({ lighting: { 0: { mode: "Temperature", sensor: "nope", temp_range: [40, 60] } } });
  assert.equal(fan(missing, 0, 0).inner[0], M.NO_READING);
});

test("ring picks which ring lights, for software and hardware modes", () => {
  const g = cfg({ lighting: { 0: { mode: "Gradient", ring: "inner", colors: ["#ff0000"] } } });
  assert.deepEqual(fan(g, 0, 0), { inner: ["#ff0000"], outer: [M.OFF] });
  const s = cfg({ lighting: { 0: { mode: "Static", ring: "outer", colors: ["#00ff00"] } } });
  // Hardware effects leave the other ring alone: it keeps its old effect.
  assert.deepEqual(fan(s, 0, 0), { inner: [M.UNSET], outer: ["#00ff00"] });
});

test("corner modes paint the outer ring only, four colours", () => {
  const c = cfg({ lighting: { 0: { mode: "Static Colorful", colors: ["#ff0000", "#00ff00"] } } });
  assert.deepEqual(fan(c, 0, 0), { inner: [M.UNSET], outer: ["#ff0000", "#00ff00", "#ff0000", "#00ff00"] });
});

test("built-in colour effects, Off, brightness off and MB sync", () => {
  assert.deepEqual(fan(cfg({ lighting: { 0: { mode: "Rainbow" } } }), 0, 0).outer, M.RAINBOW);
  assert.deepEqual(fan(cfg({ lighting: { 0: { mode: "Off" } } }), 0, 0), { inner: [M.OFF], outer: [M.OFF] });
  assert.deepEqual(fan(cfg({ lighting: { 0: { brightness: -1 } } }), 0, 0).outer, [M.OFF]);
  assert.equal(fan(cfg({ sync: true }), 0, 0), null);
  const meteor = fan(cfg({ lighting: { 0: { mode: "Meteor", colors: ["#ff0000", "#00ff00"] } } }), 0, 0);
  assert.deepEqual(meteor.outer, ["#ff0000", "#00ff00"]);
});

test("Strimer previews: gradient across ports, along a port, per-port, MB sync", () => {
  const across = cfg({ strimer: { lighting: { mode: "Gradient", colors: ["#000000", "#ffffff"] } } });
  assert.deepEqual(M.strimerPreview(across, META, 3), [["#000000"], ["#808080"], ["#ffffff"]]);
  const along = cfg({ strimer: { lighting: { mode: "Gradient", span_all: false, colors: ["#000000", "#ffffff"] } } });
  assert.deepEqual(M.strimerPreview(along, META, 2)[1], ["#000000", "#ffffff"]);
  const per = cfg({ strimer: { per_port: true, ports: [{ mode: "Static", colors: ["#ff0000"] }, { mode: "Twinkle" }] } });
  assert.deepEqual(M.strimerPreview(per, META, 2), [["#ff0000"], M.RAINBOW]);
  assert.equal(M.strimerPreview(cfg({ strimer: { mb_rgb_sync: true } }), META, 12), null);
});

test("ringCss / barCss", () => {
  assert.equal(M.ringCss(["#ff0000"]), "#ff0000");
  assert.equal(M.ringCss(["#ff0000", "#00ff00"]), "conic-gradient(#ff0000, #00ff00, #ff0000)");
  assert.equal(M.barCss(["#ff0000", "#00ff00"]), "linear-gradient(90deg, #ff0000, #00ff00)");
  assert.equal(M.ringCss(null), M.MB_LIGHT);
  assert.equal(M.barCss(null), M.MB_LIGHT);
});

const STATE = (headers, strimer = false) => ({
  hub: {}, strimer: { connected: strimer, ports: 12 },
  motherboard: { present: true, headers: headers.map(([index, rpm]) => ({ index, rpm })) },
});

test("zones: ports with fans, spinning or configured headers, then the Strimer", () => {
  const c = cfg({ headers: [{ index: 7, mode: "curve" }, { index: 3, mode: "bios" }] });
  const z = M.zones(c, STATE([[1, 0], [2, 1450], [3, 0], [7, 0]], true));
  assert.deepEqual(z.map(M.zoneKey), ["hub-0", "hub-1", "hub-2", "mb-2", "mb-7", "strimer"]);
  assert.deepEqual(M.zones(cfg({ showEmpty: true }), STATE([])).map(M.zoneKey), ["hub-0", "hub-1", "hub-2", "hub-3"]);
  assert.deepEqual(M.zones(c, { motherboard: { present: false } }).map(M.zoneKey), ["hub-0", "hub-1", "hub-2"]);
});

test("a BIOS header that stops spinning stays listed for a few polls, and while selected", () => {
  const c = cfg({ fans: [0, 0, 0, 0] });
  let runs = M.nextZeroRuns({}, STATE([[2, 900]]));
  assert.equal(runs[2], 0);
  for (let n = 1; n < M.MB_ZERO_GRACE; n++) {
    runs = M.nextZeroRuns(runs, STATE([[2, 0]]));
    assert.deepEqual(M.zones(c, STATE([[2, 0]]), { zeroRuns: runs }).map(M.zoneKey), ["mb-2"], `poll ${n}`);
  }
  runs = M.nextZeroRuns(runs, STATE([[2, 0]]));
  assert.deepEqual(M.zones(c, STATE([[2, 0]]), { zeroRuns: runs }), []);
  const keep = { kind: "mb", i: 2 };
  assert.deepEqual(M.zones(c, STATE([[2, 0]]), { zeroRuns: runs, keep }).map(M.zoneKey), ["mb-2"]);
  // Never seen spinning: not listed.
  assert.deepEqual(M.zones(c, STATE([[5, 0]]), { zeroRuns: M.nextZeroRuns({}, STATE([[5, 0]])) }), []);
  // keep can't conjure a header the board doesn't have.
  assert.deepEqual(M.zones(c, STATE([]), { keep: { kind: "mb", i: 9 } }), []);
});

test("validSelection keeps a live zone, falls back to the first, null when none", () => {
  const zs = [{ kind: "hub", i: 0 }, { kind: "strimer", i: 0 }];
  const s = { kind: "strimer", i: 0 };
  assert.equal(M.validSelection(s, zs), s);
  assert.deepEqual(M.validSelection({ kind: "hub", i: 3 }, zs), { kind: "hub", i: 0 });
  assert.deepEqual(M.validSelection(null, zs), { kind: "hub", i: 0 });
  assert.equal(M.validSelection({ kind: "hub", i: 0 }, []), null);
});

test("parseHash / hashFor round-trip, reject junk", () => {
  for (const h of ["#hub-0", "#hub-3", "#mb-1", "#mb-16", "#strimer"]) {
    const r = M.parseHash(h);
    assert.equal(M.hashFor(r.view, r.sel), h);
  }
  assert.deepEqual(M.parseHash("#settings"), { view: "settings" });
  assert.equal(M.hashFor("settings", { kind: "hub", i: 1 }), "#settings");
  assert.deepEqual(M.parseHash("hub-2"), { view: "zone", sel: { kind: "hub", i: 2 } });  // data-zone keys too
  for (const h of ["", "#", "#hub-4", "#mb-0", "#mb-17", "#hub-1x", "#Strimer"]) assert.equal(M.parseHash(h), null, h);
  assert.equal(M.hashFor("zone", null), null);
});

test("effectTag: what each effect does with colours", () => {
  const tag = (kind, m) => M.effectTag(kind, m);
  assert.equal(tag("hub", { name: "Static Colorful", inner: false, outer: true, max_colors: 4 }), "outer ring only · a colour per side");
  // Colorful City takes a 6-colour palette: only the corner modes are per side.
  assert.equal(tag("hub", { name: "Colorful City", inner: true, outer: true, max_colors: 6 }), "up to 6 colours");
  assert.equal(tag("hub", { name: "Static", inner: true, outer: true, max_colors: 6 }), "a colour per fan");
  assert.equal(tag("hub", { name: "Rainbow", inner: true, outer: true, max_colors: 0 }), "built-in colours");
  assert.equal(tag("hub", { name: "Off", inner: true, outer: true, max_colors: 0 }), "LEDs off");
  assert.equal(tag("hub", { name: "Gradient", software: true }), "2–6 colour stops");
  // The Strimer has no rings, fans or corners.
  assert.equal(tag("strimer", { name: "Static", max_colors: 1 }), "one colour");
  assert.equal(tag("strimer", { name: "Static Colorful", max_colors: 4 }), "up to 4 colours");
});

test("isCorner: hub corner modes only", () => {
  assert.equal(M.isCorner("hub", "Breathing Colorful"), true);
  assert.equal(M.isCorner("hub", "Colorful City"), false);
  assert.equal(M.isCorner("strimer", "Static Colorful"), false);
});

test("speedFoot: the sensor failsafe is only promised where it exists", () => {
  const live = { temp: 45.4, percent: 50 };
  assert.equal(M.speedFoot("hub", "curve", live), "Now 45° → 50% · if the sensor drops out, this port runs at 100%");
  assert.equal(M.speedFoot("mb", "standard", live), "Now 45° → 50% · if the sensor drops out, this header runs at 100%");
  assert.equal(M.speedFoot("hub", "quiet", { temp: null, percent: 100 }), "if the sensor drops out, this port runs at 100%");
  for (const mode of ["manual", "mb_sync", "bios"]) assert.equal(M.speedFoot("hub", mode, live), "", mode);
});

// ---------------- motherboard RGB (ASUS Aura) ----------------
const AURA_META = { aura_lighting_modes: [{ name: "Off", max_colors: 0 }, { name: "Static", max_colors: 1 }, { name: "Rainbow", max_colors: 0 }, { name: "Red Pulse", max_colors: 0 }] };
const CHANNELS = [
  { key: "onboard", kind: "onboard", leds: 5, rgb_headers: 1 },
  { key: "argb-1", kind: "argb", leds: null, rgb_headers: 0 },
  { key: "argb-2", kind: "argb", leds: null, rgb_headers: 0 },
];
const auraCfg = (m = {}) => ({ ...cfg(), motherboard_rgb: { enabled: true, per_channel: false, lighting: { mode: "Static", colors: ["#00ff00"] }, channels: {}, ...m } });

test("auraLook: per-channel entries only under per-channel control, else the shared look", () => {
  const own = { mode: "Rainbow", colors: ["#ff0000"] };
  assert.deepEqual(M.auraLook(auraCfg({ channels: { "argb-1": own } }), "argb-1"), { mode: "Static", colors: ["#00ff00"] });
  const per = auraCfg({ per_channel: true, channels: { "argb-1": own } });
  assert.equal(M.auraLook(per, "argb-1"), own);
  assert.deepEqual(M.auraLook(per, "argb-2"), { mode: "Static", colors: ["#00ff00"] });  // like daemon._aura_look
});

test("auraPreview: one colour, built-in colours, dark", () => {
  assert.deepEqual(M.auraPreview({ mode: "Static", colors: ["#00FF00", "#ff0000"] }, AURA_META), ["#00ff00"]);
  assert.deepEqual(M.auraPreview({ mode: "Rainbow", colors: ["#00ff00"] }, AURA_META), M.RAINBOW);
  assert.deepEqual(M.auraPreview({ mode: "Red Pulse", colors: ["#00ff00"] }, AURA_META), ["#ff0000"]);
  assert.deepEqual(M.auraPreview({ mode: "Off", colors: ["#00ff00"] }, AURA_META), [M.OFF]);
});

test("auraBar: the shared look when every channel has it, else each channel's colour", () => {
  assert.deepEqual(M.auraBar(auraCfg(), AURA_META, CHANNELS), ["#00ff00"]);
  assert.deepEqual(M.auraBar(auraCfg({ lighting: { mode: "Rainbow", colors: ["#00ff00"] } }), AURA_META, CHANNELS), M.RAINBOW);
  const per = auraCfg({ per_channel: true, channels: { "argb-2": { mode: "Static", colors: ["#0000ff"] } } });
  assert.deepEqual(M.auraBar(per, AURA_META, CHANNELS), ["#00ff00", "#00ff00", "#0000ff"]);
  assert.deepEqual(M.auraBar(auraCfg(), AURA_META, []), [M.OFF]);
});

test("auraDetail: onboard LED counts; ARGB headers aren't counted by the board", () => {
  assert.equal(M.auraDetail(CHANNELS[0]), "5 LEDs · 1 of them 12 V RGB header");
  assert.equal(M.auraDetail({ kind: "onboard", leds: 1, rgb_headers: 0 }), "1 LED");
  assert.equal(M.auraDetail({ kind: "onboard", leds: 8, rgb_headers: 2 }), "8 LEDs · 2 of them 12 V RGB headers");
  assert.equal(M.auraDetail(CHANNELS[1]), "LED count not reported");
});

test("zones: motherboard RGB only when it's on and the board answers; #aura routes to it", () => {
  const st = (rgb) => ({ ...STATE([], true), motherboard_rgb: { channels: CHANNELS, ...rgb } });
  const c = cfg({ fans: [0, 0, 0, 0] });
  assert.deepEqual(M.zones(c, st({ enabled: true, connected: true })).map(M.zoneKey), ["strimer", "aura"]);
  assert.deepEqual(M.zones(c, st({ enabled: true, connected: false })).map(M.zoneKey), ["strimer"]);
  assert.deepEqual(M.zones(c, st({ enabled: false, connected: true })).map(M.zoneKey), ["strimer"]);
  assert.deepEqual(M.zones(c, STATE([], true)).map(M.zoneKey), ["strimer"]);  // no motherboard_rgb in the state
  assert.deepEqual(M.parseHash("#aura"), { view: "zone", sel: { kind: "aura", i: 0 } });
  assert.equal(M.hashFor("zone", { kind: "aura", i: 0 }), "#aura");
  assert.equal(M.parseHash("#aura-1"), null);
});

test("auraUsable / auraSkipped: channels the daemon noted are left out and named", () => {
  const chans = [...CHANNELS, { key: "argb-3", name: "ARGB header 3", kind: "argb", note: "its LEDs don't fit one effect report" }];
  assert.deepEqual(M.auraUsable(chans).map((ch) => ch.key), ["onboard", "argb-1", "argb-2"]);
  assert.equal(M.auraSkipped(chans), "ARGB header 3 (its LEDs don't fit one effect report)");
  assert.equal(M.auraSkipped(CHANNELS), "");
  assert.deepEqual(M.auraUsable(undefined), []);
});

test("effectTag for Aura effects", () => {
  assert.equal(M.effectTag("aura", { name: "Static", max_colors: 1 }), "one colour");
  assert.equal(M.effectTag("aura", { name: "Rainbow", max_colors: 0 }), "built-in colours");
  assert.equal(M.effectTag("aura", { name: "Off", max_colors: 0 }), "LEDs off");
});

test("speedModes: a pump header offers only BIOS and Full", () => {
  const meta = { fan_modes: ["quiet", "mb_sync"], mb_fan_modes: ["bios", "quiet", "full", "manual"], pump_modes: ["bios", "full"] };
  assert.deepEqual(M.speedModes(meta, "hub", { pump: true }), ["quiet", "mb_sync"]);  // hub ports have no pump flag
  assert.deepEqual(M.speedModes(meta, "mb", { pump: false }), meta.mb_fan_modes);
  assert.deepEqual(M.speedModes(meta, "mb", {}), meta.mb_fan_modes);
  assert.deepEqual(M.speedModes(meta, "mb", { pump: true }), ["bios", "full"]);
});

test("pumpChange moves a slowing mode to BIOS, keeps BIOS and Full", () => {
  const P = ["bios", "full"];
  assert.deepEqual(M.pumpChange({ mode: "quiet" }, true, P), { pump: true, mode: "bios" });
  assert.deepEqual(M.pumpChange({ mode: "manual" }, true, P), { pump: true, mode: "bios" });
  assert.deepEqual(M.pumpChange({ mode: "full" }, true, P), { pump: true, mode: "full" });
  assert.deepEqual(M.pumpChange({ mode: "bios" }, true, P), { pump: true, mode: "bios" });
  assert.deepEqual(M.pumpChange({ mode: "full" }, false, P), { pump: false, mode: "full" });  // unticking changes nothing else
});

test("floorChange clamps to 0..100 and lifts the manual speed to it", () => {
  assert.deepEqual(M.floorChange({ manual_percent: 10 }, "40"), { min_percent: 40, manual_percent: 40 });
  assert.deepEqual(M.floorChange({ manual_percent: 70 }, "40"), { min_percent: 40, manual_percent: 70 });
  assert.deepEqual(M.floorChange({ manual_percent: 50 }, "150"), { min_percent: 100, manual_percent: 100 });
  assert.deepEqual(M.floorChange({ manual_percent: 50 }, "-5"), { min_percent: 0, manual_percent: 50 });
  assert.deepEqual(M.floorChange({ manual_percent: 50 }, ""), { min_percent: 0, manual_percent: 50 });
  assert.deepEqual(M.floorChange({ manual_percent: 50 }, "abc"), { min_percent: 0, manual_percent: 50 });
  assert.deepEqual(M.floorChange({ manual_percent: 50 }, "33.6"), { min_percent: 34, manual_percent: 50 });
});

test("showsFloor: only on a header Louver drives that isn't a pump", () => {
  assert.equal(M.showsFloor("mb", { mode: "quiet", pump: false }), true);
  assert.equal(M.showsFloor("mb", { mode: "manual" }), true);
  assert.equal(M.showsFloor("mb", { mode: "bios", pump: false }), false);
  assert.equal(M.showsFloor("mb", { mode: "full", pump: true }), false);
  assert.equal(M.showsFloor("hub", { mode: "quiet" }), false);
});

test("unreachable shows the daemon's words when it answered", () => {
  const e503 = Object.assign(new Error("Louver isn't running: systemctl --user start louver"), { status: 503 });
  assert.equal(M.unreachable(e503), "Louver isn't running: systemctl --user start louver");
  assert.equal(M.unreachable(new TypeError("Load failed")), "Louver daemon not reachable (Load failed).");
  assert.equal(M.unreachable(new Error("")), "Louver daemon not reachable.");
  assert.equal(M.unreachable(undefined), "Louver daemon not reachable.");
});
