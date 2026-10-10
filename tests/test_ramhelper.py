"""The louver-ram root helper: its line protocol, pacing, rescans, idle exit
and socket activation. The bus is always simulated (SimSMBus behind the real
SafeBus); nothing here opens /dev/i2c-* or starts the helper as a process."""

import errno
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from louver import corsair_dram as cd
from louver import ramhelper
from louver.ramhelper import FRAME_BURST, MAX_LINE, RESCAN_EVERY, RESCAN_RETRY_EVERY, Helper, Server
from louver.ramrgb import RamHelperAccess, RamHelperError
from louver.sim import SimCorsairStick, SimSMBus

ROOT = Path(__file__).resolve().parent.parent
WHITE10 = [[255, 255, 255]] * 10


class TroubleSMBus(SimSMBus):
    """Simulated sticks plus trouble: ``held`` addresses a kernel driver owns
    (what I2CDev raises when I2C_SLAVE gets EBUSY), ``busy`` ones whose
    transfers get EBUSY, ``eio`` ones that fail mid-identification (on the
    device-info reads), ``remote`` ones that NACK with EREMOTEIO."""

    def __init__(self, sticks, held=(), busy=(), eio=(), remote=()):
        super().__init__(sticks)
        self.held, self.busy, self.eio, self.remote = set(held), set(busy), set(eio), set(remote)

    def read_byte_data(self, addr, reg):
        if addr in self.held:
            raise cd.AddressInUse(errno.EBUSY, f"{addr:#04x} is in use by a kernel driver")
        if addr in self.busy:
            raise OSError(errno.EBUSY, "Device or resource busy")
        if addr in self.remote:
            raise OSError(errno.EREMOTEIO, "Remote I/O error")
        if addr in self.eio and reg == cd.REG_GET_BINARY_DATA:
            raise OSError(errno.EIO, "Input/output error")
        return super().read_byte_data(addr, reg)


class Rig:
    def __init__(self, sticks=None, no_bus=None, **trouble):
        self.now = 0.0
        self.opens = 0
        self.buses = []
        self.sticks = sticks if sticks is not None else {0x19: SimCorsairStick(), 0x1B: SimCorsairStick()}
        self.trouble = trouble
        self.no_bus = no_bus
        self.helper = Helper(open_bus=self.open_bus, clock=lambda: self.now, sleep=lambda s: None)

    def open_bus(self):
        self.opens += 1
        if self.no_bus is not None:
            raise self.no_bus
        bus = TroubleSMBus(self.sticks, **self.trouble)
        self.buses.append(bus)
        return "SMBus I801 adapter at 0000:00:1f.4", "/dev/i2c-3", cd.SafeBus(bus)

    def ask(self, req) -> dict:
        line = req if isinstance(req, bytes) else json.dumps(req).encode()
        return self.helper.handle(line)


# ---------------- requests ----------------
def test_list_scans_once_on_first_need_and_describes_the_bus():
    r = Rig()
    assert r.opens == 0  # nothing at construction
    got = r.ask({"op": "list"})
    assert got == {
        "ok": True,
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
            },
            {
                "addr": 0x1B,
                "name": "Corsair Vengeance RGB DDR5",
                "pid": 0x0700,
                "leds": 10,
                "firmware": "1.3.300",
                "protocol": 4,
            },
        ],
        "skipped": [],
        "error": "",
        "retry": False,
    }
    r.now = 100.0
    assert r.ask({"op": "list"}) == got and r.opens == 1  # cached: list never rescans


def test_list_explains_skipped_addresses_and_an_empty_bus():
    unknown = SimCorsairStick(pid=0x0123)
    old = SimCorsairStick(protocol=3)
    r = Rig(sticks={0x18: unknown, 0x1C: old}, held={0x1A}, remote={0x1D})
    got = r.ask({"op": "list"})
    assert got["sticks"] == [] and got["error"] == "no Corsair DDR5 RGB stick found"
    assert [(s["addr"], s["reason"], s["retry"]) for s in got["skipped"]] == [
        (0x18, "unknown Corsair DDR5 product 0x0123: its LED count isn't known", False),
        (0x1A, "in use by a kernel driver", False),
        (0x1C, "protocol 3 has no direct mode (needs 4 or later)", False),
    ]  # 0x1D NACKed with EREMOTEIO: nothing there, not listed
    assert got["retry"] is False  # answers, not trouble: scanning again won't change them
    assert r.ask({"op": "set", "addr": 0x18, "colors": WHITE10})["code"] == "unknown_addr"
    assert unknown.blocks == [] and old.blocks == []


