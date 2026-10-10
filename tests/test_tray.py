import json

import pytest

from louver import config as cfgmod
from louver import tray

from .test_control import Rig, _req, server  # noqa: F401  (fixture)


def _cfg(**over):
    cfg = cfgmod.validate({})
    for i, (fans, mode) in enumerate(over.get("groups", [(3, "quiet"), (0, "standard"), (2, "curve"), (1, "mb_sync")])):
        cfg["hub"]["groups"][i].update(fans=fans, mode=mode)
    cfg["motherboard"]["headers"] = over.get("headers", [])
    return cfgmod.validate(cfg)


def _state(rpms=(900, 0, 1100, 700), temps=(51.0, None, 51.0, None), headers=(), connected=True, writable=True,
           failsafe=()):
    return {
        "hub": {"connected": connected, "groups": [
            {"rpm": r, "temp": t, "failsafe": i in failsafe} for i, (r, t) in enumerate(zip(rpms, temps))]},
        "motherboard": {"present": True, "writable": writable,
                        "headers": [{"index": i, "rpm": r, "temp": t, "failsafe": False} for i, r, t in headers]},
    }


# ---------------- what the tray lists ----------------
def test_zones_are_ports_with_fans_and_louver_controlled_headers():
    cfg = _cfg(headers=[{"index": 7, "mode": "bios"}, {"index": 2, "mode": "curve", "name": "Top"},
                        {"index": 5, "mode": "quiet"}])
    st = _state(headers=[(2, 1450, 40.0), (7, 3020, None)])  # 5 is configured but the board doesn't report it
    zs = tray.zones(cfg, st)
    assert [z.key for z in zs] == ["hub-0", "hub-2", "hub-3", "mb-2"]  # no empty port, no BIOS pump, no ghost
    assert zs[0] == tray.Zone("hub-0", "Port 1", "quiet", 900, False, 50)
    assert zs[3].name == "Top" and zs[3].rpm == 1450


def test_read_only_motherboard_headers_are_left_out():
    cfg = _cfg(headers=[{"index": 2, "mode": "curve"}])
    assert [z.key for z in tray.zones(cfg, _state(headers=[(2, 1450, 40.0)], writable=False))] == ["hub-0", "hub-2", "hub-3"]


def test_labels():
    z = tray.Zone("hub-0", "Side", "quiet", 900, False, 50)
    assert tray.zone_label(z) == "Side · Quiet · 900 rpm"
    assert tray.zone_label(z._replace(mode="manual", manual_percent=42.4)) == "Side · Fixed 42% · 900 rpm"
    assert tray.zone_label(z._replace(mode="mb_sync", failsafe=True)) == "⚠ Side · MB PWM Sync · 900 rpm"


def test_status_line_shows_followed_temperatures():
    cfg = _cfg(headers=[{"index": 2, "mode": "curve", "sensor": "gpu"}])
    labels = {"cpu": "CPU", "gpu": "GPU"}
    assert tray.status_line(cfg, _state(headers=[(2, 1450, 33.4)]), labels) == "CPU 51°C · GPU 33°C"
    assert tray.status_line(cfg, _state(), {}) == "cpu 51°C"  # labels not loaded yet: the id will do
    assert tray.status_line(cfg, _state(failsafe={2}), labels) == "CPU 51°C · no temperature: fans at 100%"
    assert tray.status_line(cfg, _state(connected=False), labels) == "UNI HUB not connected"
    assert tray.status_line(None, None, labels) == "Louver service not running"
    assert tray.status_line(_cfg(groups=[(0, "quiet")] * 4), _state(), labels) == "Louver"


