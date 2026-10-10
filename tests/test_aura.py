"""The ASUS Aura mainboard driver (a port of OpenRGB's), its probe command,
the daemon's motherboard RGB zone, its config and its udev rule. All against
fakes: nothing here opens a real /dev/hidraw node."""

import json
import os
import re
from pathlib import Path

import pytest

from louver import __main__ as cli
from louver import aura, instance
from louver import config as cfgmod
from louver.aura import AuraMainboard, decode_config
from louver.daemon import RECONNECT_EVERY
from louver.hidraw import HidInfo, HidrawTransport
from louver.themes import apply_theme

from .fakes import FakeAuraTransport, aura_table
from .test_control import Rig, _req, server  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parent.parent
# 0b05:19af's report descriptor, as the board in hand reports it.
AURA_DESCRIPTOR = bytes.fromhex(
    "06 72 ff 09 a1 a1 01 85 ec 09 10 15 00 26 ff 00 75 08 95 40 81 02 09 11 15 00 26 ff 00 75 08 95 40 91 02 c0"
)


def P(*payload: int) -> bytes:
    """One output report as the controller gets it: report ID 0xEC, padded to 65 bytes."""
    return bytes([0xEC, *payload]).ljust(65, b"\0")


def board(table=None, firmware=b"AULA3-AR32-0207"):
    t = FakeAuraTransport(firmware=firmware, table=table)
    dev = AuraMainboard(t, clock=lambda: t.clock)
    return dev, t


# ---------------- config table ----------------
def test_decode_config_onboard_then_argb_headers():
    chans = decode_config(aura_table(argb=3, onboard=5, rgb_headers=1))
    assert [(c.key, c.kind, c.effect_channel, c.num_leds, c.rgb_headers) for c in chans] == [
        ("onboard", "onboard", 0, 5, 1),
        ("argb-1", "argb", 1, 1, 0),
        ("argb-2", "argb", 2, 1, 0),
        ("argb-3", "argb", 3, 1, 0),
    ]
    assert [c.name for c in chans] == ["Onboard LEDs", "ARGB header 1", "ARGB header 2", "ARGB header 3"]


def test_decode_config_edge_cases_follow_openrgb():
    # More 12 V headers than onboard LEDs: OpenRGB counts none.
    assert decode_config(aura_table(argb=0, onboard=2, rgb_headers=3))[0].rgb_headers == 0
    # No onboard LEDs: the ARGB headers start at effect channel 0.
    chans = decode_config(aura_table(argb=2, onboard=0, rgb_headers=0))
    assert [(c.key, c.index, c.effect_channel) for c in chans] == [("argb-1", 0, 0), ("argb-2", 1, 1)]
    assert decode_config(bytes(60)) == []


# ---------------- queries ----------------
def test_firmware_and_config_are_two_queries_only():
    dev, t = board()
    assert dev.read_firmware() == "AULA3-AR32-0207"
    chans = dev.read_config()
    assert t.writes == [P(0x82), P(0xB0)]
    assert dev.config_table == aura_table() and [c.key for c in chans] == ["onboard", "argb-1", "argb-2", "argb-3"]


def test_firmware_string_stops_at_nul_and_is_16_bytes_at_most():
    dev, _ = board(firmware=b"ABC\0junk")
    assert dev.read_firmware() == "ABC"
    dev, _ = board(firmware=b"0123456789abcdefOVERFLOW")
    assert dev.read_firmware() == "0123456789abcdef"


def test_request_skips_unrelated_reports_and_times_out():
    dev, t = board()
    t.queue.append(bytes([0xEC, 0x02]) + b"late firmware")  # a stale reply to an earlier query
    t.queue.append(bytes([0xAA, 0x30]))  # some other report ID
    dev.read_config()
    assert [c.key for c in dev.channels] == ["onboard", "argb-1", "argb-2", "argb-3"]
    t.silent.add(0xB0)
    with pytest.raises(TimeoutError):
        dev.read_config()


# ---------------- effects ----------------
def test_set_effect_sends_effect_then_colour_mask_like_openrgb():
    dev, t = board()
    dev.read_config()
    t.writes.clear()
    dev.set_effect(2, aura.MODES["Static"], (0, 255, 0))  # argb-2: after 5 onboard LEDs and argb-1
    colour = bytearray(P(0x36, 0x00, 0x40, 0x00))
    colour[5 + 3 * 6 : 5 + 3 * 7] = bytes([0, 255, 0])
    assert t.writes == [P(0x35, 2, 0x00, 0x00, 1), bytes(colour)]
    t.writes.clear()
    dev.set_effect(0, aura.MODES["Breathing"], (0x11, 0x22, 0x33))
    colour = bytearray(P(0x36, 0x00, 0x1F, 0x00))
    colour[5:20] = bytes([0x11, 0x22, 0x33]) * 5
    assert t.writes == [P(0x35, 0, 0x00, 0x00, 2), bytes(colour)]


