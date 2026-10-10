"""Simulated controllers for `louver daemon --demo` (UI work without hardware)."""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

from .aura import REPORT_ID as AURA_REPORT_ID
from .aura import AuraMainboard
from .devices import StrimerPlus, UniHubALV2
from .mbfans import MotherboardFans


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