def test_io_trouble_is_listed_as_worth_retrying():
    sticks = {0x19: SimCorsairStick(), 0x1A: SimCorsairStick(), 0x1B: SimCorsairStick()}
    r = Rig(sticks=sticks, busy={0x1A}, eio={0x1B})
    got = r.ask({"op": "list"})
    assert [s["addr"] for s in got["sticks"]] == [0x19]
    assert [(s["addr"], s["reason"], s["retry"]) for s in got["skipped"]] == [
        (0x1A, "the bus was busy", True),
        (0x1B, "I/O error: Input/output error", True),  # failed mid-identification
    ]
    assert got["retry"] is True and got["error"] == ""
    assert r.ask({"op": "set", "addr": 0x1B, "colors": WHITE10})["code"] == "unknown_addr"
    assert sticks[0x1B].blocks == []  # it probed fine but never identified: never written


def test_no_bus_is_a_listing_with_the_reason():
    r = Rig(no_bus=cd.NoBus("no Intel SMBus adapter (i2c-i801) found"))
    got = r.ask({"op": "list"})
    assert got["ok"] and got["sticks"] == [] and got["error"] == "no Intel SMBus adapter (i2c-i801) found"
    assert r.ask({"op": "set", "addr": 0x19, "colors": WHITE10})["code"] == "unknown_addr"
    assert r.ask({"op": "list"})["retry"] is False
    r = Rig(no_bus=PermissionError(13, "Permission denied"))
    got = r.ask({"op": "list"})
    assert got["error"].startswith("cannot open the bus:") and got["retry"] is True


def test_set_writes_one_frame_to_that_stick():
    r = Rig()
    colors = [[i, 0, 255 - i] for i in range(10)]
    assert r.ask({"op": "set", "addr": 0x1B, "colors": colors}) == {"ok": True}  # scans first if needed
    assert r.sticks[0x1B].colors() == [tuple(c) for c in colors] and r.sticks[0x19].blocks == []
    assert [reg for reg, _ in r.sticks[0x1B].blocks] == [0x31]


@pytest.mark.parametrize(
    "req",
    [
        b"{not json",
        b"\xff\xfe",
        b"[]",
        b'"list"',
        b'{"op": NaN}',
        b"[" * 3000 + b"]" * 3000,
        {"op": "flash"},
        {"op": 1},
        {"op": ["list"]},
        {},
        {"op": "list", "extra": 1},
        {"op": "set", "addr": 0x19},
        {"op": "set", "addr": True, "colors": WHITE10},
        {"op": "set", "addr": 25.0, "colors": WHITE10},
        {"op": "set", "addr": "0x19", "colors": WHITE10},
        {"op": "set", "addr": 128, "colors": WHITE10},
        {"op": "set", "addr": -1, "colors": WHITE10},
        {"op": "set", "addr": 2**70, "colors": WHITE10},
        {"op": "set", "addr": 0x19, "colors": [[256, 0, 0]] + WHITE10[1:]},
        {"op": "set", "addr": 0x19, "colors": [[-1, 0, 0]] + WHITE10[1:]},
        {"op": "set", "addr": 0x19, "colors": [[True, 0, 0]] + WHITE10[1:]},
        {"op": "set", "addr": 0x19, "colors": [[1.0, 0, 0]] + WHITE10[1:]},
        {"op": "set", "addr": 0x19, "colors": [[1, 2]] + WHITE10[1:]},
        {"op": "set", "addr": 0x19, "colors": [[1, 2, 3, 4]] + WHITE10[1:]},
        {"op": "set", "addr": 0x19, "colors": "white"},
        {"op": "set", "addr": 0x19, "colors": WHITE10[:9]},  # the stick has 10 LEDs
        {"op": "set", "addr": 0x19, "colors": WHITE10 + [[0, 0, 0]]},
    ],
)
def test_bad_requests_write_nothing(req):
    r = Rig()
    got = r.ask(req)
    assert got["ok"] is False and got["code"] == "bad_request", got
    assert all(st.blocks == [] for st in r.sticks.values())