def test_fits_is_whether_the_effect_colour_fits_one_report():
    dev, _ = board(table=aura_table(argb=2, onboard=19, rgb_headers=0))
    dev.read_config()
    # Colour bytes start at 5 + 3 * (LEDs before): onboard ends at 62, argb-1 at 65, argb-2 would end at 68.
    assert [dev.fits(i) for i in range(3)] == [True, True, False]
    dev, _ = board(table=bytes([0xFF]) * 60)
    dev.read_config()
    assert len(dev.channels) == 256 and not any(dev.fits(i) for i in range(256))


def test_set_effect_refuses_bad_input_before_writing():
    dev, t = board(table=aura_table(argb=2, onboard=19, rgb_headers=0))
    dev.read_config()
    t.writes.clear()
    with pytest.raises(ValueError):
        dev.set_effect(4, 1)  # no such channel
    with pytest.raises(ValueError):
        dev.set_effect(0, 256)
    with pytest.raises(ValueError):
        dev.set_effect(0, aura.MODE_DIRECT)  # direct mode isn't ported
    with pytest.raises(ValueError):
        dev.set_effect(2, 1, (255, 0, 0))  # argb-2's colour would land past the 65-byte report
    assert t.writes == []
    # argb-1's mask bit is past the 16-bit mask: OpenRGB truncates it to 0, so do we.
    dev.set_effect(1, 1, (255, 0, 0))
    assert t.writes[1][:4] == bytes([0xEC, 0x36, 0x00, 0x00]) and t.writes[1][5 + 57 : 5 + 60] == bytes([255, 0, 0])


def test_gen1_packet():
    dev, t = board()
    dev.set_gen1()
    assert t.writes == [P(0x52, 0x53, 0x00, 0x01)]


# ---------------- finding the controller ----------------
def _node(sysfs, name, hid_id, descriptor=None):
    d = sysfs / name / "device"
    d.mkdir(parents=True)
    (d / "uevent").write_text(f"HID_ID={hid_id}\nHID_NAME=test {name}\nHID_UNIQ=\nHID_PHYS=usb-x\n")
    if descriptor is not None:
        (d / "report_descriptor").write_bytes(descriptor)


def test_find_controllers_matches_mainboard_pids_and_19af_usage_page(tmp_path):
    keyboard = bytes.fromhex("05010906a101")
    _node(tmp_path, "hidraw0", "0003:00000B05:000019AF", AURA_DESCRIPTOR)
    _node(tmp_path, "hidraw1", "0003:00000B05:000019AF", keyboard)  # another interface: not the Aura one
    _node(tmp_path, "hidraw2", "0003:00000B05:00001866")  # some other ASUS device
    _node(tmp_path, "hidraw3", "0003:00000CF2:0000A104")  # the Lian Li hub
    _node(tmp_path, "hidraw4", "0003:00000B05:000018F3")  # no usage filter for this PID
    _node(tmp_path, "hidraw5", "0003:00000B05:000019AF")  # descriptor unreadable: can't tell, skip
    assert [i.devnode for i in aura.find_controllers(tmp_path)] == ["/dev/hidraw0", "/dev/hidraw4"]


def test_top_usage_reads_the_first_collection():
    assert aura._top_usage(AURA_DESCRIPTOR) == (0xFF72, 0xA1)
    assert aura._top_usage(bytes.fromhex("05010906a101")) == (0x01, 0x06)
    assert aura._top_usage(bytes.fromhex("a101")) is None
    assert aura._top_usage(b"") is None
    # A long item (0xFE, data size, tag, data) is stepped over whole, its data bytes not read as items.
    assert aura._top_usage(bytes.fromhex("fe0310a1a1a1") + AURA_DESCRIPTOR) == (0xFF72, 0xA1)
    assert aura._top_usage(bytes.fromhex("fe")) is None  # truncated: no size byte


def test_hidraw_read_waits_for_one_report(tmp_path):
    r, w = os.pipe()
    t = HidrawTransport.__new__(HidrawTransport)
    t.devnode, t._fd = "pipe", r
    try:
        assert t.read(65, 0.01) == b""
        os.write(w, bytes([0xEC, 0x02]) + b"fw")
        assert t.read(65, 1.0) == bytes([0xEC, 0x02]) + b"fw"
    finally:
        t.close()
        os.close(w)


