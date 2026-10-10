"""RAM RGB on the user side: the louver-ram client, the looks, the daemon's
RAM RGB zone, its config, themes, the demo and `louver ram-probe`. The
helper is faked (FakeRamAccess) or simulated: nothing here reaches
/run/louver-ram.sock or a /dev/i2c-* node."""

import argparse
import json
import logging
import socket
import time

import pytest

from louver import __main__ as cli
from louver import config as cfgmod
from louver import ramrgb
from louver.daemon import KEEPALIVE_EVERY, RAM_LOG_REPEAT, RAM_RESCAN_MAX, RECONNECT_EVERY
from louver.ramhelper import RESCAN_RETRY_EVERY
from louver.ramrgb import RamClient, RamHelperAccess, RamHelperError, parse_listing, render
from louver.themes import THEMES, apply_theme

from .fakes import FakeRamAccess, ram_listing
from .test_control import Rig, _req, server  # noqa: F401  (fixture)

WHITE = (255, 255, 255)


# ---------------- looks ----------------
def test_render_off_static_and_gradient():
    assert render({"mode": "Off", "colors": ["#ff0000"]}, 10) == [(0, 0, 0)] * 10
    assert render({"mode": "Static", "colors": ["#123456", "#ffffff"]}, 12) == [(0x12, 0x34, 0x56)] * 12
    g = render({"mode": "Gradient", "colors": ["#ff0000", "#0000ff"]}, 10)
    assert g[0] == (255, 0, 0) and g[-1] == (0, 0, 255) and len(g) == 10
    assert g[4] == (142, 0, 113)  # 4/9 of the way: rgbfx.gradient's rounding
    three = render({"mode": "Gradient", "colors": ["#ff0000", "#00ff00", "#0000ff"]}, 5)
    assert three == [(255, 0, 0), (128, 128, 0), (0, 255, 0), (0, 128, 128), (0, 0, 255)]
    assert render({"mode": "Gradient", "colors": ["#00ff00"]}, 6) == [(0, 255, 0)] * 6
    assert render({"mode": "Gradient", "colors": ["#ff0000", "#0000ff"]}, 1) == [(255, 0, 0)]
    assert [m["name"] for m in ramrgb.lighting_modes()] == list(ramrgb.MODES)


# ---------------- the client ----------------
def _pair():
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    a.settimeout(2)
    b.settimeout(2)
    return RamClient(a), b


def test_client_sends_one_line_and_reads_one_reply():
    client, peer = _pair()
    peer.sendall(json.dumps({"ok": True, **_listing_reply()}).encode() + b"\n")
    listing = client.list(rescan=True)
    assert peer.recv(1000) == b'{"op":"rescan"}\n'
    assert [s.describe() for s in listing.sticks] == _listing_reply()["sticks"]
    assert listing.skipped == [(0x1A, "in use by a kernel driver")] and listing.error == ""
    peer.sendall(b'{"ok":true}\n')
    client.set(0x19, [(1, 2, 3)] * 2)
    assert json.loads(peer.recv(1000)) == {"op": "set", "addr": 0x19, "colors": [[1, 2, 3], [1, 2, 3]]}
    client.close()


def test_client_raises_on_errors_and_bad_replies():
    client, peer = _pair()
    peer.sendall(b'{"ok":false,"error":"a frame came too soon","code":"too_soon"}\n')
    with pytest.raises(RamHelperError) as e:
        client.set(0x19, [WHITE])
    assert e.value.code == "too_soon" and str(e.value) == "a frame came too soon"
    for bad in (b"[]\n", b'{"ok":"yes"}\n', b"nope\n"):
        peer.sendall(bad)
        with pytest.raises(ValueError):
            client.request({"op": "list"})
    peer.sendall(b"x" * (ramrgb.MAX_REPLY + 2))
    with pytest.raises(ValueError, match="too long"):
        client.request({"op": "list"})
    client, peer = _pair()
    peer.close()
    with pytest.raises(OSError):
        client.request({"op": "list"})


