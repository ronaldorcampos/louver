"""louver-ram: the root helper that alone touches the SMBus for RAM RGB.

Run by systemd as ``python3 -I -m louver.ramhelper`` from louver-ram.service,
socket-activated by louver-ram.socket (/run/louver-ram.sock, group plugdev).
It imports nothing of Louver's but louver.corsair_dram, whose SafeBus
refuses every register, address and length that driver doesn't use.

Protocol: one JSON object per line, one reply per line, lines at most
MAX_LINE bytes. A client gets the reply to one request before its next one
is read; one that doesn't read its replies is dropped.

  {"op": "list"}    -> {"ok": true, "bus", "device", "sticks": [...], "skipped": [...], "error", "retry"}
  {"op": "rescan"}  -> the same, after scanning again if the last scan is old enough (see Helper)
  {"op": "set", "addr": 25, "colors": [[r, g, b], ...]}  one colour per LED -> {"ok": true}

"retry" is true when scanning again might find more: the bus couldn't be
opened, or an address gave an I/O error or a busy bus rather than an answer.
Errors: {"ok": false, "error": "...", "code": "bad_request" | "unknown_addr"
| "too_soon" | "io"}. The bus is scanned on the first request, not at
start; the helper exits once no client has been connected for IDLE_EXIT
seconds, and the socket starts it again on the next connection. A client
that sends no request for CLIENT_IDLE seconds is dropped, so idle
connections can't keep the helper running or lock other clients out.
"""

from __future__ import annotations

import json
import logging
import os
import selectors
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import corsair_dram as cd

log = logging.getLogger("louver-ram")

MAX_LINE = 4096
IDLE_EXIT = 60.0
RESCAN_EVERY = 30.0  # a rescan sooner than this returns the last scan...
RESCAN_RETRY_EVERY = 5.0  # ...unless it found no stick or hit I/O errors: then this soon
# Per stick, a token bucket: FRAME_BURST frames at once, FRAME_RATE a second
# after that. A frame past it is refused (too_soon), never queued.
FRAME_BURST = 4
FRAME_RATE = 1.0
MAX_CLIENTS = 8
# A client silent this long is dropped. The daemon sends a frame at least
# every KEEPALIVE_EVERY (10 s), but only on a tick, and ticks can be up to
# 30 s apart (poll_interval): so 10 + 30 s at worst, plus room for a slow tick.
CLIENT_IDLE = 60.0
BAD_LOG_EVERY = 60.0  # client trouble: the first is logged, then a count at most this often
SD_LISTEN_FDS_START = 3

BusOpener = Callable[[], tuple[str, str, cd.SafeBus]]


def open_intel_bus(
    dmi: Path = cd.SYSFS_DMI,
    sysfs: Path = cd.SYSFS_I2C,
    dev: Path = Path("/dev"),
    make: Callable[[str], cd.RawSMBus] = cd.I2CDev,
) -> tuple[str, str, cd.SafeBus]:
    """(adapter name, /dev node, bus) for the one Intel SMBus adapter, on a
    DDR5 system only. Raises NoBus before opening anything otherwise."""
    cd.require_ddr5(dmi)
    name, node = cd.find_intel_bus(sysfs, dev)
    return name, node, cd.SafeBus(make(node))


class BadRequest(ValueError):
    pass