def test_signature_ignores_live_readings_but_not_choices():
    cfg, themes = _cfg(), ["Aurora", "Stealth"]
    base = tray.signature(cfg, _state(), themes)
    assert tray.signature(cfg, _state(rpms=(1, 2, 3, 4), temps=(80.0,) * 4), themes) == base
    moved = json.loads(json.dumps(cfg)); moved["hub"]["groups"][0]["mode"] = "full"
    assert tray.signature(moved, _state(), themes) != base
    themed = json.loads(json.dumps(cfg)); themed["theme"] = "Stealth"
    assert tray.signature(themed, _state(), themes) != base
    renamed = json.loads(json.dumps(cfg)); renamed["hub"]["groups"][2]["name"] = "Top"
    assert tray.signature(renamed, _state(), themes) != base
    assert tray.signature(None, None, themes) != base


# ---------------- set_zone_mode ----------------
def test_set_zone_mode_hub_and_header():
    cfg = _cfg(headers=[{"index": 2, "mode": "curve"}])
    out = cfgmod.set_zone_mode(cfg, "hub-3", "full")
    assert out["hub"]["groups"][3]["mode"] == "full" and cfg["hub"]["groups"][3]["mode"] == "mb_sync"  # a copy
    assert out["hub"]["groups"][3]["curve"] == cfg["hub"]["groups"][3]["curve"]  # the custom curve survives
    out = cfgmod.set_zone_mode(cfg, "mb-2", "quiet")
    assert out["motherboard"]["headers"] == [{**cfg["motherboard"]["headers"][0], "mode": "quiet"}]
    out = cfgmod.validate(cfgmod.set_zone_mode(cfg, "mb-9", "high"))  # no entry yet: one is made
    assert {h["index"]: h["mode"] for h in out["motherboard"]["headers"]} == {2: "curve", 9: "high"}


@pytest.mark.parametrize("zone, mode", [
    ("hub-4", "quiet"), ("hub--1", "quiet"), ("hub-01", "quiet"), ("hub-", "quiet"), ("mb-0", "quiet"),
    ("mb-17", "quiet"), ("strimer", "quiet"), ("fan-1", "quiet"), (None, "quiet"), (["hub-0"], "quiet"),
    ("hub-0", "bios"), ("mb-2", "mb_sync"), ("hub-0", "turbo"), ("hub-0", None), ("hub-0", ["quiet"]),
])
def test_set_zone_mode_rejects(zone, mode):
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.set_zone_mode(_cfg(), zone, mode)


# ---------------- the endpoint ----------------
def test_http_zone_mode(server):  # noqa: F811
    r, port = server
    rev = r.ctrl.state()["config_rev"]
    code, cfg = _req(port, "/api/zone-mode", body={"zone": "hub-1", "mode": "high"}, headers={"X-Louver": "1"})
    assert code == 200 and cfg["hub"]["groups"][1]["mode"] == "high" and r.ctrl.cfg["hub"]["groups"][1]["mode"] == "high"
    assert r.ctrl.state()["config_rev"] == rev + 1
    code, err = _req(port, "/api/zone-mode", body={"zone": "hub-9", "mode": "high"}, headers={"X-Louver": "1"})
    assert code == 400 and "unknown zone" in err["error"]
    code, _ = _req(port, "/api/zone-mode", body={"zone": "hub-1", "mode": "full"})  # no X-Louver: refused
    assert code == 403 and r.ctrl.cfg["hub"]["groups"][1]["mode"] == "high"
    assert r.ctrl.state()["config_rev"] == rev + 1  # failed requests don't count as changes


def test_every_config_change_bumps_the_revision():
    r = Rig()
    rev = r.ctrl.state()["config_rev"]
    r.ctrl.apply_config(json.loads(json.dumps(r.ctrl.cfg)))
    r.ctrl.update_config(lambda cfg: cfgmod.set_zone_mode(cfg, "hub-0", "quiet"))
    assert r.ctrl.state()["config_rev"] == rev + 2
    with pytest.raises(cfgmod.ConfigError):
        r.ctrl.update_config(lambda cfg: cfgmod.set_zone_mode(cfg, "hub-0", "nope"))
    assert r.ctrl.state()["config_rev"] == rev + 2