def test_unknown_address():
    r = Rig()
    assert r.ask({"op": "set", "addr": 0x1A, "colors": WHITE10})["code"] == "unknown_addr"
    assert r.ask({"op": "set", "addr": 0x50, "colors": WHITE10})["code"] == "unknown_addr"


def test_frames_are_a_burst_of_four_then_one_a_second_per_stick():
    r = Rig()
    frame = {"op": "set", "addr": 0x19, "colors": WHITE10}
    assert [r.ask(frame)["ok"] for _ in range(FRAME_BURST)] == [True] * 4
    got = r.ask(frame)
    assert got["code"] == "too_soon" and len(r.sticks[0x19].blocks) == 4  # refused, not queued
    assert r.ask({**frame, "addr": 0x1B})["ok"]  # the other stick has its own bucket
    r.now = 0.5
    assert r.ask(frame)["code"] == "too_soon"
    r.now = 1.0
    assert r.ask(frame)["ok"] and r.ask(frame)["code"] == "too_soon"  # one token a second
    sent = []
    for t in range(2, 62):  # sustained: 60 frames in 60 s, asked for 10 times a second
        for k in range(10):
            r.now = t + k / 10
            sent.append(r.ask(frame)["ok"])
    assert sum(sent) == 60
    r.now = 1000.0
    assert [r.ask(frame)["ok"] for _ in range(5)] == [True] * 4 + [False]  # the burst refills, no further


def test_rescan_is_rate_limited():
    r = Rig()
    r.ask({"op": "rescan"})
    assert r.opens == 1
    r.now = RESCAN_RETRY_EVERY
    r.ask({"op": "rescan"})
    assert r.opens == 1  # a scan that found sticks and no trouble: the long limit
    r.sticks[0x1D] = SimCorsairStick()
    r.now = RESCAN_EVERY - 1
    assert len(r.ask({"op": "rescan"})["sticks"]) == 2 and r.opens == 1  # the cached result
    r.now = RESCAN_EVERY
    got = r.ask({"op": "rescan"})
    assert [s["addr"] for s in got["sticks"]] == [0x19, 0x1B, 0x1D] and r.opens == 2
    assert r.buses[0].closed and not r.buses[1].closed  # the old bus is closed, the new one used


def test_a_scan_that_found_nothing_or_hit_trouble_can_be_redone_sooner():
    # The reviewer's case: nothing found, the user fixes it and turns RAM RGB off and on 20 s later.
    r = Rig(sticks={})
    assert r.ask({"op": "rescan"})["sticks"] == []
    r.sticks[0x19] = SimCorsairStick()
    r.now = RESCAN_RETRY_EVERY - 0.1
    assert r.ask({"op": "rescan"})["sticks"] == [] and r.opens == 1  # still limited...
    r.now = 20.0
    assert [s["addr"] for s in r.ask({"op": "rescan"})["sticks"]] == [0x19] and r.opens == 2  # ...but not for 30 s
    r.now = 30.0
    r.ask({"op": "rescan"})
    assert r.opens == 2  # found it, nothing failed: back to the long limit
    # A busy bus at one address: retried on the short limit even though a stick was found.
    r = Rig(busy={0x1B})
    assert r.ask({"op": "list"})["retry"] is True
    r.trouble = {}
    r.now = RESCAN_RETRY_EVERY
    got = r.ask({"op": "rescan"})
    assert [s["addr"] for s in got["sticks"]] == [0x19, 0x1B] and got["retry"] is False and r.opens == 2
    r.now = RESCAN_RETRY_EVERY + 1
    for _ in range(50):  # a client can't force continuous scans
        r.ask({"op": "rescan"})
    assert r.opens == 2


