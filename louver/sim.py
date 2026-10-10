"""Simulated controllers for `louver daemon --demo` (UI work without hardware)."""

from __future__ import annotations

import errno
import json
import random
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from . import corsair_dram
from .aura import REPORT_ID as AURA_REPORT_ID
from .aura import AuraMainboard
from .devices import StrimerPlus, UniHubALV2
from .mbfans import MotherboardFans
from .ramhelper import Helper
from .ramrgb import RamClient, RamHelperError


class SimHubTransport:
    def __init__(self):
        self.duty = [50, 50, 50, 50]
        self._last = b""

    def send_feature(self, data: bytes) -> None:
        if data[1] & 0xF0 == 0x20 and len(data) == 4:
            self.duty[data[1] & 0x0F] = data[3]
        self._last = bytes(data[1:3])

    def write(self, data: bytes) -> None:
        pass

    def get_input(self, report_id: int, length: int) -> bytes:
        if self._last == bytes([0x50, 0x01]):
            return bytes([0xE0, 0x50, 0x80, 0xC6, 0x12]).ljust(length, b"\0")
        out = bytearray([0xF0])
        for d in self.duty:
            rpm = 0 if d == 0 else int(250 + 17.5 * d + random.uniform(-15, 15))
            out += rpm.to_bytes(2, "big")
        return bytes(out).ljust(length, b"\0")

    def close(self) -> None:
        pass


class SimStrimerTransport:
    def __init__(self):
        self._last = b""

    def send_feature(self, data: bytes) -> None:
        self._last = bytes(data[1:3])

    def write(self, data: bytes) -> None:
        pass

    def get_input(self, report_id: int, length: int) -> bytes:
        if self._last == bytes([0x50, 0x01]):
            return bytes([6]).ljust(length, b"\0")
        return bytes([0xE0, 0x52, 0xFF, 0x40, 0x05]).ljust(length, b"\0")

    def close(self) -> None:
        pass


class SimAuraTransport:
    """An Aura mainboard controller: 5 onboard LEDs (one of them a 12 V RGB
    header) and 3 ARGB headers. Like the real one, its config table says
    nothing about how many LEDs are on the ARGB headers."""

    FIRMWARE = b"AULA3-DEMO-0001"

    def __init__(self):
        self.writes: list[bytes] = []
        self._replies: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        if data[1] == 0x82:
            self._replies.append(bytes([AURA_REPORT_ID, 0x02]) + self.FIRMWARE)
        elif data[1] == 0xB0:
            table = bytearray(60)
            table[0x02], table[0x1B], table[0x1D] = 3, 5, 1
            self._replies.append(bytes([AURA_REPORT_ID, 0x30, 0, 0]) + bytes(table))

    def read(self, length: int, timeout: float) -> bytes:
        return self._replies.pop(0).ljust(length, b"\0")[:length] if self._replies else b""

    def close(self) -> None:
        pass


class SimAuraAccess:
    """The daemon's way to the simulated controller: always there, never re-enumerated."""

    def find(self) -> str:
        return "sim:aura"

    def node_id(self, devnode: str) -> str:
        return devnode

    def open(self, devnode: str) -> AuraMainboard:
        return AuraMainboard(SimAuraTransport())


def open_sim(skip=frozenset()) -> dict[int, object]:
    out: dict[int, object] = {}
    if UniHubALV2.PID not in skip:
        out[UniHubALV2.PID] = UniHubALV2(SimHubTransport(), sleep=lambda s: None)
    if StrimerPlus.PID not in skip:
        out[StrimerPlus.PID] = StrimerPlus(SimStrimerTransport(), sleep=lambda s: None)
    return out


def sim_motherboard() -> MotherboardFans:
    """A Nuvoton chip in a temporary sysfs-like tree: a header with nothing on
    it, a radiator header and a pump header. Writes land in the temp files."""
    tmp = tempfile.TemporaryDirectory(prefix="louver-demo-hwmon-")
    d = Path(tmp.name) / "hwmon0"
    d.mkdir()
    (d / "name").write_text("nct6798\n")
    for i, rpm, pwm in ((1, 0, 169), (2, 1450, 140), (7, 3020, 255)):
        (d / f"fan{i}_input").write_text(f"{rpm}\n")
        (d / f"pwm{i}").write_text(f"{pwm}\n")
        (d / f"pwm{i}_enable").write_text("5\n")
    mb = MotherboardFans(Path(tmp.name))
    mb._demo_dir = tmp  # removed with the object
    return mb


