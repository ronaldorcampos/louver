"""Driver for the RGB controller on Corsair DDR5 memory sticks, reached
over the Intel PCH SMBus (i2c-i801) through Linux i2c-dev.

Copyright (C) 2026 Ronaldo Campos (this port). Ported to Python and
modified for Louver, 2026-10-10.
SPDX-License-Identifier: GPL-2.0-or-later

A port to Python of OpenRGB's Corsair DRAM driver, at commit
b190d3e5afb6bd68b4bfa3e948947878d2bf1ba9 of
https://gitlab.com/CalcProgrammer1/OpenRGB, GPL-2.0-or-later. Ported files,
under Controllers/CorsairDRAMController/:

  CorsairDRAMController.{h,cpp}        (Adam Honse, Erik Gilling)
      the register map, the device-info read and the direct colour packet
  CorsairDRAMControllerDetect.cpp      (Adam Honse, Erik Gilling)
      the 0x43/0x44 identification test and the 10 ms between addresses
  CorsairDRAMDevices.{h,cpp}           (Adam Honse)
      the vendor ID and the product IDs, LED counts and LED order
  RGBController_CorsairDRAM.cpp        (Adam Honse, Erik Gilling)
      direct mode only on protocol 4 and later

Changes from OpenRGB:
- DDR5 only: addresses 0x18-0x1F, and only the DDR5 entries of the device
  table, and only once the firmware's memory table (SMBIOS type 17) says
  every installed module is DDR5 (require_ddr5). OpenRGB also probes
  0x58-0x5F, where DDR4 boards have other devices, and knows the DDR4 sticks.
- A stick is driven only once its device info checks out: the checksum
  matches, the vendor is Corsair, the product ID is in the table and the
  protocol has direct mode. OpenRGB logs a checksum mismatch and carries on,
  and gives a product ID it doesn't know the first table entry's LED count.
- Every bus access goes through SafeBus, which refuses any register,
  address or length outside the few this driver uses, and any write to an
  address that hasn't identified itself (see SafeBus).
- A direct packet shorter than 32 bytes (a 6-LED stick) is padded with
  zeros to the 32-byte first block; OpenRGB sends 32 bytes from the shorter
  buffer, whatever follows it in memory.
- A failed block write raises; nothing falls back to word writes.

Not ported, on purpose:
- SetEffect and the hardware effects: they write the effect configuration
  with register 0x82, and OpenRGB marks those modes MODE_FLAG_AUTOMATIC_SAVE:
  they are stored on the stick. Louver never stores anything on it.
- The non-direct colour path (registers 0x0B, 0x20 and the 0x82 apply),
  which writes the stick's colour configuration the same way.
- The word-write fallback for buses without block writes (registers
  0x90-0xAF): a bus without SMBus block writes is refused instead.
- Register 0x23, which switches the controller to its bootloader.
- Register 0x30 (status) polling, which only follows a 0x82 write.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

OPENRGB_COMMIT = "b190d3e5afb6bd68b4bfa3e948947878d2bf1ba9"

VENDOR_ID = 0x1B1C
ADDRESSES = tuple(range(0x18, 0x20))  # DDR5 only
DETECT_DELAY = 0.01  # OpenRGB sleeps 10 ms between addresses

REG_BINARY_START = 0x21  # start a binary data transfer by writing 0x00
REG_COLOR_BLOCK_1 = 0x31  # direct colour buffer, first 32 bytes
REG_COLOR_BLOCK_2 = 0x32  # direct colour buffer, the rest
REG_GET_BINARY_DATA = 0x40  # one byte of the selected buffer
REG_GET_CHECKSUM = 0x42  # CRC-8 of the selected buffer
REG_ID_1 = 0x43  # identification bytes TestForCorsairDRAMController reads
REG_ID_2 = 0x44
REG_GET_DEVICE_INFO = 0x61  # select the device-info buffer by writing 0x00
ID_1_VALUES = frozenset({0x1A, 0x1B, 0x1C})
ID_2_VALUES = frozenset({0x01, 0x03, 0x04})
DEVICE_INFO_SIZE = 32
DIRECT_MIN_PROTOCOL = 4
BLOCK_MAX = 32  # I2C_SMBUS_BLOCK_MAX

# The whole allowlist SafeBus enforces.
READ_REGS = frozenset({REG_GET_BINARY_DATA, REG_GET_CHECKSUM, REG_ID_1, REG_ID_2})
BYTE_WRITE_REGS = frozenset({REG_BINARY_START, REG_GET_DEVICE_INFO})
BLOCK_WRITE_REGS = frozenset({REG_COLOR_BLOCK_1, REG_COLOR_BLOCK_2})

Color = tuple[int, int, int]


@dataclass(frozen=True)
class DeviceType:
    """One entry of OpenRGB's corsair_dram_device_list."""

    name: str
    pids: tuple[int, ...]
    leds: int
    reverse: bool  # the packet carries the LEDs last to first


