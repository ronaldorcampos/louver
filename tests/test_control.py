import json
import threading
import urllib.error
import urllib.request

import pytest

from unihub import config as cfgmod
from unihub.curves import Governor, interpolate, normalize
from unihub.daemon import Controller, serve
from unihub.devices import StrimerPlus, UniHubALV2

from .fakes import Z, HUB_RESPONSES, STRIMER_RESPONSES, FakeSensors, FakeTransport


# ---------------- curves ----------------
def test_interpolate_clamps_and_is_linear():
    pts = normalize([[60, 50], [40, 20], [80, 100]])
    assert pts[0] == (40, 20)
    assert interpolate(pts, 10) == 20
    assert interpolate(pts, 50) == 35
    assert interpolate(pts, 95) == 100


def test_governor_rises_fast_falls_slow():
    g = Governor(up_per_s=25, down_per_s=4)
    assert g.step(30, 1) == 30
    assert g.step(100, 1) == 55
    assert g.step(20, 1) == 51


# ---------------- config ----------------
def test_validate_fills_defaults_and_rejects_bad_values():
    cfg = cfgmod.validate({})
    assert len(cfg["hub"]["groups"]) == 4 and cfg["on_exit"] == "mb_sync"
    bad = cfgmod.validate({})
    bad["hub"]["groups"][0]["mode"] = "turbo"
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.validate(bad)
    bad = cfgmod.validate({})
    bad["hub"]["lighting"][2]["colors"] = ["#zzzzzz"]
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.validate(bad)


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "c.json"
    cfg = cfgmod.validate({})
    cfg["hub"]["groups"][1]["name"] = "Rear"
    cfgmod.save(cfg, p)
    assert cfgmod.load(p)["hub"]["groups"][1]["name"] == "Rear"


# ---------------- controller ----------------
class Rig:
    def __init__(self, cfg=None, temps=None):
        self.hub_t = FakeTransport(HUB_RESPONSES)
        self.str_t = FakeTransport(STRIMER_RESPONSES)
        self.now = 0.0
        self.opens = 0
        self.sensors = FakeSensors(temps if temps is not None else {"cpu": 50.0})
        self.ctrl = Controller(cfg or {}, None, self.sensors, opener=self.open, clock=lambda: self.now)

    def open(self, skip=frozenset()):
        self.opens += 1
        out = {}
        if UniHubALV2.PID not in skip:
            out[UniHubALV2.PID] = UniHubALV2(self.hub_t, sleep=lambda s: None)
        if StrimerPlus.PID not in skip:
            out[StrimerPlus.PID] = StrimerPlus(self.str_t, sleep=lambda s: None)
        return out

    def speeds(self):
        return {f[1] & 0x0F: f[3] for f in self.hub_t.features if f[1] & 0xF0 == 0x20 and len(f) == 4}


def cfg_with_modes(*modes, **extra):
    cfg = cfgmod.validate({})
    for i, m in enumerate(modes):
        cfg["hub"]["groups"][i]["mode"] = m
    cfg.update(extra)
    return cfg


def test_first_tick_initialises_hub_and_applies_modes():
    r = Rig(cfg_with_modes("standard", "manual", "full", "mb_sync"))
    r.ctrl.tick()
    st = r.ctrl.state()
    assert st["hub"]["connected"] and st["hub"]["firmware"] == "v1.2"
    assert st["strimer"]["connected"] and st["strimer"]["ports"] == 10
    # standard @ 50 C = 45 %, manual default 50 %, full = 100 %, mb_sync untouched
    assert r.speeds() == {0: 45, 1: 50, 2: 100}
    assert Z([0xE0, 0x10, 0x62, 0x88, 0, 0]) in r.hub_t.features  # port 4 handed to MB
    assert Z([0xE0, 0x10, 0x62, 0x10, 0, 0]) in r.hub_t.features  # port 1 taken from MB
    assert [g["rpm"] for g in st["hub"]["groups"]] == [1200, 800, 0, 1950]


def test_missing_temperature_triggers_failsafe_full_speed():
    r = Rig(cfg_with_modes("quiet", "quiet", "quiet", "quiet"), temps={"cpu": None})
    r.ctrl.tick()
    assert r.speeds() == {0: 100, 1: 100, 2: 100, 3: 100}
    assert all(g["failsafe"] for g in r.ctrl.state()["hub"]["groups"])


def test_min_percent_floor_applies():
    cfg = cfg_with_modes("manual", "manual", "manual", "manual", min_percent=25)
    for g in cfg["hub"]["groups"]:
        g["manual_percent"] = 0
    r = Rig(cfg)
    r.ctrl.tick()
    assert set(r.speeds().values()) == {25}


def test_speed_only_resent_on_change_or_keepalive():
    r = Rig(cfg_with_modes("manual", "manual", "manual", "manual"))
    r.ctrl.tick()
    n = len(r.hub_t.features)
    r.now = 1.0
    r.ctrl.tick()
    assert sum(1 for f in r.hub_t.features[n:] if f[1] & 0xF0 == 0x20) == 0
    r.now = 11.0
    r.ctrl.tick()
    assert sum(1 for f in r.hub_t.features[n:] if f[1] & 0xF0 == 0x20) == 4