def _listing_reply(**over):
    return {
        "bus": "SMBus I801 adapter at 0000:00:1f.4",
        "device": "/dev/i2c-3",
        "sticks": [
            {
                "addr": 0x19,
                "name": "Corsair Vengeance RGB DDR5",
                "pid": 0x0700,
                "leds": 10,
                "firmware": "1.3.300",
                "protocol": 4,
            }
        ],
        "skipped": [{"addr": 0x1A, "reason": "in use by a kernel driver", "retry": False}],
        "error": "",
        "retry": False,
        **over,
    }


@pytest.mark.parametrize(
    "stick",
    [
        {"leds": 0},
        {"leds": True},
        {"leds": 65},
        {"leds": 10.0},
        {"addr": 0x50},
        {"addr": "25"},
        {"name": None},
        {"protocol": -1},
    ],
)
def test_listing_is_checked_before_it_sizes_a_frame(stick):
    good = _listing_reply()
    bad = {**good, "sticks": [{**good["sticks"][0], **stick}]}
    with pytest.raises(ValueError):
        parse_listing(bad)
    with pytest.raises(ValueError):
        parse_listing({**good, "retry": 1})
    for missing in ("sticks", "bus", "error", "retry"):
        with pytest.raises(ValueError):
            parse_listing({k: v for k, v in good.items() if k != missing})


def test_connect_errors_say_what_to_do(tmp_path):
    acc = RamHelperAccess(str(tmp_path / "nothing.sock"), timeout=1)
    with pytest.raises(FileNotFoundError) as e:
        acc.connect()
    assert "isn't running" in ramrgb.connect_error(acc.path, e.value) and "louver package" in ramrgb.connect_error(
        acc.path, e.value
    )
    assert "plugdev" in ramrgb.connect_error("/run/louver-ram.sock", PermissionError(13, "Permission denied"))
    assert "louver-ram.socket" in ramrgb.connect_error("/x", ConnectionRefusedError(111, "refused"))
    assert ramrgb.SOCKET_PATH == "/run/louver-ram.sock"


# ---------------- config and themes ----------------
def test_ram_rgb_is_off_by_default_and_validated():
    assert cfgmod.validate({})["ram_rgb"] == {"enabled": False, "lighting": {"mode": "Static", "colors": ["#ffffff"]}}
    ok = cfgmod.validate(
        {"ram_rgb": {"enabled": True, "lighting": {"mode": "Gradient", "colors": ["#ff0000", "#0000ff"]}}}
    )
    assert ok["ram_rgb"]["lighting"]["colors"] == ["#ff0000", "#0000ff"]
    # A config from before RAM RGB starts it on the theme in use; one that has the section keeps it.
    old = cfgmod.validate({"theme": "Ocean", "motherboard_rgb": {}})["ram_rgb"]
    assert old["lighting"] == THEMES["Ocean"]["ram"] and old["enabled"] is False
    assert cfgmod.validate({"theme": "Ocean", "ram_rgb": {}})["ram_rgb"]["lighting"]["mode"] == "Static"
    assert cfgmod.validate({"theme": ["Ocean"]})["ram_rgb"]["lighting"]["mode"] == "Static"
    for bad in (
        {"enabled": "yes"},
        {"enabled": 1},
        {"lighting": {"mode": "Rainbow"}},  # not a RAM look
        {"lighting": {"mode": "Static", "colors": []}},
        {"lighting": {"mode": "Gradient", "colors": ["#ff0000"] * 7}},
        {"lighting": {"mode": "Static", "colors": ["#zzzzzz"]}},
        {"lighting": {"mode": "Static", "colors": "#ffffff"}},
        {"lighting": {"mode": "Static", "speed": 2}},
        {"lighting": "Static"},
        {"per_stick": True},
    ):
        with pytest.raises(cfgmod.ConfigError):
            cfgmod.validate({"ram_rgb": bad})
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.validate({"ram_rgb": "on"})


def test_every_theme_has_a_static_ram_look():
    for name, theme in THEMES.items():
        look = cfgmod.validate({"ram_rgb": {"lighting": theme["ram"]}})["ram_rgb"]["lighting"]
        assert look["mode"] in ("Off", "Static", "Gradient"), name
        if look["mode"] == "Gradient":
            assert look["colors"] == theme["swatch"], name
    assert THEMES["Stealth"]["ram"]["mode"] == "Off"
    cfg = cfgmod.validate(apply_theme(cfgmod.validate({}), "Cyberpunk"))
    assert cfg["ram_rgb"] == {"enabled": False, "lighting": {"mode": "Gradient", "colors": ["#ff00c8", "#00f0ff"]}}