def _int(v, lo: int, hi: int, what: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise BadRequest(f"{what} must be an integer {lo}..{hi}")
    return v


def _no_constants(name: str):
    raise BadRequest(f"{name} isn't JSON")


def parse_request(line: bytes) -> dict:
    try:
        req = json.loads(line.decode("utf-8"), parse_constant=_no_constants)
    except (ValueError, RecursionError):  # bad UTF-8 or JSON, NaN, too deep
        raise BadRequest("not a JSON line") from None
    if not isinstance(req, dict):
        raise BadRequest("a request is a JSON object")
    op = req.get("op")
    keys = {"list": {"op"}, "rescan": {"op"}, "set": {"op", "addr", "colors"}}.get(op) if isinstance(op, str) else None
    if keys is None:
        raise BadRequest("unknown op")
    if set(req) != keys:
        raise BadRequest(f"{op} takes exactly {sorted(keys)}")
    if op == "set":
        _int(req["addr"], 0, 127, "addr")
        colors = req["colors"]
        if not isinstance(colors, list):
            raise BadRequest("colors must be a list")
        for c in colors:
            if not isinstance(c, list) or len(c) != 3:
                raise BadRequest("each colour is [r, g, b]")
            for v in c:
                _int(v, 0, 255, "a colour value")
    return req


class LogThrottle:
    """For what any plugdev process can cause at will (bad requests, clients
    dropped for misbehaving): the first is logged, then at most one line every
    ``every`` seconds, with a count of the ones held back."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, every: float = BAD_LOG_EVERY):
        self._clock = clock
        self._every = every
        self._at = -1e9
        self._quiet = 0

    def __call__(self, msg: str) -> None:
        now = self._clock()
        if now - self._at < self._every:
            self._quiet += 1
            return
        more = f" (and {self._quiet} more since the last one logged)" if self._quiet else ""
        log.info("%s%s", msg, more)
        self._at, self._quiet = now, 0


class Helper:
    """The protocol over one bus, without the sockets. All bus access is
    from handle(), one request at a time.

    A rescan scans again once the last scan is RESCAN_EVERY old, or only
    RESCAN_RETRY_EVERY old when it found no stick or hit I/O errors, so a
    stick that wasn't there or didn't answer is found again soon after, but
    no client can keep the bus busy with scans."""

    def __init__(
        self,
        open_bus: BusOpener = open_intel_bus,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._open_bus = open_bus
        self._clock = clock
        self._sleep = sleep
        self._bus: cd.SafeBus | None = None
        self._sticks: dict[int, cd.Stick] = {}
        self._listing: dict | None = None
        self._scanned_at = -1e9
        self._tokens: dict[int, tuple[float, float]] = {}  # addr -> (tokens, when)
        self.client_trouble = LogThrottle(clock)  # shared with Server: one budget for all of it

    def _scan(self) -> dict:
        self.close()
        self._scanned_at = self._clock()
        empty = {"bus": "", "device": "", "sticks": [], "skipped": []}
        try:
            name, node, bus = self._open_bus()
        except cd.NoBus as e:
            listing = {**empty, "error": str(e), "retry": False}
        except OSError as e:
            listing = {**empty, "error": f"cannot open the bus: {e}", "retry": True}
        else:
            self._bus = bus
            sticks, skipped = cd.scan(bus, self._sleep)
            self._sticks = {s.addr: s for s in sticks}
            listing = {
                "bus": name,
                "device": node,
                "sticks": [s.describe() for s in sticks],
                "skipped": skipped,
                "error": "" if sticks else "no Corsair DDR5 RGB stick found",
                "retry": any(s["retry"] for s in skipped),
            }
        log.info(
            "scan: %s; %d stick(s) %s; skipped %s",
            listing["error"] or f"{listing['bus']} ({listing['device']})",
            len(listing["sticks"]),
            [f"{s['addr']:#04x} {s['name']}" for s in listing["sticks"]],
            [f"{s['addr']:#04x}: {s['reason']}" for s in listing["skipped"]],
        )
        self._listing = listing
        return listing

    def _rescan_due(self) -> bool:
        last = self._listing
        assert last is not None
        every = RESCAN_RETRY_EVERY if not last["sticks"] or last["retry"] else RESCAN_EVERY
        return self._clock() - self._scanned_at >= every

    def _list(self, rescan: bool) -> dict:
        if self._listing is None or (rescan and self._rescan_due()):
            self._scan()
        assert self._listing is not None
        return {"ok": True, **self._listing}

    def _take_token(self, addr: int) -> bool:
        now = self._clock()
        tokens, at = self._tokens.get(addr, (float(FRAME_BURST), now))
        tokens = min(float(FRAME_BURST), tokens + (now - at) * FRAME_RATE)
        ok = tokens >= 1.0
        self._tokens[addr] = (tokens - 1.0 if ok else tokens, now)
        return ok

    def _set(self, addr: int, colors: list) -> dict:
        if self._listing is None:
            self._scan()
        stick = self._sticks.get(addr)
        if stick is None or self._bus is None:
            return _error("unknown_addr", f"no stick at {addr:#04x}")
        if len(colors) != stick.device.leds:
            return _error("bad_request", f"the stick at {addr:#04x} has {stick.device.leds} LEDs")
        if not self._take_token(addr):
            return _error("too_soon", f"too many frames for {addr:#04x}: at most {FRAME_RATE:g} a second")
        try:
            cd.set_colors(self._bus, stick, [tuple(c) for c in colors])
        except OSError as e:
            log.warning("set %#04x failed: %s", addr, e)
            return _error("io", f"write to {addr:#04x} failed: {e.strerror or e}")
        return {"ok": True}

    def handle(self, line: bytes) -> dict:
        try:
            req = parse_request(line)
        except BadRequest as e:
            self.client_trouble(f"bad request: {e}")
            return _error("bad_request", str(e))
        if req["op"] == "set":
            return self._set(req["addr"], req["colors"])
        return self._list(rescan=req["op"] == "rescan")

    def close(self) -> None:
        bus, self._bus = self._bus, None
        self._sticks = {}
        if bus is not None:
            try:
                bus.close()
            except OSError:
                pass


def _error(code: str, msg: str) -> dict:
    return {"ok": False, "error": msg, "code": code}


def _encode(reply: dict) -> bytes:
    return json.dumps(reply, separators=(",", ":")).encode() + b"\n"


class Server:
    """Serves Helper on the listening socket, one request at a time across
    every client, round robin. Sockets never block: a client whose reply
    doesn't fit its socket at once (it isn't reading) is dropped, and a
    client isn't read from while it has a whole request waiting, and one
    that sends no whole request for ``client_idle`` seconds is dropped.
    idle() turns true once no client has been connected for ``idle_exit``
    seconds."""

    def __init__(
        self,
        listener: socket.socket,
        helper: Helper,
        clock: Callable[[], float] = time.monotonic,
        idle_exit: float = IDLE_EXIT,
        client_idle: float = CLIENT_IDLE,
    ):
        self.listener = listener
        self.helper = helper
        self.clock = clock
        self.idle_exit = idle_exit
        self.client_idle = client_idle
        self.clients: dict[socket.socket, bytearray] = {}
        self.heard_at: dict[socket.socket, float] = {}  # when each client's last whole request came
        self.pending: list[socket.socket] = []  # have a whole line waiting: not read from until it's answered
        self.last_active = clock()
        self.sel = selectors.DefaultSelector()
        listener.setblocking(False)
        self.sel.register(listener, selectors.EVENT_READ)

    def idle(self) -> bool:
        return not self.clients and self.clock() - self.last_active >= self.idle_exit

    def step(self, timeout: float | None) -> None:
        """Wait at most ``timeout`` (None: no limit but the next silent
        client's deadline) for something to do, and do it."""
        now = self.clock()
        self._expire(now)
        for key, _ in self.sel.select(self._wait(timeout, now)):
            if key.fileobj is self.listener:
                self._accept()
            else:
                self._read(key.fileobj)  # type: ignore[arg-type]
        for conn in list(self.pending):
            if conn in self.clients:
                self._serve_one(conn)

    def _expire(self, now: float) -> None:
        for conn, at in list(self.heard_at.items()):
            if now - at >= self.client_idle and conn not in self.pending:
                self.helper.client_trouble(f"dropped a client that sent nothing for {self.client_idle:g} s")
                self._drop(conn)

    def _wait(self, timeout: float | None, now: float) -> float:
        """How long select may wait: ``timeout`` at most, and never past the
        next silent client's deadline or, with no client, the idle exit."""
        if self.pending:
            return 0.0
        if self.heard_at:
            deadline = min(self.heard_at.values()) + self.client_idle
        else:
            deadline = self.last_active + self.idle_exit
        left = max(0.0, deadline - now)
        return left if timeout is None else min(timeout, left)

    def run(self) -> None:
        while not self.idle():
            self.step(None)  # bounded by step: the next silent client's deadline, or the idle exit
        self.close()

    def _accept(self) -> None:
        try:
            conn, _ = self.listener.accept()
        except (BlockingIOError, InterruptedError):
            return
        if len(self.clients) >= MAX_CLIENTS:
            conn.close()
            return
        conn.setblocking(False)
        self.clients[conn] = bytearray()
        self.heard_at[conn] = self.clock()
        self.sel.register(conn, selectors.EVENT_READ)

    def _drop(self, conn: socket.socket) -> None:
        if conn in self.pending:
            self.pending.remove(conn)
        else:
            self.sel.unregister(conn)
        del self.clients[conn]
        del self.heard_at[conn]
        conn.close()
        if not self.clients:
            self.last_active = self.clock()

    def _send(self, conn: socket.socket, data: bytes) -> bool:
        """Send without blocking; a reply that doesn't all fit drops the client."""
        try:
            sent = conn.send(data)
        except BlockingIOError:
            sent = 0
        except OSError:
            self._drop(conn)
            return False
        if sent != len(data):
            self.helper.client_trouble("dropped a client that doesn't read its replies")
            self._drop(conn)
            return False
        return True

    def _too_long(self, conn: socket.socket) -> bool:
        buf = self.clients[conn]
        end = buf.find(b"\n")
        if end > MAX_LINE or (end < 0 and len(buf) > MAX_LINE):
            self.helper.client_trouble(f"dropped a client: request line over {MAX_LINE} bytes")
            try:
                conn.send(_encode(_error("bad_request", f"line over {MAX_LINE} bytes")))
            except OSError:
                pass
            self._drop(conn)
            return True
        return False

    def _read(self, conn: socket.socket) -> None:
        try:
            data = conn.recv(MAX_LINE + 1)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop(conn)
            return
        if not data:
            self._drop(conn)
            return
        self.clients[conn] += data
        if not self._too_long(conn) and b"\n" in self.clients[conn]:
            self.heard_at[conn] = self.clock()
            self.sel.unregister(conn)
            self.pending.append(conn)

    def _serve_one(self, conn: socket.socket) -> None:
        buf = self.clients[conn]
        end = buf.find(b"\n")
        line = bytes(buf[:end])
        del buf[: end + 1]
        if not self._send(conn, _encode(self.helper.handle(line))):
            return
        if self._too_long(conn):
            return
        self.pending.remove(conn)
        if b"\n" in buf:
            self.pending.append(conn)  # its next line, after everyone else's
        else:
            self.sel.register(conn, selectors.EVENT_READ)

    def close(self) -> None:
        for conn in list(self.clients):
            self._drop(conn)
        self.sel.close()
        self.helper.close()


def systemd_listener(env=None, getpid: Callable[[], int] = os.getpid, fd: int = SD_LISTEN_FDS_START) -> socket.socket:
    """The listening socket systemd passed (sd_listen_fds with one fd)."""
    env = os.environ if env is None else env
    if env.get("LISTEN_PID") != str(getpid()) or env.get("LISTEN_FDS") != "1":
        raise RuntimeError("not socket-activated: start louver-ram.socket, not this program")
    for k in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
        env.pop(k, None)
    sock = socket.socket(fileno=fd)
    if (
        sock.family != socket.AF_UNIX
        or sock.type != socket.SOCK_STREAM
        or not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    ):
        sock.detach()
        raise RuntimeError("the passed socket isn't a listening AF_UNIX stream socket")
    return sock


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    try:
        listener = systemd_listener()
    except (RuntimeError, OSError) as e:
        print(f"louver-ram: {e}", file=sys.stderr)
        return 2
    Server(listener, Helper()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