# ---------------- louver aura-probe ----------------
@pytest.fixture
def probe_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    fakes = []

    def transport(devnode):
        assert devnode == "/nonexistent/hidraw-aura"
        fakes.append(FakeAuraTransport())
        return fakes[-1]

    info = HidInfo("/nonexistent/hidraw-aura", 0x0B05, 0x19AF, "AsusTek Computer Inc. AURA LED Controller", "", "")
    monkeypatch.setattr(cli.aura, "find_controllers", lambda: [info])
    monkeypatch.setattr(cli, "HidrawTransport", transport)
    return fakes, tmp_path


def test_aura_probe_prints_firmware_and_channels_and_only_queries(probe_env, capsys):
    fakes, _ = probe_env
    assert cli.cmd_aura_probe(None, cfgmod.validate({}), Path("x")) == 0
    out = capsys.readouterr().out
    assert "0b05:19af" in out and "firmware AULA3-AR32-0207" in out
    assert re.search(r"onboard +Onboard LEDs +effect channel 0 +5 LEDs, 1 of them 12 V RGB headers", out)
    assert re.search(r"argb-3 +ARGB header 3 +effect channel 3 +LED count not reported", out)
    assert "    00 00 03 00 00 00" in out  # the table's first row: 3 ARGB headers at 0x02
    (t,) = fakes
    assert t.writes == [P(0x82), P(0xB0)] and t.closed  # queries only: nothing that changes the lights


def test_aura_probe_reports_a_dead_controller_and_no_controller(probe_env, monkeypatch, capsys):
    fakes, _ = probe_env
    real = cli.HidrawTransport

    def silent(devnode):
        t = real(devnode)
        t.silent.update({0x82, 0xB0})
        return t

    monkeypatch.setattr(cli, "HidrawTransport", silent)
    monkeypatch.setattr(cli.aura.AuraMainboard, "reply_timeout", 0.02)  # the probe runs on the real clock
    assert cli.cmd_aura_probe(None, cfgmod.validate({}), Path("x")) == 1
    out = capsys.readouterr()
    assert "firmware: no reply" in out.out and "query failed: no reply to Aura request 0xb0" in out.out
    monkeypatch.setattr(cli.aura, "find_controllers", lambda: [])
    assert cli.cmd_aura_probe(None, cfgmod.validate({}), Path("x")) == 1
    assert "no usable ASUS Aura" in capsys.readouterr().err


def test_aura_probe_takes_the_hardware_lock(probe_env, capsys):
    fakes, tmp = probe_env
    held = instance.try_acquire()  # what a running daemon holds
    try:
        assert cli.cmd_aura_probe(None, cfgmod.validate({}), Path("x")) == cli.ALREADY_RUNNING
    finally:
        held.close()
    err = capsys.readouterr().err
    assert fakes == [] and "already running" in err
    # Right whether motherboard RGB is on (Settings shows it) or off (no zone to point at).
    assert "systemctl --user stop louver" in err and "Settings → Motherboard RGB" in err


def test_aura_probe_says_which_channels_louver_skips(probe_env, monkeypatch, capsys):
    fakes, _ = probe_env
    real = cli.HidrawTransport

    def oversized(devnode):
        t = real(devnode)
        t.table = aura_table(argb=2, onboard=19, rgb_headers=0)
        return t

    monkeypatch.setattr(cli, "HidrawTransport", oversized)
    assert cli.cmd_aura_probe(None, cfgmod.validate({}), Path("x")) == 0
    lines = capsys.readouterr().out.splitlines()
    skipped = [line.split()[0] for line in lines if "Louver skips it" in line]
    assert skipped == ["argb-2"] and fakes[0].writes == [P(0x82), P(0xB0)]


def test_aura_probe_is_a_subcommand(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_aura_probe", lambda *a: seen.append(a) or 0)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent")
    assert cli.main(["aura-probe"]) == 0 and len(seen) == 1