# ---------------- daemon ----------------
def ram_cfg(**r):
    cfg = cfgmod.validate({})
    cfg["ram_rgb"].update(r)
    return cfgmod.validate(cfg)


def test_disabled_never_contacts_the_helper():
    r = Rig()
    for t in range(0, 61, 1):
        r.now = float(t)
        r.ctrl.tick()
    new = json.loads(json.dumps(r.ctrl.cfg))
    new["ram_rgb"]["lighting"]["mode"] = "Off"
    r.ctrl.apply_config(new)
    r.ctrl.update_config(lambda cfg: apply_theme(cfg, "Ocean"))
    r.ctrl.shutdown()
    assert r.ram.connects == 0
    assert r.ctrl.state()["ram_rgb"] == {"enabled": False, "connected": False, "sticks": [], "error": ""}


def test_turning_it_on_connects_at_once_with_a_rescan_and_lights_every_stick():
    r = Rig()
    r.now = 2.0
    r.ctrl.tick()
    r.ctrl.apply_config(ram_cfg(enabled=True, lighting={"mode": "Static", "colors": ["#ff0000"]}))
    (client,) = r.ram.clients
    assert r.ram.connects == 1 and client.lists == [True]  # straight away, asking for a fresh scan
    assert client.sets == [(0x19, [(255, 0, 0)] * 10), (0x1B, [(255, 0, 0)] * 10)]
    st = r.ctrl.state()["ram_rgb"]
    assert st["connected"] and st["error"] == "" and [s["addr"] for s in st["sticks"]] == [0x19, 0x1B]
    assert st["sticks"][0] == {
        "addr": 0x19,
        "name": "Corsair Vengeance RGB DDR5",
        "pid": 0x0700,
        "leds": 10,
        "firmware": "1.3.300",
        "protocol": 4,
    }


def test_enabled_at_start_connects_on_the_first_tick_without_a_rescan():
    r = Rig(ram_cfg(enabled=True))
    r.ctrl.tick()
    assert r.ram.connects == 1 and r.ram.clients[0].lists == [False]
    assert len(r.ram.sets()) == 2


def test_frame_resent_on_change_and_every_keepalive_only():
    r = Rig(ram_cfg(enabled=True))
    r.ctrl.tick()
    client = r.ram.clients[0]
    for t in range(1, int(KEEPALIVE_EVERY)):
        r.now = float(t)
        r.ctrl.tick()
    assert len(client.sets) == 2  # nothing new
    r.now = KEEPALIVE_EVERY
    r.ctrl.tick()
    assert len(client.sets) == 4  # the keepalive, both sticks
    new = json.loads(json.dumps(r.ctrl.cfg))
    new["ram_rgb"]["lighting"] = {"mode": "Gradient", "colors": ["#ff0000", "#0000ff"]}
    r.now = KEEPALIVE_EVERY + 0.5
    r.ctrl.apply_config(new)  # applies, then ticks: one frame per stick, not two
    assert len(client.sets) == 6 and client.sets[-1][1][0] == (255, 0, 0) and client.sets[-1][1][-1] == (0, 0, 255)
    new["min_percent"] = 10  # a save that doesn't touch RAM RGB
    r.ctrl.apply_config(new)
    assert len(client.sets) == 6 and r.ram.connects == 1


def test_connect_failures_are_paced_and_logged_once(caplog):
    r = Rig(ram_cfg(enabled=True))
    r.ram.connect_error = FileNotFoundError(2, "No such file or directory")
    with caplog.at_level(logging.WARNING, logger="louver"):
        for t in range(21):
            r.now = float(t)
            r.ctrl.tick()
            new = json.loads(json.dumps(r.ctrl.cfg))
            new["min_percent"] = t % 7
            r.ctrl.apply_config(new)
    assert r.ram.connects == 5  # t = 0, 5, 10, 15, 20
    err = r.ctrl.state()["ram_rgb"]["error"]
    assert "isn't running" in err and r.ram.path in err
    assert sum("RAM RGB:" in rec.getMessage() for rec in caplog.records) == 1
    r.ram.connect_error = None
    r.now = 25.0
    r.ctrl.tick()
    assert r.ctrl.state()["ram_rgb"]["connected"] and r.ctrl.state()["ram_rgb"]["error"] == ""