class SimCorsairStick:
    """One Corsair DDR5 stick's RGB controller, as the SMBus sees it: the
    identification bytes, the device-info buffer and its checksum, and the
    direct colour blocks it receives."""

    def __init__(self, pid: int = 0x0700, protocol: int = 4, vid: int = corsair_dram.VENDOR_ID):
        info = bytearray(corsair_dram.DEVICE_INFO_SIZE)
        info[0:4] = bytes([vid & 0xFF, vid >> 8, pid & 0xFF, pid >> 8])
        info[8:12] = bytes([3, 1, 0x2C, 0x01])  # firmware 1.3.300
        info[28] = protocol
        self.info = bytes(info)
        self.regs = {corsair_dram.REG_ID_1: 0x1B, corsair_dram.REG_ID_2: 0x04}
        self.checksum: int | None = None  # None: the right one
        self.blocks: list[tuple[int, bytes]] = []
        self._selected = b""
        self._pos = 0

    def read(self, reg: int) -> int:
        if reg == corsair_dram.REG_GET_BINARY_DATA:
            b = self._selected[self._pos] if self._pos < len(self._selected) else 0xFF
            self._pos += 1
            return b
        if reg == corsair_dram.REG_GET_CHECKSUM:
            return corsair_dram.crc8(self._selected) if self.checksum is None else self.checksum
        return self.regs.get(reg, 0xFF)

    def write(self, reg: int, value: int) -> None:
        if reg == corsair_dram.REG_GET_DEVICE_INFO:
            self._selected = self.info
        elif reg == corsair_dram.REG_BINARY_START:
            self._pos = 0

    def write_block(self, reg: int, data: bytes) -> None:
        self.blocks.append((reg, bytes(data)))

    def colors(self) -> list[tuple[int, int, int]]:
        """The LED colours of the last direct frame, in packet order."""
        last = max(i for i, (reg, _) in enumerate(self.blocks) if reg == corsair_dram.REG_COLOR_BLOCK_1)
        frame = b"".join(d for _, d in self.blocks[last:])
        n = frame[0]
        return [tuple(frame[1 + 3 * i : 4 + 3 * i]) for i in range(n)]  # type: ignore[misc]


class SimSMBus:
    """A raw SMBus with simulated sticks on it: {address: SimCorsairStick}.
    An empty address NACKs (ENXIO), as on the i801."""

    def __init__(self, sticks: dict[int, SimCorsairStick]):
        self.sticks = sticks
        self.closed = False

    def _stick(self, addr: int) -> SimCorsairStick:
        if addr not in self.sticks:
            raise OSError(errno.ENXIO, "No such device or address")
        return self.sticks[addr]

    def read_byte_data(self, addr: int, reg: int) -> int:
        return self._stick(addr).read(reg)

    def write_byte_data(self, addr: int, reg: int, value: int) -> None:
        self._stick(addr).write(reg, value)

    def write_block_data(self, addr: int, reg: int, data: bytes) -> None:
        self._stick(addr).write_block(reg, data)

    def close(self) -> None:
        self.closed = True


class SimRamClient(RamClient):
    """A RamClient wired to an in-process helper instead of the socket."""

    def __init__(self, helper: Helper):
        self._helper = helper

    def request(self, req: dict, timeout: float = 0.0) -> dict:
        reply = json.loads(json.dumps(self._helper.handle(json.dumps(req).encode())))
        if not reply["ok"]:
            raise RamHelperError(reply["error"], reply["code"])
        return reply

    def close(self) -> None:
        pass


class SimRamAccess:
    """The daemon's way to a simulated louver-ram helper: the real helper
    code over two simulated Vengeance RGB DDR5 sticks (10 LEDs each)."""

    path = "sim:louver-ram"

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.bus = SimSMBus({0x19: SimCorsairStick(), 0x1B: SimCorsairStick()})
        self.helper = Helper(
            open_bus=lambda: ("SMBus I801 adapter (simulated)", "sim:i2c", corsair_dram.SafeBus(self.bus)),
            clock=clock,
            sleep=lambda s: None,
        )

    def connect(self) -> SimRamClient:
        return SimRamClient(self.helper)