# ---------------- config ----------------
def test_motherboard_rgb_is_off_by_default_and_validated():
    m = cfgmod.validate({})["motherboard_rgb"]
    assert m == {
        "enabled": False,
        "per_channel": False,
        "lighting": {"mode": "Rainbow", "colors": ["#ff0000"]},
        "channels": {},
    }
    ok = cfgmod.validate({"motherboard_rgb": {"enabled": True, "channels": {"argb-2": {"mode": "Static"}}}})
    assert ok["motherboard_rgb"]["channels"] == {"argb-2": {"mode": "Static", "colors": ["#ff0000"]}}
    # A config from before motherboard RGB starts it on the theme in use; one that has the section keeps it.
    old = cfgmod.validate({"theme": "Cyberpunk"})["motherboard_rgb"]
    assert old["lighting"] == {"mode": "Static", "colors": ["#ff00c8"]} and old["enabled"] is False
    assert (
        cfgmod.validate({"theme": "Cyberpunk", "motherboard_rgb": {}})["motherboard_rgb"]["lighting"]["mode"]
        == "Rainbow"
    )
    assert cfgmod.validate({"theme": "Gone"})["motherboard_rgb"]["lighting"]["mode"] == "Rainbow"
    # master (d5b59b1) never checks theme, so a list or dict one is kept as it is; it must not crash the seeding.
    for odd in (["Cyberpunk"], {"name": "Cyberpunk"}):
        got = cfgmod.validate({"theme": odd})
        assert got["theme"] == odd and got["motherboard_rgb"]["lighting"]["mode"] == "Rainbow"
    for bad in (
        {"enabled": "yes"},
        {"per_channel": 1},
        {"lighting": {"mode": "Meteor"}},  # a Lian Li effect, not an Aura one
        {"lighting": {"mode": "Static", "colors": ["#ff0000", "#00ff00"]}},
        {"lighting": {"mode": "Static", "colors": []}},
        {"lighting": {"mode": "Static", "colors": ["#zzzzzz"]}},
        {"lighting": {"mode": "Static", "speed": 2}},
        {"lighting": "Static"},
        {"channels": []},
        {"channels": {"argb-0": {"mode": "Static"}}},
        {"channels": {"dram": {"mode": "Static"}}},
        {"surprise": True},
    ):
        with pytest.raises(cfgmod.ConfigError):
            cfgmod.validate({"motherboard_rgb": bad})


# ---------------- daemon ----------------
def aura_cfg(**m):
    cfg = cfgmod.validate({})
    cfg["motherboard_rgb"].update(m)
    return cfgmod.validate(cfg)


def effect_writes(writes):
    """{effect channel: (mode, colour packet)} from a run of effect writes."""
    out = {}
    for eff, col in zip(writes[0::2], writes[1::2]):
        assert eff[1] == 0x35 and col[1] == 0x36
        out[eff[2]] = (eff[5], col)
    return out


def test_disabled_never_opens_the_aura_controller():
    r = Rig()
    r.ctrl.tick()
    r.now = 30.0
    r.ctrl.tick()
    assert r.aura.opens == 0 and r.aura_t.writes == []
    st = r.ctrl.state()["motherboard_rgb"]
    assert st == {
        "enabled": False,
        "connected": False,
        "name": "ASUS Aura LED Controller",
        "firmware": "",
        "channels": [],
        "error": "",
    }


def test_enabled_reads_the_board_then_sets_every_channel():
    r = Rig(aura_cfg(enabled=True))
    r.ctrl.tick()
    w = r.aura_t.writes
    assert w[:3] == [P(0x82), P(0xB0), P(0x52, 0x53, 0x00, 0x01)]
    # Rainbow (5) everywhere; OpenRGB sends black with effects that take no colour.
    masks = {0: (0x00, 0x1F), 1: (0x00, 0x20), 2: (0x00, 0x40), 3: (0x00, 0x80)}
    assert w[3:] == [p for ch, m in masks.items() for p in (P(0x35, ch, 0, 0, 5), P(0x36, *m, 0))]
    st = r.ctrl.state()["motherboard_rgb"]
    assert st["connected"] and st["firmware"] == "AULA3-AR32-0207" and st["error"] == ""
    assert st["channels"][0] == {
        "key": "onboard",
        "name": "Onboard LEDs",
        "kind": "onboard",
        "leds": 5,
        "rgb_headers": 1,
        "note": "",
    }
    assert st["channels"][3] == {
        "key": "argb-3",
        "name": "ARGB header 3",
        "kind": "argb",
        "leds": None,
        "rgb_headers": 0,
        "note": "",
    }


