"""Driver for the ASUS Aura USB mainboard controller: the motherboard's
addressable (ARGB) headers and its onboard LEDs.

Copyright (C) 2026 Ronaldo Campos (this port). Ported to Python and
modified for Louver, 2026-10-10.
SPDX-License-Identifier: GPL-2.0-or-later

A port to Python of OpenRGB's Aura USB mainboard driver, at commit
b190d3e5afb6bd68b4bfa3e948947878d2bf1ba9 of
https://gitlab.com/CalcProgrammer1/OpenRGB, GPL-2.0-or-later. Ported files,
under Controllers/AsusAuraUSBController/:

  AsusAuraUSBController/AsusAuraUSBController.{h,cpp}        (Martin Hartl)
      the firmware and config-table queries
  AsusAuraUSBController/AsusAuraMainboardController.{h,cpp}  (Martin Hartl)
      config-table decoding into channels, effect and effect-colour commands
  AsusAuraUSBController/RGBController_AsusAuraUSB.cpp        (Adam Honse)
      the effect list and which effects take a colour
  AsusAuraUSBControllerDetect.cpp
      the mainboard product IDs and 0x19AF's usage-page filter

Changes from OpenRGB: replies are awaited for a bounded time and unrelated
input reports are skipped; an effect colour that wouldn't fit its report
raises instead of overrunning the buffer.

Not ported, on purpose:
- OpenRGB's "save to device" path (RGBController_AsusAuraMainboard::
  DeviceSaveMode), which sets the shutdown-effect flag and sends the
  0x3F 0x55 commit that stores the effect in the controller's flash. Louver
  never writes the flash, so the board shows its own saved effect again
  after a restart until Louver sets it.
- Direct mode: AuraUSBController::SendDirect (command 0x40, 20 LEDs per
  report, apply flag 0x80, 8-bit overflow flag 0x10), effect 0xFF, and each
  channel's direct channel number (0x04 for the onboard LEDs, the header
  index for an ARGB header). A per-LED feature would re-port it from there.

Every command is one 65-byte output report (report ID 0xEC + 64 bytes);
the two queries are answered with an input report read from the interrupt
endpoint (hidapi's hid_read), not with HIDIOCGINPUT.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .hidraw import HidInfo, HidrawTransport, enumerate_hidraw

OPENRGB_COMMIT = "b190d3e5afb6bd68b4bfa3e948947878d2bf1ba9"

VENDOR_ID = 0x0B05
REPORT_ID = 0xEC
# AURA_MOTHERBOARD_1_PID .. AURA_MOTHERBOARD_5_PID in AsusAuraUSBControllerDetect.cpp.
MAINBOARD_PIDS = (0x18F3, 0x1939, 0x19AF, 0x1AA6, 0x1BED)
# 0x19AF is registered with REGISTER_HID_DETECTOR_PU: only its interface
# whose top-level collection is usage page 0xFF72, usage 0xA1.
USAGE_FILTER = {0x19AF: (0xFF72, 0x00A1)}
SYSFS_HIDRAW = Path("/sys/class/hidraw")

REQUEST_FIRMWARE_VERSION = 0x82
REQUEST_CONFIG_TABLE = 0xB0
REPLY_FIRMWARE_VERSION = 0x02
REPLY_CONFIG_TABLE = 0x30
CONTROL_MODE_EFFECT = 0x35
CONTROL_MODE_EFFECT_COLOR = 0x36
MODE_DIRECT = 0xFF  # not ported: set_effect refuses it

# OpenRGB's mode list for these controllers, in its order, without Direct
# (that one needs per-LED colours streamed by the host).
MODES: dict[str, int] = {
    "Off": 0, "Static": 1, "Breathing": 2, "Flashing": 3, "Spectrum Cycle": 4, "Rainbow": 5,
    "Chase Fade": 7, "Chase": 9, "Spectrum Cycle Breathing": 6, "Spectrum Cycle Chase Fade": 8,
    "Spectrum Cycle Chase": 10, "Spectrum Cycle Wave": 11, "Chase Rainbow Pulse": 12,
    "Random Flicker": 13, "Gentle Transition": 16, "Wave Propagation": 17,
    "Wave Propagation Pause": 18, "Red Pulse": 19,
}  # fmt: skip
# MODE_FLAG_HAS_MODE_SPECIFIC_COLOR: these take one colour, the rest none.
COLOR_MODES = frozenset({"Static", "Breathing", "Flashing", "Chase Fade", "Chase"})

Color = tuple[int, int, int]


class AuraTransport(Protocol):
    """What the Aura driver needs from the wire. Faked in tests."""

    def write(self, data: bytes) -> None: ...

    def read(self, length: int, timeout: float) -> bytes: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class Channel:
    """One entry of OpenRGB's device_info list. ``num_leds`` is what the
    effect-colour mask reserves: the onboard LED count from the config
    table, or 1 for an ARGB header (the table doesn't report how many LEDs
    are on a header)."""

    index: int
    key: str  # "onboard" | "argb-1" ...
    kind: str  # "onboard" | "argb"
    effect_channel: int
    num_leds: int
    rgb_headers: int  # onboard only: how many of its LEDs are 12 V RGB headers

    @property
    def name(self) -> str:
        return "Onboard LEDs" if self.kind == "onboard" else f"ARGB header {self.key.split('-')[1]}"


def decode_config(table: bytes) -> list[Channel]:
    """AuraMainboardController's constructor: the channels a config table describes."""
    total_onboard, rgb_headers, addressable = table[0x1B], table[0x1D], table[0x02]
    if total_onboard < rgb_headers:
        rgb_headers = 0
    out: list[Channel] = []
    if total_onboard > 0:
        out.append(Channel(0, "onboard", "onboard", 0, total_onboard, rgb_headers))
    for i in range(addressable):
        n = len(out)
        out.append(Channel(n, f"argb-{i + 1}", "argb", n, 1, 0))
    return out


