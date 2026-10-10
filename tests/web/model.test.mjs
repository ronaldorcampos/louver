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