def test_per_channel_looks_and_only_changed_channels_are_resent():
    r = Rig(aura_cfg(enabled=True, per_channel=True, channels={"argb-2": {"mode": "Static", "colors": ["#00ff00"]}}))
    r.ctrl.tick()
    sent = effect_writes(r.aura_t.writes[3:])
    assert {ch: mode for ch, (mode, _) in sent.items()} == {0: 5, 1: 5, 2: 1, 3: 5}  # the rest use the shared look
    assert sent[2][1][5 + 3 * 6 : 5 + 3 * 7] == bytes([0, 255, 0])
    r.aura_t.writes.clear()
    new = json.loads(json.dumps(r.ctrl.cfg))
    new["motherboard_rgb"]["channels"]["argb-2"]["colors"] = ["#0000ff"]
    r.ctrl.apply_config(new)
    assert list(effect_writes(r.aura_t.writes)) == [2]
    r.aura_t.writes.clear()
    new["motherboard_rgb"]["lighting"] = {"mode": "Off", "colors": ["#ff0000"]}  # argb-2 keeps its own look
    r.ctrl.apply_config(new)
    assert sorted(effect_writes(r.aura_t.writes)) == [0, 1, 3]
    r.aura_t.writes.clear()
    new["theme"] = "nothing aura-related changed"
    r.ctrl.apply_config(new)
    assert r.aura_t.writes == []


def test_turning_it_off_closes_the_controller_and_on_opens_it_now():
    r = Rig(aura_cfg(enabled=True))
    r.ctrl.tick()
    first = r.aura_t
    r.ctrl.apply_config(aura_cfg(enabled=False))
    assert first.closed and r.ctrl.aura is None and not r.ctrl.state()["motherboard_rgb"]["connected"]
    n = len(first.writes)
    r.now = 60.0
    r.ctrl.tick()
    assert r.aura.opens == 1 and len(first.writes) == n  # off: never reopened, nothing restored on close
    r.aura_t = FakeAuraTransport()
    r.ctrl.apply_config(aura_cfg(enabled=True))
    assert r.aura.opens == 2 and r.ctrl.state()["motherboard_rgb"]["connected"]


def test_io_error_drops_the_controller_then_reconnects():
    r = Rig(aura_cfg(enabled=True))
    r.ctrl.tick()
    r.aura_t.fail = True
    new = json.loads(json.dumps(r.ctrl.cfg))
    new["motherboard_rgb"]["lighting"]["mode"] = "Static"
    r.ctrl.apply_config(new)
    st = r.ctrl.state()["motherboard_rgb"]
    assert not st["connected"] and st["error"].startswith("disconnected:") and r.aura_t.closed
    r.aura_t = FakeAuraTransport()
    r.now = RECONNECT_EVERY
    r.ctrl.tick()
    assert r.ctrl.state()["motherboard_rgb"]["connected"] and r.aura.opens == 2
    assert effect_writes(r.aura_t.writes[3:])[0][0] == 1  # the new look went out on reconnect


def test_missing_unopenable_or_silent_controller():
    r = Rig(aura_cfg(enabled=True))
    r.aura.present = False
    r.ctrl.tick()
    assert r.ctrl.state()["motherboard_rgb"]["error"] == "" and not r.ctrl.state()["motherboard_rgb"]["connected"]
    r.now = RECONNECT_EVERY
    r.ctrl.tick()
    assert r.aura.finds == 2 and r.aura.opens == 0  # still looking

    def denied(devnode):
        raise PermissionError(13, "no permission to open /dev/hidraw1")

    r.aura.present = True
    r.aura.open = denied
    r.now = 2 * RECONNECT_EVERY
    r.ctrl.tick()
    assert r.ctrl.state()["motherboard_rgb"]["error"].startswith("cannot open:")

    r = Rig(aura_cfg(enabled=True))
    r.aura_t.silent.add(0xB0)
    r.ctrl.tick()
    st = r.ctrl.state()["motherboard_rgb"]
    assert not st["connected"] and "init failed" in st["error"] and r.aura_t.closed
    assert [w[1] for w in r.aura_t.writes] == [0x82, 0xB0]  # gave up before any command


def test_firmware_without_reply_is_tolerated_like_openrgb():
    r = Rig(aura_cfg(enabled=True))
    r.aura_t.silent.add(0x82)
    r.ctrl.tick()
    st = r.ctrl.state()["motherboard_rgb"]
    assert st["connected"] and st["firmware"] == "" and len(st["channels"]) == 4


def test_shutdown_leaves_the_lights_as_they_are():
    r = Rig(aura_cfg(enabled=True))
    r.ctrl.tick()
    n = len(r.aura_t.writes)
    r.ctrl.shutdown()
    assert r.aura_t.closed and len(r.aura_t.writes) == n and r.ctrl.aura is None