def test_an_error_drops_the_connection_then_reconnects_at_the_usual_pace():
    r = Rig(ram_cfg(enabled=True))
    r.ctrl.tick()
    first = r.ram.clients[0]
    r.ram.set_errors = [RamHelperError("write to 0x19 failed: Input/output error", "io")]
    r.now = KEEPALIVE_EVERY
    r.ctrl.tick()
    st = r.ctrl.state()["ram_rgb"]
    assert first.closed and not st["connected"] and st["sticks"] == []
    assert st["error"] == "disconnected: write to 0x19 failed: Input/output error"
    r.now = KEEPALIVE_EVERY + 1
    r.ctrl.tick()
    assert r.ram.connects == 2 and r.ctrl.state()["ram_rgb"]["connected"]  # the last try was at t=0
    second = r.ram.clients[1]
    assert len(second.sets) == 2  # the look went back on
    r.ram.set_errors = [BrokenPipeError(32, "Broken pipe")]
    new = json.loads(json.dumps(r.ctrl.cfg))
    new["ram_rgb"]["lighting"]["mode"] = "Off"
    r.now = KEEPALIVE_EVERY + 2
    r.ctrl.apply_config(new)
    assert second.closed and r.ram.connects == 2  # dropped; the next try waits for RECONNECT_EVERY
    r.now = KEEPALIVE_EVERY + 1 + RECONNECT_EVERY
    r.ctrl.tick()
    assert r.ram.connects == 3 and r.ram.clients[2].sets[0][1] == [(0, 0, 0)] * 10


def test_a_frame_too_soon_is_resent_on_the_next_tick_without_a_reconnect():
    r = Rig(ram_cfg(enabled=True))
    r.ctrl.tick()
    client = r.ram.clients[0]
    r.ram.set_errors = [RamHelperError("too soon", "too_soon")]
    new = json.loads(json.dumps(r.ctrl.cfg))
    new["ram_rgb"]["lighting"]["colors"] = ["#00ff00"]
    r.ctrl.apply_config(new)
    assert not client.closed and r.ctrl.state()["ram_rgb"]["connected"]
    r.now = 1.0
    r.ctrl.tick()
    assert client.sets[-2:] == [(0x19, [(0, 255, 0)] * 10), (0x1B, [(0, 255, 0)] * 10)]
    assert r.ram.connects == 1


def test_a_helper_that_found_nothing_is_not_asked_again_until_re_enabled():
    r = Rig(ram_cfg(enabled=True))
    r.ram.listing = ram_listing(
        none=True, error="no Corsair DDR5 RGB stick found", skipped=[(0x1A, "in use by a kernel driver")]
    )
    r.ctrl.tick()
    st = r.ctrl.state()["ram_rgb"]
    assert not st["connected"] and r.ram.clients[0].closed  # closed, so the helper can exit
    assert st["error"] == (
        "no Corsair DDR5 RGB stick found (0x1a: in use by a kernel driver); "
        "not asked again until it's turned off and on"
    )
    for t in range(1, 61):
        r.now = float(t)
        r.ctrl.tick()
    assert r.ram.connects == 1
    r.ctrl.apply_config(ram_cfg(enabled=False))
    assert r.ctrl.state()["ram_rgb"]["error"] == ""
    r.ram.listing = ram_listing()
    r.ctrl.apply_config(ram_cfg(enabled=True))
    assert r.ram.connects == 2 and r.ctrl.state()["ram_rgb"]["connected"]