def test_bad_requests_are_logged_once_then_counted(caplog):
    r = Rig()
    with caplog.at_level(logging.INFO, logger="louver-ram"):
        for t in range(120):
            r.now = float(t)
            r.ask(b"nope")
    bad = [rec.getMessage() for rec in caplog.records if "bad request" in rec.getMessage()]
    assert bad == [
        "bad request: not a JSON line",
        "bad request: not a JSON line (and 59 more since the last one logged)",
    ]
    assert all("nope" not in m for m in bad)  # the reason only, never the request


def test_open_intel_bus_needs_ddr5_before_it_touches_the_bus(tmp_path):
    opened = []
    dmi, sysfs, dev = tmp_path / "dmi", tmp_path / "sys", tmp_path / "dev"
    for d in (dmi, sysfs / "i2c-3", dev):
        d.mkdir(parents=True)
    (sysfs / "i2c-3" / "name").write_text("SMBus I801 adapter at 0000:00:1f.4\n")
    (dev / "i2c-3").write_text("")
    _dimm(dmi, 0, 0x1A)  # DDR4
    with pytest.raises(cd.NoBus, match="not a DDR5 system: the firmware lists DDR4 memory"):
        ramhelper.open_intel_bus(dmi, sysfs, dev, make=opened.append)
    assert opened == []
    _dimm(dmi, 0, 0x22)
    name, node, bus = ramhelper.open_intel_bus(dmi, sysfs, dev, make=lambda n: opened.append(n) or SimSMBus({}))
    assert (name, node, opened) == ("SMBus I801 adapter at 0000:00:1f.4", str(dev / "i2c-3"), [str(dev / "i2c-3")])
    assert isinstance(bus, cd.SafeBus)


def _dimm(dmi, n, mem_type, size=16384):
    """A type 17 entry as /sys/firmware/dmi/entries/17-N/raw holds it."""
    d = dmi / f"17-{n}"
    d.mkdir(exist_ok=True)
    raw = bytearray(0x28)
    raw[0], raw[1] = 17, 0x28
    raw[0x0C:0x0E] = size.to_bytes(2, "little")
    raw[0x12] = mem_type
    (d / "raw").write_bytes(bytes(raw) + b"DIMM 1\0\0")


def test_a_failed_write_is_an_io_error_reply():
    r = Rig()
    r.ask({"op": "list"})

    def eio(addr, reg, data):
        raise OSError(errno.EIO, "Input/output error")

    r.buses[0].write_block_data = eio
    got = r.ask({"op": "set", "addr": 0x19, "colors": WHITE10})
    assert got == {"ok": False, "error": "write to 0x19 failed: Input/output error", "code": "io"}


def test_close_closes_the_bus():
    r = Rig()
    r.ask({"op": "list"})
    r.helper.close()
    assert r.buses[0].closed