# CorsairDRAMDevices.cpp, the DDR5 entries.
DEVICES: tuple[DeviceType, ...] = (
    DeviceType("Corsair Dominator Platinum RGB DDR5", (0x0600, 0x0601), 12, True),
    DeviceType("Corsair Dominator Titanium RGB DDR5", (0x0800, 0x0801, 0x0810, 0x0811), 12, True),
    DeviceType("Corsair Vengeance RGB DDR5", (0x0700, 0x0701, 0x0900, 0x0901, 0x0910, 0x0911), 10, False),
    DeviceType("Corsair Vengeance Shugo Series DDR5", (0x0A00, 0x0A01, 0x0A10, 0x0A11), 10, False),
    DeviceType("Corsair Vengeance RGB RS DDR5", (0x0B00, 0x0B01), 6, False),
)


def device_for(pid: int) -> DeviceType | None:
    return next((d for d in DEVICES if pid in d.pids), None)


def crc8(data: bytes | Sequence[int]) -> int:
    """CRCpp's CRC_8 (CRC-8/SMBus): polynomial 0x07, initial value 0, no
    reflection, no final XOR."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


@dataclass(frozen=True)
class DeviceInfo:
    vid: int
    pid: int
    firmware: str
    protocol: int


def parse_device_info(d: bytes) -> DeviceInfo:
    """CorsairDRAMController::ReadDeviceInfo's decoding of the 32 bytes."""
    if len(d) != DEVICE_INFO_SIZE:
        raise ValueError(f"device info is {DEVICE_INFO_SIZE} bytes, got {len(d)}")
    return DeviceInfo(
        vid=d[1] << 8 | d[0],
        pid=d[3] << 8 | d[2],
        firmware=f"{d[9]}.{d[8]}.{d[11] << 8 | d[10]}",
        protocol=d[28],
    )


def direct_packet(device: DeviceType, colors: Sequence[Color]) -> bytes:
    """SetColorsPerLED's direct packet: the LED count, R, G, B per LED (last
    LED first on a reversed stick), then the CRC-8 of all of it."""
    if len(colors) != device.leds:
        raise ValueError(f"{device.name} has {device.leds} LEDs, got {len(colors)} colours")
    order = reversed(colors) if device.reverse else colors
    body = bytearray([device.leds])
    for c in order:
        if len(c) != 3 or not all(_is_int(v) and 0 <= v <= 255 for v in c):
            raise ValueError(f"bad colour {c!r}")
        body += bytes(c)
    return bytes(body) + bytes([crc8(body)])


def direct_blocks(packet: bytes) -> list[tuple[int, bytes]]:
    """The packet as block writes: its first 32 bytes to 0x31 (zero-padded
    when shorter), the rest, if any, to 0x32."""
    out = [(REG_COLOR_BLOCK_1, packet[:BLOCK_MAX].ljust(BLOCK_MAX, b"\0"))]
    if len(packet) > BLOCK_MAX:
        out.append((REG_COLOR_BLOCK_2, packet[BLOCK_MAX:]))
    return out