def test_nothing_found_but_io_trouble_is_retried_with_backoff():
    r = Rig(ram_cfg(enabled=True))
    trouble = ram_listing(
        none=True, error="no Corsair DDR5 RGB stick found", skipped=[(0x19, "the bus was busy")], retry=True
    )
    r.ram.listing = trouble
    tries = []
    for t in range(0, 700):
        r.now = float(t)
        n = r.ram.connects
        r.ctrl.tick()
        if r.ram.connects > n:
            tries.append((t, r.ram.clients[-1].lists[-1]))
    # Asked again after 5, 10, 20, 40, 80, 160, then every 300 s: always for a fresh scan.
    assert tries == [(0, False), (5, True), (15, True), (35, True), (75, True), (155, True), (315, True), (615, True)]
    assert all(c.closed for c in r.ram.clients) and not r.ctrl.state()["ram_rgb"]["connected"]
    assert r.ctrl.state()["ram_rgb"]["error"] == (
        "no Corsair DDR5 RGB stick found (0x19: the bus was busy); scanning again in 300 s"
    )
    r.ram.listing = ram_listing()
    r.now = 915.0
    r.ctrl.tick()
    assert r.ctrl.state()["ram_rgb"]["connected"] and r.ctrl.state()["ram_rgb"]["error"] == ""
    assert RAM_RESCAN_MAX == 300.0


def test_io_trouble_while_connected_is_rescanned_until_it_clears():
    r = Rig(ram_cfg(enabled=True))
    r.ram.listing = ram_listing((0x19, 10), skipped=[(0x1B, "I/O error: Input/output error")], retry=True)
    r.ctrl.tick()
    (client,) = r.ram.clients
    assert client.lists == [False] and [a for a, _ in client.sets] == [0x19]
    for t in range(1, 16):
        r.now = float(t)
        r.ctrl.tick()
    assert client.lists == [False, True, True]  # at 5 s, then 10 s later
    r.ram.listing = ram_listing()  # the second stick answers now
    for t in range(16, 100):
        r.now = float(t)
        r.ctrl.tick()
    assert client.lists == [False, True, True, True] and r.ram.connects == 1  # at 35 s, then no more
    assert [a for a, _ in client.sets[-2:]] == [0x19, 0x1B]  # its frame went out at once
    assert [s["addr"] for s in r.ctrl.state()["ram_rgb"]["sticks"]] == [0x19, 0x1B]


def test_turning_it_off_and_on_finds_a_stick_the_helper_missed():
    """The reviewer's stale-toggle case, through the real helper (simulated bus)."""
    from louver.sim import SimRamAccess

    r = Rig(ram_cfg(enabled=False))
    acc = SimRamAccess(clock=lambda: r.now)
    r.ctrl.ram_access = acc
    sticks = dict(acc.bus.sticks)
    acc.bus.sticks.clear()  # nothing answers yet
    r.ctrl.apply_config(ram_cfg(enabled=True))
    assert "not asked again until it's turned off and on" in r.ctrl.state()["ram_rgb"]["error"]
    acc.bus.sticks.update(sticks)
    r.now = 20.0  # well within the helper's 30 s rescan limit
    r.ctrl.apply_config(ram_cfg(enabled=False))
    r.ctrl.apply_config(ram_cfg(enabled=True))
    st = r.ctrl.state()["ram_rgb"]
    assert st["connected"] and [s["addr"] for s in st["sticks"]] == [0x19, 0x1B]
    assert RESCAN_RETRY_EVERY < 20.0


def test_repeated_drops_are_logged_once(caplog):
    r = Rig(ram_cfg(enabled=True))
    with caplog.at_level(logging.WARNING, logger="louver"):
        for t in range(0, 60, 5):
            r.now = float(t)
            r.ram.set_errors = [RamHelperError("write to 0x19 failed: Input/output error", "io")]
            r.ctrl.tick()  # connects, the first frame fails, dropped
    assert r.ram.connects == 12
    drops = [rec.getMessage() for rec in caplog.records if "disconnected" in rec.getMessage()]
    assert drops == ["RAM RGB: disconnected, will reconnect: write to 0x19 failed: Input/output error"]
    r.now = 60.0 + RAM_LOG_REPEAT
    r.ram.set_errors = [RamHelperError("write to 0x19 failed: Input/output error", "io")]
    with caplog.at_level(logging.WARNING, logger="louver"):
        r.ctrl.tick()
    assert sum("disconnected" in rec.getMessage() for rec in caplog.records) == 2  # again, much later