def test_theme_sets_every_aura_channel():
    r = Rig(aura_cfg(enabled=True, per_channel=True, channels={"onboard": {"mode": "Rainbow"}}))
    r.ctrl.tick()
    r.aura_t.writes.clear()
    r.ctrl.update_config(lambda cfg: apply_theme(cfg, "Cyberpunk"))
    m = r.ctrl.cfg["motherboard_rgb"]
    assert m["per_channel"] is False and m["lighting"] == {"mode": "Static", "colors": ["#ff00c8"]}
    sent = effect_writes(r.aura_t.writes)
    assert {ch: mode for ch, (mode, _) in sent.items()} == {0: 1, 1: 1, 2: 1, 3: 1}
    assert sent[0][1][5:8] == bytes([0xFF, 0x00, 0xC8])  # R, G, B: no channel swap on Aura


def test_http_state_and_meta_carry_motherboard_rgb(server):  # noqa: F811
    r, port = server
    code, st = _req(port, "/api/state")
    assert code == 200 and st["motherboard_rgb"]["enabled"] is False
    code, m = _req(port, "/api/meta")
    modes = {x["name"]: x["max_colors"] for x in m["aura_lighting_modes"]}
    assert modes["Static"] == 1 and modes["Rainbow"] == 0 and "Direct" not in modes and len(modes) == 18
    cfg = json.loads(json.dumps(r.ctrl.cfg))
    cfg["motherboard_rgb"]["enabled"] = True
    code, new = _req(port, "/api/config", body=cfg)
    assert code == 200 and new["motherboard_rgb"]["enabled"]
    assert _req(port, "/api/state")[1]["motherboard_rgb"]["connected"]


# ---------------- packaging and provenance ----------------
def test_udev_rule_grants_the_aura_pids_only():
    rules = (ROOT / "packaging" / "99-louver.rules").read_text().splitlines()
    asus = [line for line in rules if "0b05" in line and not line.startswith("#")]
    pids = []
    for line in asus:
        m = re.fullmatch(
            r'SUBSYSTEM=="hidraw", ATTRS\{idVendor\}=="0b05", ATTRS\{idProduct\}=="([0-9a-f]{4})", '
            r'MODE="0660", GROUP="plugdev", TAG\+="uaccess"',
            line,
        )
        assert m, line
        pids.append(int(m.group(1), 16))
    assert pids == list(aura.MAINBOARD_PIDS) and 0x19AF in pids


def test_notice_names_the_ported_openrgb_commit_and_files():
    notice = (ROOT / "NOTICE").read_text()
    assert aura.OPENRGB_COMMIT in notice and aura.OPENRGB_COMMIT in aura.__doc__
    for name in (
        "AsusAuraUSBController.h, .cpp",
        "AsusAuraMainboardController.h, .cpp",
        "RGBController_AsusAuraUSB.cpp",
    ):
        assert name in notice and name.split(",")[0].removesuffix(".h") in aura.__doc__
    assert "GPL-2.0-or-later" in aura.__doc__


# ---------------- review round: bad tables, pacing, liveness ----------------
def test_oversized_table_drives_the_channels_that_fit_and_notes_the_rest():
    r = Rig(aura_cfg(enabled=True))
    r.aura_t.table = aura_table(argb=3, onboard=19, rgb_headers=0)
    r.ctrl.tick()
    w = r.aura_t.writes
    assert w[:3] == [P(0x82), P(0xB0), P(0x52, 0x53, 0x00, 0x01)]
    assert sorted(effect_writes(w[3:])) == [0, 1]  # argb-2 and argb-3 never get a packet
    st = r.ctrl.state()["motherboard_rgb"]
    assert st["connected"] and [c["note"] for c in st["channels"]] == [
        "",
        "",
        "its LEDs don't fit one effect report",
        "its LEDs don't fit one effect report",
    ]
    n = len(w)
    for t in range(1, 31):
        r.now = float(t)
        r.ctrl.tick()
    new = json.loads(json.dumps(r.ctrl.cfg))
    new["motherboard_rgb"]["lighting"]["mode"] = "Static"
    r.ctrl.apply_config(new)
    assert sorted(effect_writes(w[n:])) == [0, 1] and r.aura.opens == 1  # a change skips them too; no reconnects