# --- Linux i2c-dev ------------------------------------------------------------
# <linux/i2c-dev.h> and <linux/i2c.h>
I2C_SLAVE = 0x0703
I2C_FUNCS = 0x0705
I2C_SMBUS = 0x0720
I2C_SMBUS_WRITE = 0
I2C_SMBUS_READ = 1
I2C_SMBUS_BYTE_DATA = 2
I2C_SMBUS_BLOCK_DATA = 5
I2C_FUNC_SMBUS_READ_BYTE_DATA = 0x00080000
I2C_FUNC_SMBUS_WRITE_BYTE_DATA = 0x00100000
I2C_FUNC_SMBUS_WRITE_BLOCK_DATA = 0x02000000
NEEDED_FUNCS = I2C_FUNC_SMBUS_READ_BYTE_DATA | I2C_FUNC_SMBUS_WRITE_BYTE_DATA | I2C_FUNC_SMBUS_WRITE_BLOCK_DATA


class SMBusData(ctypes.Union):
    """union i2c_smbus_data"""

    _fields_ = [("byte", ctypes.c_uint8), ("word", ctypes.c_uint16), ("block", ctypes.c_uint8 * (BLOCK_MAX + 2))]


class SMBusIoctlData(ctypes.Structure):
    """struct i2c_smbus_ioctl_data"""

    _fields_ = [
        ("read_write", ctypes.c_uint8),
        ("command", ctypes.c_uint8),
        ("size", ctypes.c_uint32),
        ("data", ctypes.POINTER(SMBusData)),
    ]


class RawSMBus(Protocol):
    """SMBus transfers as i2c-dev offers them. Only SafeBus calls these."""

    def read_byte_data(self, addr: int, reg: int) -> int: ...

    def write_byte_data(self, addr: int, reg: int, value: int) -> None: ...

    def write_block_data(self, addr: int, reg: int, data: bytes) -> None: ...

    def close(self) -> None: ...


class I2CDev:
    """One /dev/i2c-N node. Addresses are claimed with I2C_SLAVE, never
    I2C_SLAVE_FORCE, so one a kernel driver holds fails with EBUSY.
    ``ioctl``, ``open_`` and ``close_`` are injectable for tests."""

    def __init__(
        self,
        devnode: str,
        *,
        ioctl: Callable = fcntl.ioctl,
        open_: Callable[[str, int], int] = os.open,
        close_: Callable[[int], None] = os.close,
    ):
        self.devnode = devnode
        self._ioctl = ioctl
        self._close = close_
        self._addr: int | None = None
        self._fd: int | None = open_(devnode, os.O_RDWR | os.O_CLOEXEC)
        try:
            funcs = ctypes.c_ulong(0)
            self._ioctl(self._fd, I2C_FUNCS, funcs)
            missing = NEEDED_FUNCS & ~funcs.value
            if missing:
                raise OSError(
                    errno.EOPNOTSUPP, f"{devnode} lacks SMBus byte-data or block-write support (funcs {funcs.value:#x})"
                )
        except BaseException:
            self.close()
            raise

    def _use(self, addr: int) -> int:
        if self._fd is None:
            raise OSError(errno.EBADF, f"{self.devnode} is closed")
        if addr != self._addr:
            self._addr = None
            try:
                self._ioctl(self._fd, I2C_SLAVE, addr)
            except OSError as e:
                if e.errno == errno.EBUSY:
                    raise AddressInUse(errno.EBUSY, f"{addr:#04x} is in use by a kernel driver") from None
                raise
            self._addr = addr
        return self._fd

    def _smbus(self, addr: int, read_write: int, reg: int, size: int, data: SMBusData) -> None:
        fd = self._use(addr)
        args = SMBusIoctlData(read_write, reg, size, ctypes.pointer(data))
        self._ioctl(fd, I2C_SMBUS, args)

    def read_byte_data(self, addr: int, reg: int) -> int:
        data = SMBusData()
        self._smbus(addr, I2C_SMBUS_READ, reg, I2C_SMBUS_BYTE_DATA, data)
        return data.byte

    def write_byte_data(self, addr: int, reg: int, value: int) -> None:
        data = SMBusData()
        data.byte = value
        self._smbus(addr, I2C_SMBUS_WRITE, reg, I2C_SMBUS_BYTE_DATA, data)

    def write_block_data(self, addr: int, reg: int, payload: bytes) -> None:
        data = SMBusData()
        data.block[0] = len(payload)
        for i, b in enumerate(payload):
            data.block[1 + i] = b
        self._smbus(addr, I2C_SMBUS_WRITE, reg, I2C_SMBUS_BLOCK_DATA, data)

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            self._close(fd)