def test_a_stuck_helper_holds_the_loop_up_briefly(tmp_path):
    """A helper socket that accepts connections but never answers (the
    reviewer's lock repro): one tick waits LIST_TIMEOUT, not seconds."""
    path = str(tmp_path / "m.sock")
    mute = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    mute.bind(path)
    mute.listen(16)
    try:
        r = Rig(ram_cfg(enabled=True), ram_access=RamHelperAccess(path))
        t0 = time.monotonic()
        r.ctrl.tick()
        took = time.monotonic() - t0
        assert ramrgb.LIST_TIMEOUT <= took < ramrgb.LIST_TIMEOUT + 0.5
        assert r.ctrl.state()["ram_rgb"]["error"] == "the RAM helper didn't answer: timed out"
    finally:
        mute.close()
    assert (ramrgb.CONNECT_TIMEOUT, ramrgb.LIST_TIMEOUT, ramrgb.SET_TIMEOUT) == (0.5, 0.5, 0.3)


def test_client_timeout_is_for_the_whole_reply():
    client, peer = _pair()
    peer.sendall(b'{"ok":')  # half a reply, then nothing
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        client.request({"op": "list"}, timeout=0.2)
    assert time.monotonic() - t0 < 0.6


@pytest.mark.parametrize("poll", [1.0, 9.9, 30.0])  # 30 s: the slowest poll_interval config allows
def test_a_connected_daemon_talks_to_the_helper_well_within_client_idle(poll):
    """The helper drops a client silent for CLIENT_IDLE. A connected daemon
    sends a frame at least every KEEPALIVE_EVERY, on its next tick, also while
    frames come back too_soon and while it's backing off a rescan."""
    from louver.ramhelper import CLIENT_IDLE

    cfgmod.validate({"poll_interval": 30})
    with pytest.raises(cfgmod.ConfigError):
        cfgmod.validate({"poll_interval": 30.5})
    r = Rig(ram_cfg(enabled=True))
    r.ram.listing = ram_listing((0x19, 10), skipped=[(0x1B, "the bus was busy")], retry=True)
    asked = []
    r.ctrl.tick()
    (client,) = r.ram.clients
    real_set, real_list = client.set, client.list

    def set_(addr, colors):
        asked.append(r.now)
        real_set(addr, colors)

    def list_(rescan=False, timeout=None):
        asked.append(r.now)
        return real_list(rescan)

    client.set, client.list = set_, list_
    t, n = 0.0, 0
    while t < 900:
        t += poll
        n += 1
        r.now = t
        if n % 3 == 0:
            r.ram.set_errors = [RamHelperError("too many frames for 0x19: at most 1 a second", "too_soon")]
        r.ctrl.tick()
    assert r.ram.connects == 1 and client.lists[1:] and all(client.lists[1:])  # still connected, rescans on backoff
    gaps = [b - a for a, b in zip([0.0, *asked], asked)]
    assert max(gaps) <= KEEPALIVE_EVERY + poll + 1e-6
    assert KEEPALIVE_EVERY + 30.0 < CLIENT_IDLE  # with room for a slow tick


def test_a_helper_that_doesnt_answer_is_retried():
    r = Rig(ram_cfg(enabled=True))
    r.ram.list_error = TimeoutError("timed out")
    r.ctrl.tick()
    assert r.ram.clients[0].closed and "didn't answer: timed out" in r.ctrl.state()["ram_rgb"]["error"]
    r.ram.list_error = None
    r.now = RECONNECT_EVERY
    r.ctrl.tick()
    assert r.ctrl.state()["ram_rgb"]["connected"]


def test_turning_it_off_closes_and_sends_nothing_more():
    r = Rig(ram_cfg(enabled=True))
    r.ctrl.tick()
    client = r.ram.clients[0]
    n = len(client.sets)
    r.ctrl.apply_config(ram_cfg(enabled=False))
    assert client.closed and r.ctrl.state()["ram_rgb"] == {
        "enabled": False,
        "connected": False,
        "sticks": [],
        "error": "",
    }
    for t in range(1, 40):
        r.now = float(t)
        r.ctrl.tick()
    assert len(client.sets) == n and r.ram.connects == 1  # no black frame on the way out, no reconnects