def test_garbage_table_sends_nothing_and_is_not_retried_until_something_changes():
    r = Rig(aura_cfg(enabled=True))
    r.aura_t.table = bytes([0xFF]) * 60  # 255 onboard LEDs, 255 ARGB headers: nothing fits
    r.ctrl.tick()
    st = r.ctrl.state()["motherboard_rgb"]
    assert not st["connected"] and "nothing to drive (255 onboard LEDs, 255 ARGB headers" in st["error"]
    assert r.aura_t.writes == [P(0x82), P(0xB0)] and r.aura_t.closed  # no Gen1, no effects
    for t in range(1, 31):
        r.now = float(t)
        r.ctrl.tick()
    assert r.aura.opens == 1 and r.aura_t.writes == [P(0x82), P(0xB0)]  # no retry storm
    assert r.aura.finds == 7  # it still looks (sysfs only) every RECONNECT_EVERY
    r.aura.ident = 2  # the controller re-enumerated: worth one more try
    r.now = 35.0
    r.ctrl.tick()
    assert r.aura.opens == 2 and len(r.aura_t.writes) == 4
    r.now = 40.0
    r.ctrl.tick()
    assert r.aura.opens == 2
    r.ctrl.apply_config(aura_cfg(enabled=False))
    r.ctrl.apply_config(aura_cfg(enabled=True))  # turning it off and on retries at once
    assert r.aura.opens == 3


def test_empty_table_is_unusable_too():
    r = Rig(aura_cfg(enabled=True))
    r.aura_t.table = bytes(60)
    r.ctrl.tick()
    assert "nothing to drive (0 onboard LEDs, 0 ARGB headers)" in r.ctrl.state()["motherboard_rgb"]["error"]
    assert r.aura_t.writes == [P(0x82), P(0xB0)]


def test_connect_attempts_are_paced_and_unrelated_saves_dont_add_any():
    r = Rig(aura_cfg(enabled=True))
    r.aura_t.silent.update({0x82, 0xB0})  # a controller that never answers: every attempt fails
    for t in range(21):
        r.now = float(t)
        r.ctrl.tick()
        new = json.loads(json.dumps(r.ctrl.cfg))
        new["min_percent"] = t % 7  # a save that has nothing to do with the motherboard
        r.ctrl.apply_config(new)
    assert r.aura.opens == 5  # t = 0, 5, 10, 15, 20
    assert "init failed" in r.ctrl.state()["motherboard_rgb"]["error"]


def test_turning_it_on_connects_at_once_then_retries_at_the_usual_pace():
    r = Rig()
    r.aura.present = False
    r.now = 2.0
    r.ctrl.tick()
    r.ctrl.apply_config(aura_cfg(enabled=True))
    assert r.aura.finds == 1  # straight away, not at the next RECONNECT_EVERY
    for t in (3.0, 4.0, 6.0):
        r.now = t
        r.ctrl.tick()
    assert r.aura.finds == 1
    r.now = 7.0
    r.ctrl.tick()
    assert r.aura.finds == 2


def test_init_failing_on_an_effect_is_closed_and_retried():
    r = Rig(aura_cfg(enabled=True))
    r.aura_t.fail_cmds.add(0x36)  # the first effect colour report fails
    r.ctrl.tick()
    st = r.ctrl.state()["motherboard_rgb"]
    assert not st["connected"] and st["error"].startswith("init failed:") and r.aura_t.closed
    assert [w[1] for w in r.aura_t.writes] == [0x82, 0xB0, 0x52, 0x35]
    r.aura_t = FakeAuraTransport()  # an I/O error isn't the table's fault: it's tried again
    r.now = RECONNECT_EVERY
    r.ctrl.tick()
    assert r.ctrl.state()["motherboard_rgb"]["connected"] and r.aura.opens == 2


def test_liveness_drops_a_vanished_or_reenumerated_controller_and_reapplies():
    r = Rig(aura_cfg(enabled=True))
    r.ctrl.tick()
    first = r.aura_t
    n = len(first.writes)
    r.now = 5.0
    r.ctrl.tick()
    assert len(first.writes) == n and not first.closed  # still there: the check does no device I/O
    r.aura.present = False  # unplugged
    r.now = 10.0
    r.ctrl.tick()
    assert first.closed and not r.ctrl.state()["motherboard_rgb"]["connected"]
    r.aura.present, r.aura.ident = True, 2
    r.aura_t = FakeAuraTransport()
    r.now = 15.0
    r.ctrl.tick()
    assert r.ctrl.state()["motherboard_rgb"]["connected"] and len(effect_writes(r.aura_t.writes[3:])) == 4
    second = r.aura_t
    r.aura.ident = 3  # re-enumerated in place, between two checks
    r.aura_t = FakeAuraTransport()
    r.now = 20.0
    r.ctrl.tick()
    assert second.closed and r.ctrl.state()["motherboard_rgb"]["connected"]
    assert len(effect_writes(r.aura_t.writes[3:])) == 4 and r.aura.opens == 3  # the look went back on