def test_io_error_drops_device_then_reconnects():
    r = Rig()
    r.ctrl.tick()
    assert r.opens == 1
    r.hub_t.fail = True
    r.now = 11.0
    r.ctrl.tick()
    assert not r.ctrl.state()["hub"]["connected"] and "hub disconnected" in r.ctrl.error
    r.hub_t = FakeTransport(HUB_RESPONSES)
    r.now = 20.0
    r.ctrl.tick()
    assert r.ctrl.state()["hub"]["connected"]
    assert r.opens == 2
    assert r.ctrl.strimer is not None  # strimer was kept, not re-opened


def test_apply_config_pushes_only_changed_things():
    r = Rig()
    r.ctrl.tick()
    r.hub_t.features.clear(); r.hub_t.outputs.clear()
    new = json.loads(json.dumps(r.ctrl.cfg))
    new["hub"]["lighting"][2]["mode"] = "Static"
    new["hub"]["groups"][0]["mode"] = "mb_sync"
    r.ctrl.apply_config(new)
    assert {o[1] for o in r.hub_t.outputs} == {0x34, 0x35}  # only port 3's rings
    assert Z([0xE0, 0x10, 0x62, 0x11, 0, 0]) in r.hub_t.features


def test_shutdown_hands_fans_back_to_motherboard():
    r = Rig()
    r.ctrl.tick()
    r.hub_t.features.clear()
    r.ctrl.shutdown()
    assert r.hub_t.features == [Z([0xE0, 0x10, 0x62, (1 << (i + 4)) | (1 << i), 0, 0]) for i in range(4)]
    assert r.hub_t.closed and r.str_t.closed


def test_shutdown_fixed_speed():
    r = Rig(cfg_with_modes("quiet", "quiet", "quiet", "mb_sync", on_exit="fixed", on_exit_percent=70))
    r.ctrl.tick()
    r.hub_t.features.clear()
    r.ctrl.shutdown()
    assert r.hub_t.features == [Z([0xE0, 0x20 | i, 0, 70]) for i in range(3)]


# ---------------- HTTP ----------------
@pytest.fixture
def server():
    r = Rig()
    r.ctrl.tick()
    httpd = serve(r.ctrl, "127.0.0.1:0") if False else None
    # bind an ephemeral port: serve() needs a concrete one, so pick it first
    import socket

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    httpd = serve(r.ctrl, f"127.0.0.1:{port}")
    yield r, port
    httpd.shutdown()


def _req(port, path, body=None, headers=None, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET" if body is None else "PUT",
                                 data=None if body is None else json.dumps(body).encode())
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read()) if resp.headers["Content-Type"] == "application/json" else resp.read()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_state_and_static(server):
    _, port = server
    code, st = _req(port, "/api/state")
    assert code == 200 and st["hub"]["connected"]
    code, html = _req(port, "/")
    assert code == 200 and b"UniHub" in html
    assert _req(port, "/../../etc/passwd")[0] == 404


def test_http_rejects_foreign_host_and_missing_header(server):
    r, port = server
    assert _req(port, "/api/state", host="evil.example:80")[0] == 403
    assert _req(port, "/api/config", body=r.ctrl.cfg)[0] == 403  # no X-UniHub header


def test_http_put_config_validates(server):
    r, port = server
    cfg = json.loads(json.dumps(r.ctrl.cfg))
    cfg["hub"]["groups"][0]["mode"] = "turbo"
    code, err = _req(port, "/api/config", body=cfg, headers={"X-UniHub": "1"})
    assert code == 400 and "mode" in err["error"]
    cfg["hub"]["groups"][0]["mode"] = "full"
    code, new = _req(port, "/api/config", body=cfg, headers={"X-UniHub": "1"})
    assert code == 200 and new["hub"]["groups"][0]["mode"] == "full"
    assert r.speeds()[0] == 100


def test_serve_refuses_non_loopback():
    r = Rig()
    with pytest.raises(ValueError):
        serve(r.ctrl, "0.0.0.0:47999")


def test_partially_unreadable_composite_sensor_is_reported():
    cfg = cfg_with_modes("standard", "standard", "standard", "standard")
    cfg["hub"]["groups"][3]["sensor"] = "max:cpu,gpu"
    r = Rig(cfg)
    r.ctrl.sensors = FakeSensors({"cpu": 50.0, "gpu": None, "max:cpu,gpu": 50.0})
    r.ctrl.tick()
    g = r.ctrl.state()["hub"]["groups"]
    assert g[3]["missing"] == ["gpu"] and not g[3]["failsafe"]
    assert g[0]["missing"] == []


def test_autostart_entry_managed_and_only_removes_its_own(tmp_path):
    from unihub import autostart

    p = tmp_path / "autostart" / "unihub.desktop"
    autostart.sync(True, p, launcher="/x/unihub")
    text = p.read_text()
    assert "Exec=/x/unihub app --autostart" in text and autostart.MARKER in text
    autostart.sync(False, p)
    assert not p.exists()
    p.write_text("[Desktop Entry]\nExec=something-the-user-made\n")
    autostart.sync(False, p)
    assert p.exists()  # not ours: left alone


def test_app_settings_validate():
    cfg = cfgmod.validate({})
    assert cfg["app"] == {"start_on_login": False, "start_minimized": True, "close_to_tray": True}
    legacy = cfgmod.validate({})
    legacy["app"]["show_tray"] = False
    assert "show_tray" not in cfgmod.validate(legacy)["app"]  # retired key from older configs is dropped
    bad = cfgmod.validate({})
    bad["app"]["close_to_tray"] = "yes"
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.validate(bad)