def _top_usage(descriptor: bytes) -> tuple[int, int] | None:
    """The first Usage Page and the first Usage before the descriptor's
    first Collection item: the top-level usage of its first collection.

    Not exactly hidapi's enumeration, which keeps the last Usage Page and
    Usage seen before the Collection, and (since 0.13) lists every
    top-level collection. The two differ only when a descriptor repeats
    those items before its first collection or has several collections, and
    then this reports another usage or None: a device it can't classify is
    skipped (a miss), never mistaken for one it matches."""
    page = usage = None
    i = 0
    while i < len(descriptor):
        prefix = descriptor[i]
        if prefix == 0xFE:  # long item: data size in the next byte, then a tag byte
            if i + 1 >= len(descriptor):
                return None
            i += 3 + descriptor[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 0x03]
        value = int.from_bytes(descriptor[i + 1 : i + 1 + size], "little")
        item = prefix & 0xFC
        if item == 0x04 and page is None:
            page = value
        elif item == 0x08 and usage is None:
            usage = value
        elif item == 0xA0:  # Collection
            return None if page is None or usage is None else (page, usage)
        i += 1 + size
    return None


def find_controllers(sysfs: Path = SYSFS_HIDRAW) -> list[HidInfo]:
    """The hidraw nodes of Aura mainboard controllers. Reads sysfs only."""
    out = []
    for info in enumerate_hidraw(sysfs):
        if info.vid != VENDOR_ID or info.pid not in MAINBOARD_PIDS:
            continue
        want = USAGE_FILTER.get(info.pid)
        if want is not None:
            try:
                descriptor = (sysfs / Path(info.devnode).name / "device" / "report_descriptor").read_bytes()
            except OSError:
                continue
            if _top_usage(descriptor) != want:
                continue
        out.append(info)
    return out


class HidrawAccess:
    """How the daemon reaches the controller: sysfs to find it and to notice
    when it goes away, hidraw to open it. ``node_id`` only looks at sysfs and
    stats the device node; it never opens it."""

    def __init__(self, sysfs: Path = SYSFS_HIDRAW, dev: Path = Path("/dev")):
        self.sysfs = sysfs
        self.dev = dev

    def find(self) -> str | None:
        found = find_controllers(self.sysfs)
        return found[0].devnode if found else None

    def node_id(self, devnode: str) -> tuple[str, int, int] | None:
        """(node, st_rdev, st_ino) while sysfs lists the node, else None. A
        re-enumerated device gets a new node, or a new inode on the same one."""
        name = Path(devnode).name
        if not (self.sysfs / name).exists():
            return None
        try:
            st = os.stat(self.dev / name)
        except OSError:
            return None
        return (name, st.st_rdev, st.st_ino)

    def open(self, devnode: str) -> AuraMainboard:
        return AuraMainboard(HidrawTransport(devnode))