class AddressInUse(OSError):
    """I2C_SLAVE refused the address: a kernel driver holds it. Not retried,
    unlike EBUSY from a transfer (the bus was busy for a moment)."""


# --- the allowlist ---------------------------------------------------------------
def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class Refused(ValueError):
    """A bus access outside the allowlist. Raised before any I/O."""


class NotIdentified(Exception):
    """The address answered, but isn't a stick Louver drives (the reason)."""

    retry = False  # what it answered won't change on a rescan


class GarbledInfo(NotIdentified):
    """The device info failed its checksum: a corrupted transfer, not an
    answer, so a rescan may well identify the stick."""

    retry = True


@dataclass(frozen=True)
class Stick:
    addr: int
    device: DeviceType
    info: DeviceInfo

    def describe(self) -> dict:
        return {
            "addr": self.addr,
            "name": self.device.name,
            "pid": self.info.pid,
            "leds": self.device.leds,
            "firmware": self.info.firmware,
            "protocol": self.info.protocol,
        }


class SafeBus:
    """The only way this driver reaches the SMBus. It allows:

    - reads of registers 0x40, 0x42, 0x43, 0x44 at 0x18-0x1F;
    - byte writes of 0x00 to registers 0x21 and 0x61 (select the device-info
      buffer and start reading it), only at an address whose 0x43/0x44 bytes
      matched a Corsair controller;
    - block writes of 1-32 bytes to registers 0x31 and 0x32 (the direct
      colour buffer), only at an address that identify() accepted.

    Anything else raises Refused before the raw bus is called. The
    identification state lives here, set only by probe() and identify()
    from what the bus itself answered, so a caller can't skip it."""

    def __init__(self, raw: RawSMBus):
        self._raw = raw
        self._probed: set[int] = set()
        self._identified: dict[int, Stick] = {}

    @staticmethod
    def _check(addr, reg, allowed: frozenset[int], what: str) -> None:
        if not _is_int(addr) or addr not in ADDRESSES:
            raise Refused(f"address {addr!r} is outside 0x18-0x1F")
        if not _is_int(reg) or reg not in allowed:
            raise Refused(f"{what} of register {reg!r} refused")

    def read(self, addr: int, reg: int) -> int:
        self._check(addr, reg, READ_REGS, "read")
        return self._raw.read_byte_data(addr, reg)

    def write_byte(self, addr: int, reg: int, value: int) -> None:
        self._check(addr, reg, BYTE_WRITE_REGS, "byte write")
        if not _is_int(value) or value != 0:
            raise Refused(f"byte write of {value!r} refused: only 0x00 is ever written")
        if addr not in self._probed:
            raise Refused(f"byte write to {addr:#04x} refused: it isn't a Corsair controller")
        self._raw.write_byte_data(addr, reg, 0)

    def write_block(self, addr: int, reg: int, data: bytes) -> None:
        self._check(addr, reg, BLOCK_WRITE_REGS, "block write")
        if not isinstance(data, (bytes, bytearray)):
            raise Refused("block write refused: the data must be bytes")
        if not 1 <= len(data) <= BLOCK_MAX:
            raise Refused(f"block write of {len(data)} bytes refused (1-{BLOCK_MAX})")
        if addr not in self._identified:
            raise Refused(f"block write to {addr:#04x} refused: no identified stick there")
        self._raw.write_block_data(addr, reg, bytes(data))

    def probe(self, addr: int) -> tuple[int, int]:
        """TestForCorsairDRAMController: the 0x43 and 0x44 bytes, when they
        match a Corsair controller; NotIdentified when they don't. Nothing
        answering raises OSError."""
        self._probed.discard(addr)
        self._identified.pop(addr, None)
        id1 = self.read(addr, REG_ID_1)
        if id1 not in ID_1_VALUES:
            raise NotIdentified(f"not a Corsair RGB controller (register 0x43 = {id1:#04x})")
        id2 = self.read(addr, REG_ID_2)
        if id2 not in ID_2_VALUES:
            raise NotIdentified(f"not a Corsair RGB controller (register 0x44 = {id2:#04x})")
        self._probed.add(addr)
        return id1, id2

    def identify(self, addr: int) -> Stick:
        """ReadDeviceInfo, then every check before a stick may be written."""
        self._identified.pop(addr, None)
        if addr not in self._probed:
            raise Refused(f"{addr:#04x} hasn't passed the 0x43/0x44 check")
        self.write_byte(addr, REG_GET_DEVICE_INFO, 0)
        self.write_byte(addr, REG_BINARY_START, 0)
        data = bytes(self.read(addr, REG_GET_BINARY_DATA) for _ in range(DEVICE_INFO_SIZE))
        checksum = self.read(addr, REG_GET_CHECKSUM)
        if crc8(data) != checksum:
            raise GarbledInfo(f"device info checksum mismatch (read {checksum:#04x}, computed {crc8(data):#04x})")
        info = parse_device_info(data)
        if info.vid != VENDOR_ID:
            raise NotIdentified(f"vendor {info.vid:#06x} isn't Corsair")
        device = device_for(info.pid)
        if device is None:
            raise NotIdentified(f"unknown Corsair DDR5 product {info.pid:#06x}: its LED count isn't known")
        if info.protocol < DIRECT_MIN_PROTOCOL:
            raise NotIdentified(f"protocol {info.protocol} has no direct mode (needs {DIRECT_MIN_PROTOCOL} or later)")
        stick = Stick(addr, device, info)
        self._identified[addr] = stick
        return stick

    def close(self) -> None:
        self._probed.clear()
        self._identified.clear()
        self._raw.close()