def test_shutdown_closes_the_connection_without_a_frame():
    r = Rig(ram_cfg(enabled=True))
    r.ctrl.tick()
    n = len(r.ram.sets())
    r.ctrl.shutdown()
    assert r.ram.clients[0].closed and len(r.ram.sets()) == n


def test_theme_sets_the_ram_look_and_sends_it():
    r = Rig(ram_cfg(enabled=True))
    r.ctrl.tick()
    r.ctrl.update_config(lambda cfg: apply_theme(cfg, "Sunset"))
    assert r.ctrl.cfg["ram_rgb"]["lighting"] == THEMES["Sunset"]["ram"]
    frame = r.ram.sets()[-1][1]
    assert frame[0] == (0xFF, 0x1E, 0x00) and frame[-1] == (0x8A, 0x00, 0xFF) and len(frame) == 10
    r.ctrl.update_config(lambda cfg: apply_theme(cfg, "Stealth"))
    assert r.ram.sets()[-1][1] == [(0, 0, 0)] * 10


def test_sticks_with_different_led_counts_each_get_their_own_frame():
    r = Rig(ram_cfg(enabled=True, lighting={"mode": "Gradient", "colors": ["#000000", "#ffffff"]}))
    r.ram.listing = ram_listing((0x19, 10), (0x1B, 12))
    r.ctrl.tick()
    (a, fa), (b, fb) = r.ram.sets()
    assert (a, len(fa), b, len(fb)) == (0x19, 10, 0x1B, 12) and fa[-1] == fb[-1] == WHITE


def test_http_state_and_meta_carry_ram_rgb(server):  # noqa: F811
    r, port = server
    code, st = _req(port, "/api/state")
    assert code == 200 and st["ram_rgb"] == {"enabled": False, "connected": False, "sticks": [], "error": ""}
    code, m = _req(port, "/api/meta")
    assert m["ram_lighting_modes"] == [
        {"name": "Off", "max_colors": 0},
        {"name": "Static", "max_colors": 1},
        {"name": "Gradient", "max_colors": 6},
    ]
    cfg = json.loads(json.dumps(r.ctrl.cfg))
    cfg["ram_rgb"]["enabled"] = True
    code, new = _req(port, "/api/config", body=cfg, headers={"X-Louver": "1"})
    assert code == 200 and new["ram_rgb"]["enabled"]
    assert _req(port, "/api/state")[1]["ram_rgb"]["connected"]
    cfg["ram_rgb"]["lighting"] = {"mode": "Rainbow", "colors": ["#ffffff"]}
    code, err = _req(port, "/api/config", body=cfg, headers={"X-Louver": "1"})
    assert code == 400 and "ram_rgb" in err["error"]


# ---------------- demo ----------------
def test_demo_turns_ram_rgb_on_in_memory_with_the_simulated_helper(tmp_path, monkeypatch):
    from louver import daemon
    from louver.sim import SimRamAccess

    made = []

    class Capture:
        def __init__(self, cfg, cfg_path, sensors, **kw):
            made.append((cfg, cfg_path, kw))

    def no_listen(ctrl, listen):
        raise OSError(98, "in use")

    monkeypatch.setattr(daemon, "Controller", Capture)
    monkeypatch.setattr(daemon, "serve", no_listen)
    cfg = cfgmod.validate({})
    args = argparse.Namespace(demo=True, listen="127.0.0.1:1")
    assert cli.cmd_daemon(args, cfg, tmp_path / "config.json") == cli.ALREADY_RUNNING
    ((demo_cfg, cfg_path, kw),) = made
    assert demo_cfg["ram_rgb"]["enabled"] is True and cfg_path is None
    assert isinstance(kw["ram_access"], SimRamAccess)
    assert cfg["ram_rgb"]["enabled"] is False and not (tmp_path / "config.json").exists()