class AuraMainboard:
    NAME = "ASUS Aura LED Controller"
    output_size = 65  # report ID + the descriptor's 64-byte output report
    # OpenRGB's hid_read blocks without a limit. Nobody has timed this board's
    # replies; 0.5 s is generous for a USB HID round trip (the Lian Li hub's
    # slowest measured reply is 165 ms) and keeps a silent controller from
    # holding the daemon up for long: one attempt is two requests, and
    # attempts come at most every RECONNECT_EVERY.
    reply_timeout = 0.5

    def __init__(self, transport: AuraTransport, clock: Callable[[], float] = time.monotonic):
        self.t = transport
        self._clock = clock
        self.lock = threading.RLock()
        self.config_table = b""
        self.channels: list[Channel] = []

    def _write(self, *payload: int) -> None:
        self.t.write(bytes([REPORT_ID, *payload]).ljust(self.output_size, b"\0"))

    def _request(self, cmd: int, reply_id: int) -> bytes:
        """Send a query and read input reports until its reply comes back
        (anything else, such as a reply to an earlier query, is skipped)."""
        with self.lock:
            self._write(cmd)
            deadline = self._clock() + self.reply_timeout
            while True:
                left = deadline - self._clock()
                if left <= 0:
                    raise TimeoutError(f"no reply to Aura request {cmd:#04x}")
                data = self.t.read(self.output_size, left)
                if len(data) >= 2 and data[0] == REPORT_ID and data[1] == reply_id:
                    return bytes(data).ljust(self.output_size, b"\0")

    # --- queries (read-only) ---------------------------------------------------
    def read_firmware(self) -> str:
        d = self._request(REQUEST_FIRMWARE_VERSION, REPLY_FIRMWARE_VERSION)
        return d[2:18].split(b"\0", 1)[0].decode("ascii", "replace")

    def read_config(self) -> list[Channel]:
        """Read the config table and decode it into ``channels``."""
        d = self._request(REQUEST_CONFIG_TABLE, REPLY_CONFIG_TABLE)
        self.config_table = d[4:64]
        self.channels = decode_config(self.config_table)
        return self.channels

    def fits(self, index: int) -> bool:
        """Whether a channel's effect colour fits one report. OpenRGB writes
        it at byte 5 + 3 * (LEDs of the channels before it) without checking,
        so an oversized (or garbage) table would overrun its buffer."""
        ch = self._channel(index)
        start = sum(c.num_leds for c in self.channels[:index])
        return 5 + 3 * (start + ch.num_leds) <= self.output_size

    # --- commands -------------------------------------------------------------
    def set_gen1(self) -> None:
        """The packet OpenRGB's AuraMainboardController sends right after
        reading the config table, before any effect (its SetGen1). OpenRGB
        doesn't say what it does; it isn't the flash commit."""
        self._write(0x52, 0x53, 0x00, 0x01)

    def _channel(self, index: int) -> Channel:
        if not 0 <= index < len(self.channels):
            raise ValueError(f"Aura channel {index} out of range 0..{len(self.channels) - 1}")
        return self.channels[index]

    def set_effect(self, index: int, mode: int, color: Color = (0, 0, 0)) -> None:
        """SetMode: run hardware effect ``mode`` on a channel, with ``color``
        for the effects that take one."""
        ch = self._channel(index)
        if not 0 <= mode < MODE_DIRECT:
            raise ValueError("mode must be 0..254 (direct mode isn't ported)")
        if not self.fits(index):
            raise ValueError(f"Aura channel {ch.key}: its effect colour doesn't fit one report")
        start = sum(c.num_leds for c in self.channels[:index])
        r, g, b = (int(v) & 0xFF for v in color)
        mask = (((1 << ch.num_leds) - 1) << start) & 0xFFFF
        buf = bytearray(self.output_size)
        buf[0:5] = bytes([REPORT_ID, CONTROL_MODE_EFFECT_COLOR, mask >> 8, mask & 0xFF, 0x00])
        buf[5 + 3 * start : 5 + 3 * (start + ch.num_leds)] = bytes([r, g, b]) * ch.num_leds
        with self.lock:
            self._write(CONTROL_MODE_EFFECT, ch.effect_channel, 0x00, 0x00, mode)  # 0x00: not the shutdown effect
            self.t.write(bytes(buf))

    def close(self) -> None:
        self.t.close()

    @staticmethod
    def lighting_modes() -> list[dict]:
        return [{"name": n, "max_colors": 1 if n in COLOR_MODES else 0} for n in MODES]
