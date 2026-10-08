import json

import pytest

from unihub import config as cfgmod
from unihub.devices import StrimerPlus, UniHubALV2
from unihub.rgbfx import gradient, render_hub_port, render_strimer_port, thermal
from unihub.themes import THEMES, apply_theme

from .fakes import HUB_RESPONSES, STRIMER_RESPONSES, FakeTransport
from .test_control import Rig, _req, server  # noqa: F401  (fixture)


def test_gradient_and_thermal_endpoints():
    stops = [(0, 0, 0), (200, 100, 0)]
    assert gradient(stops, 0) == (0, 0, 0)
    assert gradient(stops, 0.5) == (100, 50, 0)
    assert gradient(stops, 2) == (200, 100, 0)
    assert thermal(20, 35, 85) == (0, 90, 255)
    assert thermal(99, 35, 85) == (255, 40, 0)
    assert thermal(None, 35, 85) == (255, 0, 255)


def test_hub_gradient_spans_all_ports_by_fan_position():
    entry = {"mode": "Gradient", "colors": ["#000000", "#ff0000"], "span_all": True, "ring": "outer"}
    fans = [2, 0, 1, 1]  # 4 fans total -> positions 0..3
    inner, outer = render_hub_port(entry, 2, fans)
    assert inner == [(0, 0, 0)] * 48  # ring=outer blanks inner
    assert outer[0] == (170, 0, 0)  # port 3's only fan is global fan #2 of 0..3
    assert outer[12] == (0, 0, 0)  # fan slots beyond the port's count stay dark
    _, outer0 = render_hub_port(entry, 0, fans)
    assert outer0[0] == (0, 0, 0) and outer0[12] == (85, 0, 0)


def test_hub_custom_pads_and_strimer_custom():
    inner, outer = render_hub_port({"mode": "Custom", "custom": {"inner": ["#010203"] * 3}}, 0, [3] * 4)
    assert inner[:3] == [(1, 2, 3)] * 3 and inner[3] == (0, 0, 0) and len(inner) == 48 and len(outer) == 72
    leds = render_strimer_port({"mode": "Custom", "custom": {"leds": ["#ffffff"]}}, 0, 10)
    assert leds[0] == (255, 255, 255) and leds[1] == (0, 0, 0) and len(leds) == 27


def test_hub_set_leds_packets():
    t = FakeTransport(HUB_RESPONSES)
    h = UniHubALV2(t, sleep=lambda s: None)
    h.set_leds(3, [(1, 2, 3)] * 48, [(4, 5, 6)] * 72, brightness=2)
    assert [o[:5] for o in t.outputs] == [bytes([0xE0, 0x36, 1, 3, 2]), bytes([0xE0, 0x37, 4, 6, 5])]
    assert t.features == [
        bytes([0xE0, 0x16, 1, 0, 0, 2]),
        bytes([0xE0, 0x17, 1, 0, 0, 2]),
        bytes([0xE0, 0x10, 0x60, 4, 3, 0]),
        bytes([0xE0, 0x60, 0, 1]),
    ]
    with pytest.raises(ValueError):
        h.set_leds(0, [(0, 0, 0)] * 47, None)


def test_strimer_set_leds_packets():
    t = FakeTransport(STRIMER_RESPONSES)
    s = StrimerPlus(t, sleep=lambda s: None)
    s.read_channel2_ports()
    t.features.clear()
    s.set_leds({7: [(9, 8, 7)] * 27}, brightness=-1)
    assert t.features[0] == bytes([0xE0, 0x17, 1, 0, 0, 0xFF])
    assert t.outputs[0][:5] == bytes([0xE0, 0x37, 9, 7, 8])


def test_every_theme_validates_and_controller_applies_it():
    for name in THEMES:
        cfg = cfgmod.validate(apply_theme(cfgmod.validate({}), name))
        assert cfg["theme"] == name
    r = Rig()
    r.ctrl.tick()
    r.hub_t.outputs.clear(); r.str_t.outputs.clear()
    r.ctrl.apply_config(apply_theme(r.ctrl.cfg, "Cyberpunk"))
    inner = [o for o in r.hub_t.outputs if o[1] == 0x30][0]
    outer = [o for o in r.hub_t.outputs if o[1] == 0x31][0]
    assert inner[2:5] == bytes([0xFF, 0xC8, 0x00])  # #ff00c8 as R,B,G
    assert outer[2:5] == bytes([0x00, 0xFF, 0xF0])  # #00f0ff as R,B,G
    assert r.str_t.outputs  # strimer got its Mixing palette


def test_temperature_lighting_resends_only_when_bucket_moves():
    r = Rig(temps={"cpu": 40.0})
    cfg = apply_theme(r.ctrl.cfg, "Thermal")
    cfg["strimer"]["lighting"]["sensor"] = "cpu"
    r.ctrl.apply_config(cfg)
    r.ctrl.tick()
    n = len(r.hub_t.outputs)
    r.sensors.values["cpu"] = 40.4
    r.ctrl.tick()
    assert len(r.hub_t.outputs) == n
    r.sensors.values["cpu"] = 70.0
    r.ctrl.tick()
    assert len(r.hub_t.outputs) == n + 8  # 4 ports x 2 rings re-rendered
    hot = r.hub_t.outputs[-1]
    assert hot[2] > hot[4]  # red channel above green


def test_strimer_per_port_mixes_hardware_and_software():
    r = Rig()
    r.ctrl.tick()
    cfg = json.loads(json.dumps(r.ctrl.cfg))
    cfg["strimer"]["per_port"] = True
    cfg["strimer"]["ports"][0] = {"mode": "Static", "colors": ["#00ff00"]}
    cfg["strimer"]["ports"][1] = {"mode": "Gradient", "colors": ["#ff0000", "#0000ff"], "span_all": False}
    r.str_t.outputs.clear()
    r.ctrl.apply_config(cfg)
    by_port = {o[1] & 0x0F: o for o in r.str_t.outputs}
    assert by_port[0][2:5] == bytes([0, 0, 255])  # green -> R,B,G = 0,0,255
    assert by_port[1][2:5] == bytes([255, 0, 0]) and by_port[1][-3:] == bytes([0, 255, 0])  # red .. blue along the strip
    assert len(by_port) == 10


def test_http_theme_endpoint(server):  # noqa: F811
    r, port = server
    code, cfg = _req(port, "/api/theme", body={"name": "Stealth"}, headers={"X-UniHub": "1"})
    assert code == 200 and cfg["theme"] == "Stealth" and cfg["hub"]["lighting"][0]["mode"] == "Off"
    code, err = _req(port, "/api/theme", body={"name": "Nope"}, headers={"X-UniHub": "1"})
    assert code == 400
    code, m = _req(port, "/api/meta")
    assert {t["name"] for t in m["themes"]} == set(THEMES)