def set_colors(bus: SafeBus, stick: Stick, colors: Sequence[Color]) -> None:
    """Write one direct frame. The second block goes only after the first
    succeeded (a failure raises)."""
    for reg, block in direct_blocks(direct_packet(stick.device, colors)):
        bus.write_block(stick.addr, reg, block)


# Errors an empty address gives: i801 NACKs with ENXIO; other adapters use EREMOTEIO.
_ABSENT = frozenset({errno.ENXIO, errno.EREMOTEIO})


def scan(bus: SafeBus, sleep: Callable[[float], None] = time.sleep) -> tuple[list[Stick], list[dict]]:
    """DetectCorsairDRAMControllers over 0x18-0x1F: the sticks Louver can
    drive, and every address that answered but isn't one, with the reason
    and whether scanning again might change it (``retry``: an I/O error, a
    busy bus or a garbled transfer, not an answer). Addresses where nothing answers aren't listed."""
    sticks: list[Stick] = []
    skipped: list[dict] = []
    for n, addr in enumerate(ADDRESSES):
        if n:
            sleep(DETECT_DELAY)
        try:
            bus.probe(addr)
            sticks.append(bus.identify(addr))
        except NotIdentified as e:
            skipped.append({"addr": addr, "reason": str(e), "retry": e.retry})
        except AddressInUse:
            skipped.append({"addr": addr, "reason": "in use by a kernel driver", "retry": False})
        except OSError as e:
            if e.errno in _ABSENT:
                continue
            why = "the bus was busy" if e.errno == errno.EBUSY else f"I/O error: {e.strerror or e}"
            skipped.append({"addr": addr, "reason": why, "retry": True})
    return sticks, skipped


