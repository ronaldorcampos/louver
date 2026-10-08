"""Minimal Linux hidraw transport (no hidapi dependency).

Lian Li's ENE-based controllers expose a single vendor collection with
report ID 0xE0: commands go out as *feature* reports, colour data as
*output* reports, and replies (RPM, firmware) come back as *input*
reports fetched with HIDIOCGINPUT.
"""

from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_IOC_WRITE = 1
_IOC_READ = 2


def _ioc(direction: int, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord("H") << 8) | nr


def HIDIOCSFEATURE(size: int) -> int:
    return _ioc(_IOC_WRITE | _IOC_READ, 0x06, size)


def HIDIOCGFEATURE(size: int) -> int:
    return _ioc(_IOC_WRITE | _IOC_READ, 0x07, size)


def HIDIOCGINPUT(size: int) -> int:
    return _ioc(_IOC_WRITE | _IOC_READ, 0x0A, size)


class Transport(Protocol):
    """What a device driver needs from the wire. Faked in tests."""

    def send_feature(self, data: bytes) -> None: ...

    def write(self, data: bytes) -> None: ...

    def get_input(self, report_id: int, length: int) -> bytes: ...

    def close(self) -> None: ...


class HidrawError(OSError):
    pass


class HidrawTransport:
    def __init__(self, devnode: str):
        self.devnode = devnode
        try:
            self._fd = os.open(devnode, os.O_RDWR | os.O_CLOEXEC)
        except PermissionError as e:
            raise HidrawError(
                e.errno,
                f"no permission to open {devnode}; install packaging/99-louver.rules "
                "(see README) or run as root",
            ) from e

    def send_feature(self, data: bytes) -> None:
        buf = bytearray(data)
        fcntl.ioctl(self._fd, HIDIOCSFEATURE(len(buf)), buf, True)

    def write(self, data: bytes) -> None:
        n = os.write(self._fd, bytes(data))
        if n != len(data):
            raise HidrawError(0, f"short write to {self.devnode}: {n}/{len(data)}")

    def get_input(self, report_id: int, length: int) -> bytes:
        """Return the input report payload *after* the report-ID byte."""
        buf = bytearray(length + 1)
        buf[0] = report_id
        n = fcntl.ioctl(self._fd, HIDIOCGINPUT(len(buf)), buf, True)
        return bytes(buf[1:n]) if n > 0 else b""

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


@dataclass(frozen=True)
class HidInfo:
    devnode: str
    vid: int
    pid: int
    name: str
    serial: str
    phys: str


def enumerate_hidraw(sysfs: Path = Path("/sys/class/hidraw")) -> list[HidInfo]:
    out: list[HidInfo] = []
    if not sysfs.exists():
        return out
    for entry in sorted(sysfs.iterdir()):
        try:
            uevent = (entry / "device" / "uevent").read_text()
        except OSError:
            continue
        props = dict(line.split("=", 1) for line in uevent.splitlines() if "=" in line)
        hid_id = props.get("HID_ID", "")
        parts = hid_id.split(":")
        if len(parts) != 3:
            continue
        out.append(
            HidInfo(
                devnode=f"/dev/{entry.name}",
                vid=int(parts[1], 16),
                pid=int(parts[2], 16),
                name=props.get("HID_NAME", ""),
                serial=props.get("HID_UNIQ", ""),
                phys=props.get("HID_PHYS", ""),
            )
        )
    return out