def test_the_drop_reason_stays_until_something_replaces_it():
    r = Rig(aura_cfg(enabled=True))
    r.ctrl.tick()
    assert r.ctrl.state()["motherboard_rgb"]["error"] == ""
    r.aura.present = False  # unplugged
    for t in (5.0, 10.0, 15.0, 20.0):  # the drop, then reconnects that find nothing
        r.now = t
        r.ctrl.tick()
        assert "went away" in r.ctrl.state()["motherboard_rgb"]["error"], t
    assert r.aura.finds >= 4  # it kept looking, and the reason didn't go
    r.aura.present, r.aura.ident = True, 2
    r.aura_t = FakeAuraTransport()
    r.now = 25.0
    r.ctrl.tick()
    st = r.ctrl.state()["motherboard_rgb"]
    assert st["connected"] and st["error"] == ""  # a connect replaces it
    r.aura.present = False
    r.now = 30.0
    r.ctrl.tick()
    assert "went away" in r.ctrl.state()["motherboard_rgb"]["error"]
    r.ctrl.apply_config(aura_cfg(enabled=False))
    assert r.ctrl.state()["motherboard_rgb"]["error"] == ""  # and so does turning it off


class _SysfsAccess(aura.HidrawAccess):
    """The real sysfs and stat checks over a fake tree; opening gives a fake transport."""

    def __init__(self, sysfs, dev):
        super().__init__(sysfs, dev)
        self.opened = []

    def open(self, devnode):
        t = FakeAuraTransport()
        self.opened.append((devnode, t))
        return AuraMainboard(t, clock=lambda: t.clock)


def _plug(tmp_path, name):
    _node(tmp_path / "sys", name, "0003:00000B05:000018F3")
    (tmp_path / "dev").mkdir(exist_ok=True)
    (tmp_path / "dev" / name).write_text("")


def test_node_id_sees_sysfs_and_the_node_only(tmp_path):
    _plug(tmp_path, "hidraw3")
    acc = aura.HidrawAccess(tmp_path / "sys", tmp_path / "dev")
    assert acc.find() == "/dev/hidraw3"
    first = acc.node_id("/dev/hidraw3")
    assert first[0] == "hidraw3"
    fresh = tmp_path / "dev" / "new"
    fresh.write_text("")
    os.replace(fresh, tmp_path / "dev" / "hidraw3")  # recreated: a new inode
    assert acc.node_id("/dev/hidraw3") not in (None, first)
    (tmp_path / "dev" / "hidraw3").unlink()
    assert acc.node_id("/dev/hidraw3") is None
    assert acc.node_id("/dev/hidraw9") is None  # not in sysfs


def test_liveness_over_a_fake_sysfs(tmp_path):
    import shutil

    _plug(tmp_path, "hidraw3")
    acc = _SysfsAccess(tmp_path / "sys", tmp_path / "dev")
    r = Rig(aura_cfg(enabled=True), aura_access=acc)
    r.ctrl.tick()
    assert [d for d, _ in acc.opened] == ["/dev/hidraw3"] and r.ctrl.state()["motherboard_rgb"]["connected"]
    shutil.rmtree(tmp_path / "sys" / "hidraw3")  # unplugged
    _plug(tmp_path, "hidraw5")  # and back, as another node
    r.now = 5.0
    r.ctrl.tick()
    assert acc.opened[0][1].closed and [d for d, _ in acc.opened] == ["/dev/hidraw3", "/dev/hidraw5"]
    assert len(effect_writes(acc.opened[1][1].writes[3:])) == 4  # the look is back on the new node


def test_demo_turns_motherboard_rgb_on_in_memory_only(tmp_path, monkeypatch):
    import argparse

    from louver import daemon
    from louver.sim import SimAuraAccess

    made = []

    class Capture:
        def __init__(self, cfg, cfg_path, sensors, **kw):
            made.append((cfg, cfg_path, kw))

    def no_listen(ctrl, sock):
        raise OSError(98, "in use")

    monkeypatch.setattr(daemon, "Controller", Capture)
    monkeypatch.setattr(daemon, "serve", no_listen)
    cfg = cfgmod.validate({})
    args = argparse.Namespace(demo=True, socket=None)
    assert cli.cmd_daemon(args, cfg, tmp_path / "config.json") == cli.ALREADY_RUNNING
    ((demo_cfg, cfg_path, kw),) = made
    assert demo_cfg["motherboard_rgb"]["enabled"] is True and cfg_path is None
    assert isinstance(kw["aura_access"], SimAuraAccess)
    assert cfg["motherboard_rgb"]["enabled"] is False and not (tmp_path / "config.json").exists()