# --- is it DDR5 ---------------------------------------------------------------------
SYSFS_DMI = Path("/sys/firmware/dmi/entries")
DMI_MEMORY_DEVICE = 17
# SMBIOS 3.x, type 17 (Memory Device): Size is the WORD at 0x0C (0 = no module
# in the socket), Memory Type the BYTE at 0x12.
DMI_SIZE_OFFSET = 0x0C
DMI_TYPE_OFFSET = 0x12
DMI_DDR5 = 0x22
DMI_TYPE_NAMES = {0x12: "DDR", 0x13: "DDR2", 0x18: "DDR3", 0x1A: "DDR4", 0x1E: "LPDDR4", 0x22: "DDR5", 0x23: "LPDDR5"}


def require_ddr5(dmi: Path = SYSFS_DMI) -> int:
    """How many DDR5 modules the firmware's SMBIOS type 17 entries list.
    Raises NoBus, before anything touches the bus, unless at least one module
    is installed and every installed one is DDR5: on DDR4 boards 0x18-0x1F
    hold the modules' temperature sensors."""
    try:
        entries = sorted(p for p in dmi.iterdir() if re.fullmatch(rf"{DMI_MEMORY_DEVICE}-[0-9]+", p.name))
    except OSError as e:
        raise NoBus(f"cannot read the firmware's memory table ({dmi}): {e.strerror or e}") from None
    types = []
    for entry in entries:
        try:
            raw = (entry / "raw").read_bytes()
        except OSError as e:
            raise NoBus(f"cannot read {entry / 'raw'}: {e.strerror or e}") from None
        if len(raw) <= DMI_TYPE_OFFSET or raw[0] != DMI_MEMORY_DEVICE or raw[1] <= DMI_TYPE_OFFSET:
            raise NoBus(f"{entry / 'raw'} isn't a memory device entry this code understands")
        if int.from_bytes(raw[DMI_SIZE_OFFSET : DMI_SIZE_OFFSET + 2], "little") == 0:
            continue  # an empty slot
        types.append(raw[DMI_TYPE_OFFSET])
    if not types:
        raise NoBus("the firmware's memory table lists no installed module")
    other = sorted({t for t in types if t != DMI_DDR5})
    if other:
        names = ", ".join(DMI_TYPE_NAMES.get(t, f"type {t:#04x}") for t in other)
        raise NoBus(f"not a DDR5 system: the firmware lists {names} memory")
    return len(types)


# --- which bus ---------------------------------------------------------------------
SYSFS_I2C = Path("/sys/bus/i2c/devices")
INTEL_ADAPTER = "SMBus I801 adapter"


class NoBus(Exception):
    """No single Intel SMBus adapter to use (the reason)."""


def find_intel_bus(sysfs: Path = SYSFS_I2C, dev: Path = Path("/dev")) -> tuple[str, str]:
    """(adapter name, /dev node) of the one Intel PCH SMBus adapter. Reads
    sysfs only; raises NoBus unless exactly one adapter matches by name."""
    try:
        entries = sorted(p for p in sysfs.iterdir() if re.fullmatch(r"i2c-[0-9]+", p.name))
    except OSError as e:
        raise NoBus(f"cannot list {sysfs}: {e.strerror or e}") from None
    found = []
    for entry in entries:
        try:
            name = (entry / "name").read_text().strip()
        except OSError:
            continue
        if name.startswith(INTEL_ADAPTER):
            found.append((entry.name, name))
    if not found:
        raise NoBus("no Intel SMBus adapter (i2c-i801) found")
    if len(found) > 1:
        raise NoBus(f"{len(found)} Intel SMBus adapters ({', '.join(n for n, _ in found)}): not choosing one")
    node, name = found[0]
    if not (dev / node).exists():
        raise NoBus(f"{dev / node} is missing: the i2c-dev module isn't loaded")
    return name, str(dev / node)
