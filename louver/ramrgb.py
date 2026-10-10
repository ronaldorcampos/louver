"""RAM RGB from the user side: a client for the louver-ram root helper
(louver/ramhelper.py) and the looks Louver draws on the sticks.

Louver itself never opens /dev/i2c-*: it asks the helper, over
/run/louver-ram.sock, which only plugdev can reach.
"""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass

from .devices import Color, parse_color
from .rgbfx import gradient

SOCKET_PATH = "/run/louver-ram.sock"
# The daemon waits for the helper under its control lock, so these bound how
# long a slow or stuck helper can hold up fan control.
# A connect returns at once: systemd holds the listening socket.
CONNECT_TIMEOUT = 0.5
# A list's first answer includes the scan: 7 gaps of 10 ms between the 8
# addresses, plus 37 byte transfers per stick (under 1 ms each at 100 kHz),
# so about 150 ms for 2 sticks and 370 ms if all 8 addresses held one, plus
# the helper's start (~0.1 s) on the first connection. A scan slower than
# this costs one failed attempt: the helper keeps its result for the next.
LIST_TIMEOUT = 0.5
# A frame is one or two block writes of 32 bytes or less, a few ms.
SET_TIMEOUT = 0.3
# `louver ram-probe` holds no lock and exists to diagnose a slow or cold
# helper, so it waits much longer than the daemon may.
PROBE_TIMEOUT = 5.0
MAX_REPLY = 1 << 16

# Software looks, drawn by Louver as one static frame per stick.
MODES = ("Off", "Static", "Gradient")
MAX_COLORS = 6


def lighting_modes() -> list[dict]:
    return [
        {"name": "Off", "max_colors": 0},
        {"name": "Static", "max_colors": 1},
        {"name": "Gradient", "max_colors": MAX_COLORS},
    ]


def render(look: dict, leds: int) -> list[Color]:
    """One stick's frame for a ram_rgb look, LED 0 first."""
    if look["mode"] == "Off":
        return [(0, 0, 0)] * leds
    stops = [parse_color(c) for c in look["colors"]]
    if look["mode"] == "Static" or len(stops) == 1:
        return [stops[0]] * leds
    return [gradient(stops, i / max(1, leds - 1)) for i in range(leds)]


class RamHelperError(Exception):
    """The helper answered with an error."""

    def __init__(self, msg: str, code: str):
        super().__init__(msg)
        self.code = code


@dataclass(frozen=True)
class RamStick:
    addr: int
    name: str
    pid: int
    leds: int
    firmware: str
    protocol: int

    def describe(self) -> dict:
        return {
            "addr": self.addr,
            "name": self.name,
            "pid": self.pid,
            "leds": self.leds,
            "firmware": self.firmware,
            "protocol": self.protocol,
        }


@dataclass(frozen=True)
class Listing:
    bus: str
    device: str
    sticks: list[RamStick]
    skipped: list[tuple[int, str]]
    error: str
    retry: bool  # scanning again might find more: an I/O error or a busy bus, not an answer


def _int(v, lo: int, hi: int) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
        raise ValueError(f"bad value from the RAM helper: {v!r}")
    return v


def _str(v) -> str:
    if not isinstance(v, str):
        raise ValueError(f"bad value from the RAM helper: {v!r}")
    return v


def parse_listing(reply: dict) -> Listing:
    """A list reply, checked: the LED counts size the frames Louver sends."""
    try:
        sticks = [
            RamStick(
                _int(s["addr"], 0x18, 0x1F),
                _str(s["name"]),
                _int(s["pid"], 0, 0xFFFF),
                _int(s["leds"], 1, 64),
                _str(s["firmware"]),
                _int(s["protocol"], 0, 255),
            )
            for s in reply["sticks"]
        ]
        skipped = [(_int(s["addr"], 0, 127), _str(s["reason"])) for s in reply["skipped"]]
        retry = reply["retry"]
        if not isinstance(retry, bool):
            raise ValueError(f"bad value from the RAM helper: {retry!r}")
        return Listing(_str(reply["bus"]), _str(reply["device"]), sticks, skipped, _str(reply["error"]), retry)
    except (KeyError, TypeError) as e:
        raise ValueError(f"malformed list reply from the RAM helper: {e!r}") from None


class RamClient:
    """One connection to the helper: a request, then its reply."""

    def __init__(self, sock: socket.socket):
        self._sock = sock
        self._buf = b""

    def request(self, req: dict, timeout: float = LIST_TIMEOUT) -> dict:
        """Send one request and wait at most ``timeout`` in all for its reply."""
        deadline = time.monotonic() + timeout
        self._sock.settimeout(timeout)
        self._sock.sendall(json.dumps(req, separators=(",", ":")).encode() + b"\n")
        while b"\n" not in self._buf:
            if len(self._buf) > MAX_REPLY:
                raise ValueError("RAM helper reply too long")
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("timed out")
            self._sock.settimeout(left)
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("the RAM helper closed the connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        reply = json.loads(line)
        if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
            raise ValueError("malformed reply from the RAM helper")
        if not reply["ok"]:
            raise RamHelperError(str(reply.get("error", "")), str(reply.get("code", "")))
        return reply

    def list(self, rescan: bool = False, timeout: float = LIST_TIMEOUT) -> Listing:
        return parse_listing(self.request({"op": "rescan" if rescan else "list"}, timeout))

    def set(self, addr: int, colors: list[Color]) -> None:
        self.request({"op": "set", "addr": addr, "colors": [list(c) for c in colors]}, SET_TIMEOUT)

    def close(self) -> None:
        self._sock.close()


class RamHelperAccess:
    """How the daemon reaches the helper."""

    def __init__(self, path: str = SOCKET_PATH, timeout: float = CONNECT_TIMEOUT):
        self.path = path
        self.timeout = timeout

    def connect(self) -> RamClient:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            raise
        return RamClient(sock)


def connect_error(path: str, e: OSError) -> str:
    """Why the helper can't be reached, in words."""
    if isinstance(e, FileNotFoundError):
        return f"the RAM helper isn't running: {path} is missing (it comes with the louver package)"
    if isinstance(e, PermissionError):
        return f"no permission to reach the RAM helper at {path}: your user must be in the plugdev group"
    if isinstance(e, ConnectionRefusedError):
        return f"the RAM helper's socket at {path} isn't listening (systemctl status louver-ram.socket)"
    return f"cannot reach the RAM helper at {path}: {e}"