def test_demo_ram_runs_the_real_helper_over_two_simulated_sticks():
    from louver.sim import SimRamAccess

    acc = SimRamAccess()
    r = Rig(ram_cfg(enabled=True, lighting={"mode": "Gradient", "colors": ["#ff0000", "#0000ff"]}), ram_access=acc)
    r.ctrl.tick()
    st = r.ctrl.state()["ram_rgb"]
    assert st["connected"] and [(s["addr"], s["leds"]) for s in st["sticks"]] == [(0x19, 10), (0x1B, 10)]
    assert acc.bus.sticks[0x19].colors()[0] == (255, 0, 0) and acc.bus.sticks[0x1B].colors()[-1] == (0, 0, 255)


# ---------------- louver ram-probe ----------------
def test_ram_probe_prints_what_the_helper_found(capsys):
    acc = FakeRamAccess()
    acc.listing = ram_listing(skipped=[(0x1A, "in use by a kernel driver")])
    assert cli.cmd_ram_probe(None, cfgmod.validate({}), None, access=acc) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "SMBus I801 adapter at 0000:00:1f.4  /dev/i2c-3"
    assert out[1] == "  0x19  Corsair Vengeance RGB DDR5  pid 0x0700  10 LEDs  firmware 1.3.300  protocol 4"
    assert out[3] == "  0x1a  skipped: in use by a kernel driver"
    (client,) = acc.clients
    assert client.lists == [False] and client.sets == [] and client.closed  # a list, nothing else


def test_ram_probe_failures(capsys):
    acc = FakeRamAccess()
    acc.connect_error = PermissionError(13, "Permission denied")
    assert cli.cmd_ram_probe(None, {}, None, access=acc) == 1
    assert "plugdev" in capsys.readouterr().err
    acc = FakeRamAccess()
    acc.list_error = RamHelperError("bad", "bad_request")
    assert cli.cmd_ram_probe(None, {}, None, access=acc) == 1 and acc.clients[0].closed
    assert "didn't answer: bad" in capsys.readouterr().err
    acc = FakeRamAccess()
    acc.listing = ramrgb.Listing("", "", [], [], "no Intel SMBus adapter (i2c-i801) found", False)
    assert cli.cmd_ram_probe(None, {}, None, access=acc) == 1
    assert "no Intel SMBus adapter" in capsys.readouterr().err


def test_ram_probe_waits_for_a_slow_first_scan(capsys):
    """A cold helper start plus a slow scan is what ram-probe is for: it
    waits PROBE_TIMEOUT, where the daemon (under its lock) gives up sooner."""
    import threading

    acc = FakeRamAccess()
    assert cli.cmd_ram_probe(None, {}, None, access=acc) == 0
    assert acc.clients[0].list_timeouts == [ramrgb.PROBE_TIMEOUT]
    assert ramrgb.PROBE_TIMEOUT >= 5 * ramrgb.LIST_TIMEOUT

    delay = ramrgb.LIST_TIMEOUT + 0.5

    class SlowHelper:
        path = "/nonexistent"

        def __init__(self):
            self.peers, self.threads = [], []

        def connect(self):
            client, peer = _pair()
            self.peers.append(peer)

            def answer():
                peer.recv(4096)
                time.sleep(delay)
                try:
                    peer.sendall(json.dumps({"ok": True, **_listing_reply()}).encode() + b"\n")
                except OSError:  # the client gave up and went
                    pass

            t = threading.Thread(target=answer, daemon=True)
            t.start()
            self.threads.append(t)
            return client

    slow = SlowHelper()
    daemon_side = slow.connect()
    with pytest.raises(TimeoutError):
        daemon_side.list()  # the daemon's bound
    capsys.readouterr()
    assert cli.cmd_ram_probe(None, {}, None, access=slow) == 0
    assert capsys.readouterr().out.startswith("SMBus I801 adapter")
    daemon_side.close()
    for t in slow.threads:
        t.join(5)
    for peer in slow.peers:
        peer.close()


def test_default_probe_access_waits_long_too(monkeypatch):
    made = []
    monkeypatch.setattr(ramrgb, "RamHelperAccess", lambda **kw: made.append(kw) or FakeRamAccess())
    assert cli.cmd_ram_probe(None, {}, None) == 0
    assert made == [{"timeout": ramrgb.PROBE_TIMEOUT}]


def test_ram_probe_is_a_subcommand(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_ram_probe", lambda *a: seen.append(a) or 0)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent")
    assert cli.main(["ram-probe"]) == 0 and len(seen) == 1