# ---------------- the socket server ----------------
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def served(tmp_path):
    """A Server on a real AF_UNIX socket in a temp dir, over a simulated bus,
    stepped by the test with an injected clock."""
    path = str(tmp_path / "ram.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen()
    rig = Rig()
    clock = Clock()
    server = Server(listener, rig.helper, clock=clock, idle_exit=60.0)
    yield server, path, clock, rig
    server.close()
    listener.close()


def _client(path):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.settimeout(2)
    c.connect(path)
    return c


def _reply(server, c):
    server.step(1.0)
    data = b""
    while not data.endswith(b"\n"):
        data += c.recv(65536)
    return [json.loads(x) for x in data.splitlines()]


def test_server_answers_each_line_and_several_in_one_packet(served):
    server, path, clock, rig = served
    c = _client(path)
    server.step(1.0)  # accept
    c.sendall(b'{"op":"list"}\n{"op":"set","addr":25,"colors":' + json.dumps(WHITE10).encode() + b"}\n")
    replies = _reply(server, c)
    while len(replies) < 2:
        replies += _reply(server, c)
    assert replies[0]["ok"] and len(replies[0]["sticks"]) == 2 and replies[1] == {"ok": True}
    c.close()


def test_an_over_long_line_is_refused_and_the_client_dropped(served):
    server, path, clock, rig = served
    c = _client(path)
    server.step(1.0)
    c.sendall(b" " * MAX_LINE + b"\n")  # exactly the limit: answered (it isn't JSON), kept
    (got,) = _reply(server, c)
    assert got["code"] == "bad_request" and len(server.clients) == 1
    c.sendall(b" " * (MAX_LINE + 1))  # no newline yet, already over
    (got,) = _reply(server, c)
    assert got["error"] == f"line over {MAX_LINE} bytes" and server.clients == {}
    assert c.recv(10) == b""  # closed
    assert all(st.blocks == [] for st in rig.sticks.values())


def test_idle_exit_counts_from_the_last_disconnect(served):
    server, path, clock, rig = served
    clock.now += 59
    assert not server.idle()
    clock.now += 1
    assert server.idle()  # nobody came: exits a minute after start
    c = _client(path)
    server.step(1.0)
    clock.now += 600
    assert not server.idle()  # a client is connected: never idle
    c.close()
    server.step(1.0)  # sees the EOF
    assert server.clients == {} and server.last_active == clock.now
    clock.now += 59.5
    assert not server.idle()
    clock.now += 0.5
    assert server.idle()


def test_run_returns_once_idle(tmp_path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(tmp_path / "s"))
    listener.listen()
    rig = Rig()
    server = Server(listener, rig.helper, idle_exit=0.05)
    t = threading.Thread(target=server.run)
    t.start()
    t.join(5)
    assert not t.is_alive() and rig.opens == 0  # never scanned: nobody asked
    listener.close()


def test_too_many_clients_are_turned_away(served, monkeypatch):
    server, path, clock, rig = served
    monkeypatch.setattr(ramhelper, "MAX_CLIENTS", 2)
    cs = [_client(path) for _ in range(3)]
    for _ in range(3):
        server.step(0.2)
    assert len(server.clients) == 2
    assert cs[2].recv(10) == b""
    for c in cs:
        c.close()


def test_a_client_that_doesnt_read_its_replies_is_dropped_without_stalling_the_others(served):
    """The reviewer's repro: one client pipelines requests and never reads.
    Each step stays quick, that client is dropped once its socket is full, and
    an ordinary client is answered meanwhile."""
    server, path, clock, rig = served
    mute = _client(path)
    mute.setblocking(False)
    victim = _client(path)
    while len(server.clients) < 2:
        server.step(0.2)
    victim.sendall(b'{"op":"list"}\n')
    slowest, rounds = 0.0, 0
    while len(server.clients) == 2 and rounds < 5000:
        rounds += 1
        try:
            for _ in range(1000):
                mute.send(b'{"op":"list"}\n')
        except BlockingIOError:
            pass
        t0 = time.monotonic()
        server.step(0)
        slowest = max(slowest, time.monotonic() - t0)
    assert len(server.clients) == 1, rounds  # the mute one went
    assert slowest < 0.25  # never a blocking send
    (got,) = _reply(server, victim)
    assert got["ok"] and len(got["sticks"]) == 2
    victim.close()
    mute.close()


def test_one_request_in_flight_per_client(served):
    server, path, clock, rig = served
    c = _client(path)
    server.step(0.2)
    c.sendall(b'{"op":"list"}\n{"op":"list"}\n{"op":"list"}\n')
    server.step(0.5)
    c.setblocking(False)
    assert c.recv(65536).count(b"\n") == 1  # one reply per turn, however many lines came
    (conn,) = server.clients
    assert server.pending == [conn] and conn not in server.sel.get_map().values()  # not read from meanwhile
    server.step(0.5)
    server.step(0.5)
    assert c.recv(65536).count(b"\n") == 2 and server.pending == []
    c.close()


class _ResetSocket(socket.socket):
    def recv(self, *args):
        raise ConnectionResetError(104, "Connection reset by peer")


def test_a_receive_error_drops_the_client(served):
    server, path, clock, rig = served
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    bad = _ResetSocket(socket.AF_UNIX, socket.SOCK_STREAM, 0, os.dup(a.fileno()))
    a.close()
    bad.setblocking(False)
    server.clients[bad] = bytearray()
    server.heard_at[bad] = clock.now
    server.sel.register(bad, ramhelper.selectors.EVENT_READ)
    b.sendall(b'{"op":"list"}\n')
    clock.now += 5
    server.step(0.5)
    assert server.clients == {} and server.last_active == clock.now and rig.opens == 0
    b.close()


def _settle(server, want, rounds=50):
    for _ in range(rounds):
        if len(server.clients) == want:
            return
        server.step(0.05)
    assert len(server.clients) == want


def test_idle_holders_are_dropped_and_a_new_client_is_then_served(served):
    """Any plugdev process could hold every slot: the daemon would be locked
    out and the helper would never exit. Silent clients go after CLIENT_IDLE."""
    server, path, clock, rig = served
    holders = [_client(path) for _ in range(ramhelper.MAX_CLIENTS)]
    _settle(server, ramhelper.MAX_CLIENTS)
    late = _client(path)
    server.step(0.2)
    assert late.recv(1) == b"" and len(server.clients) == ramhelper.MAX_CLIENTS  # turned away
    late.close()
    clock.now += ramhelper.CLIENT_IDLE - 1
    server.step(0)
    assert len(server.clients) == ramhelper.MAX_CLIENTS
    clock.now += 1
    server.step(0)
    assert server.clients == {} and server.heard_at == {}
    assert all(h.recv(1) == b"" for h in holders)
    clock.now += ramhelper.IDLE_EXIT
    assert server.idle()  # nothing keeps the helper running now
    c = _client(path)
    _settle(server, 1)
    c.sendall(b'{"op":"list"}\n')
    (got,) = _reply(server, c)
    assert got["ok"] and len(got["sticks"]) == 2
    for h in (*holders, c):
        h.close()


def test_a_client_that_keeps_asking_is_never_dropped(served):
    server, path, clock, rig = served
    active, silent = _client(path), _client(path)
    _settle(server, 2)
    for _ in range(30):  # 5 minutes, a request every 10 s
        clock.now += 10
        rig.now += 10  # the helper's clock, for its frame rate
        active.sendall(b'{"op":"set","addr":25,"colors":' + json.dumps([[1, 2, 3]] * 10).encode() + b"}\n")
        (got,) = _reply(server, active)
        assert got == {"ok": True}
    assert len(server.clients) == 1 and silent.recv(1) == b""  # only the silent one went
    active.close()
    silent.close()


def test_select_waits_no_longer_than_the_next_silent_clients_deadline(served):
    server, path, clock, rig = served
    c = _client(path)
    _settle(server, 1)
    clock.now += ramhelper.CLIENT_IDLE - 0.05
    assert server._wait(None, clock.now) == pytest.approx(0.05)
    assert server._wait(0.01, clock.now) == 0.01
    t0 = time.monotonic()
    server.step(None)  # no limit asked for: still back by the deadline
    assert time.monotonic() - t0 < 1.0 and len(server.clients) == 1
    clock.now += 0.05
    server.step(0)
    assert server.clients == {}
    assert server._wait(None, clock.now) == pytest.approx(server.idle_exit)  # nobody left: the idle exit
    clock.now += server.idle_exit - 0.05
    assert server._wait(None, clock.now) == pytest.approx(0.05)
    t0 = time.monotonic()
    server.step(None)
    assert time.monotonic() - t0 < 1.0
    clock.now += 0.05
    assert server.idle()
    c.close()


def test_client_trouble_shares_one_log_budget(served, caplog):
    server, path, clock, rig = served
    with caplog.at_level(logging.INFO, logger="louver-ram"):
        for _ in range(3):
            c = _client(path)
            _settle(server, 1)
            c.sendall(b"x" * (ramhelper.MAX_LINE + 2))
            _settle(server, 0)  # dropped: line too long
            c.close()
        c = _client(path)
        _settle(server, 1)
        c.sendall(b"nope\n")
        _reply(server, c)
        rig.now += ramhelper.BAD_LOG_EVERY
        c.sendall(b"nope\n")
        _reply(server, c)
        clock.now += ramhelper.CLIENT_IDLE
        rig.now += ramhelper.BAD_LOG_EVERY
        server.step(0)
        c.close()
    msgs = [rec.getMessage() for rec in caplog.records if rec.name == "louver-ram"]
    assert msgs == [
        "dropped a client: request line over 4096 bytes",
        "bad request: not a JSON line (and 3 more since the last one logged)",
        "dropped a client that sent nothing for 60 s",
    ]


def test_end_to_end_through_the_client(tmp_path):
    """RamHelperAccess -> the socket -> Server -> Helper -> SafeBus -> simulated sticks."""
    path = str(tmp_path / "ram.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen()
    rig = Rig()
    server = Server(listener, rig.helper, idle_exit=0.3)
    t = threading.Thread(target=server.run)
    t.start()
    try:
        client = RamHelperAccess(path, timeout=2).connect()
        listing = client.list()
        assert [(s.addr, s.leds) for s in listing.sticks] == [(0x19, 10), (0x1B, 10)] and listing.bus.startswith(
            "SMBus I801"
        )
        client.set(0x19, [(1, 2, 3)] * 10)
        assert rig.sticks[0x19].colors() == [(1, 2, 3)] * 10
        with pytest.raises(RamHelperError) as e:
            client.set(0x19, [(1, 2, 3)] * 9)
        assert e.value.code == "bad_request"
        client.close()
    finally:
        t.join(5)
        listener.close()
    assert not t.is_alive()


# ---------------- socket activation ----------------
def test_systemd_listener_takes_only_the_socket_passed_to_this_process(tmp_path):
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(tmp_path / "s"))
    listener.listen()
    fd = os.dup(listener.fileno())
    for env in ({}, {"LISTEN_PID": "1", "LISTEN_FDS": "1"}, {"LISTEN_PID": "77", "LISTEN_FDS": "2"}):
        with pytest.raises(RuntimeError, match="not socket-activated"):
            ramhelper.systemd_listener(env, getpid=lambda: 77, fd=fd)
    env = {"LISTEN_PID": "77", "LISTEN_FDS": "1", "LISTEN_FDNAMES": "louver-ram.socket", "OTHER": "x"}
    sock = ramhelper.systemd_listener(env, getpid=lambda: 77, fd=fd)
    assert sock.fileno() == fd and sock.family == socket.AF_UNIX and env == {"OTHER": "x"}
    sock.close()
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    with pytest.raises(RuntimeError, match="listening AF_UNIX stream"):
        ramhelper.systemd_listener({"LISTEN_PID": "77", "LISTEN_FDS": "1"}, getpid=lambda: 77, fd=udp.fileno())
    udp.close()
    bound = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound.bind(str(tmp_path / "not-listening"))  # SO_ACCEPTCONN is 0 until listen()
    with pytest.raises(RuntimeError, match="listening AF_UNIX stream"):
        ramhelper.systemd_listener({"LISTEN_PID": "77", "LISTEN_FDS": "1"}, getpid=lambda: 77, fd=bound.fileno())
    bound.close()
    listener.close()


def test_main_refuses_to_run_unless_socket_activated(monkeypatch, capsys):
    def no_bus():
        raise AssertionError("must not touch the bus")

    monkeypatch.setattr(ramhelper, "open_intel_bus", no_bus)
    for k in ("LISTEN_PID", "LISTEN_FDS"):
        monkeypatch.delenv(k, raising=False)
    assert ramhelper.main([]) == 2
    assert "not socket-activated" in capsys.readouterr().err


def test_helper_imports_only_the_driver_and_the_stdlib():
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); before = set(sys.modules); import louver.ramhelper; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'louver')); "
        "new = {m.split('.')[0] for m in set(sys.modules) - before}; "
        "print(sorted(new - set(sys.stdlib_module_names) - {'louver'}))"
    )
    out = subprocess.run([sys.executable, "-I", "-c", code, str(ROOT)], capture_output=True, text=True, check=True)
    louver_mods, others = out.stdout.splitlines()
    assert louver_mods == "['louver', 'louver.corsair_dram', 'louver.ramhelper']"
    assert others == "[]"
