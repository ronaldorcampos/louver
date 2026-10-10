from __future__ import annotations

from louver.aura import AuraMainboard


def Z(data) -> bytes:
    """A feature report with its zero padding removed (tests use this on
    both sides, so a meaningful trailing 0 is dropped consistently)."""
    return bytes(data).rstrip(b"\0")


class FakeTransport:
    """Records every report; answers input reads from a scripted map keyed by
    the last feature report's command bytes."""

    def __init__(self, responses: dict[bytes, bytes] | None = None, fail: bool = False):
        self.features: list[bytes] = []
        self.outputs: list[bytes] = []
        self.log: list[tuple[str, bytes]] = []
        self.responses = responses or {}
        self.fail = fail
        self.closed = False
        self._last = b""

    def send_feature(self, data: bytes) -> None:
        if self.fail:
            raise OSError(5, "EIO")
        # Devices pad reports to descriptor size; compare without the padding.
        self.features.append(Z(data))
        self.log.append(("F", Z(data)))
        self._last = bytes(data[1:3])

    def write(self, data: bytes) -> None:
        if self.fail:
            raise OSError(5, "EIO")
        self.outputs.append(bytes(data))
        self.log.append(("O", bytes(data)))

    def get_input(self, report_id: int, length: int) -> bytes:
        if self.fail:
            raise OSError(5, "EIO")
        return self.responses.get(self._last, b"").ljust(length, b"\0")

    def close(self) -> None:
        self.closed = True


HUB_RESPONSES = {
    bytes([0x50, 0x01]): bytes([0xE0, 0x50, 0x80, 0xC6, 0x12]),
    # 1 leading byte then 4 BE u16: 1200, 800, 0, 1950
    bytes([0x50, 0x00]): bytes([0xF0, 0x04, 0xB0, 0x03, 0x20, 0x00, 0x00, 0x07, 0x9E]),
}

STRIMER_RESPONSES = {
    bytes([0x50, 0x00]): bytes([0xE0, 0x52, 0xFF, 0x40, 0x05]),
    bytes([0x50, 0x01]): bytes([0x04]),
}


class FakeSensors:
    def __init__(self, values: dict[str, float | None]):
        self.values = values

    def read(self, sid: str):
        return self.values.get(sid)

    def missing(self, sid: str):
        parts = sid[4:].split(",") if sid.startswith("max:") else [sid]
        return [p for p in parts if self.values.get(p) is None]

    def list(self):
        return [{"id": k, "label": k, "value": v} for k, v in self.values.items()]


def aura_table(argb: int = 3, onboard: int = 5, rgb_headers: int = 1) -> bytes:
    """A 60-byte Aura config table: the three bytes AuraMainboardController reads."""
    t = bytearray(60)
    t[0x02], t[0x1B], t[0x1D] = argb, onboard, rgb_headers
    return bytes(t)


class FakeAuraTransport:
    """Records every output report (all 65 bytes); answers the firmware and
    config-table queries from the interrupt endpoint, as the controller does.
    A read with nothing queued advances ``clock`` by its timeout."""

    def __init__(self, firmware: bytes = b"AULA3-AR32-0207", table: bytes | None = None, fail: bool = False):
        self.firmware = firmware
        self.table = aura_table() if table is None else table
        self.fail = fail
        self.silent: set[int] = set()  # requests left unanswered
        self.fail_cmds: set[int] = set()  # commands whose write fails with EIO
        self.writes: list[bytes] = []
        self.queue: list[bytes] = []
        self.clock = 0.0
        self.closed = False

    def write(self, data: bytes) -> None:
        if self.fail or data[1] in self.fail_cmds:
            raise OSError(5, "EIO")
        assert len(data) == 65, len(data)
        self.writes.append(bytes(data))
        if data[1] in self.silent:
            return
        if data[1] == 0x82:
            self.queue.append(bytes([0xEC, 0x02]) + self.firmware)
        elif data[1] == 0xB0:
            self.queue.append(bytes([0xEC, 0x30, 0x00, 0x00]) + self.table)

    def read(self, length: int, timeout: float) -> bytes:
        if self.fail:
            raise OSError(5, "EIO")
        if self.queue:
            return self.queue.pop(0).ljust(65, b"\0")[:length]
        self.clock += timeout
        return b""

    def close(self) -> None:
        self.closed = True


class FakeAuraAccess:
    """The daemon's way to the Aura controller, in memory: a node that's
    there or not, whose identity changes when it re-enumerates. ``transport``
    makes the transport each open gets."""

    def __init__(self, transport):
        self.transport = transport
        self.present = True
        self.ident = 1
        self.finds = 0
        self.opens = 0

    def find(self):
        self.finds += 1
        return "/dev/hidraw-fake" if self.present else None

    def node_id(self, devnode):
        return self.ident if self.present else None

    def open(self, devnode):
        self.opens += 1
        t = self.transport()
        return AuraMainboard(t, clock=lambda: t.clock)


class FakeI2CKernel:
    """i2c-dev as louver.corsair_dram.I2CDev's ioctls see it: a fake fd, the
    claimed slave address and simulated sticks behind it ({addr: stick}).
    ``calls`` records every ioctl request; nothing here touches a real node."""

    FD = 4321

    def __init__(self, sticks=None, funcs=None, busy=()):
        from louver import corsair_dram as cd

        self.cd = cd
        self.sticks = sticks if sticks is not None else {}
        self.funcs = cd.NEEDED_FUNCS | 0x00010000 if funcs is None else funcs
        self.busy = set(busy)  # addresses a kernel driver holds: I2C_SLAVE gets EBUSY
        self.calls: list[int] = []
        self.transfers: list[tuple] = []  # (addr, "r"/"w"/"block", reg, value or payload)
        self.opened: list[tuple[str, int]] = []
        self.closed: list[int] = []
        self.addr = None

    def open(self, path, flags):
        self.opened.append((path, flags))
        return self.FD

    def close(self, fd):
        self.closed.append(fd)

    def ioctl(self, fd, request, arg):
        import errno

        cd = self.cd
        assert fd == self.FD
        self.calls.append(request)
        if request == cd.I2C_FUNCS:
            arg.value = self.funcs
            return 0
        if request == cd.I2C_SLAVE:
            assert isinstance(arg, int)
            if arg in self.busy:
                raise OSError(errno.EBUSY, "Device or resource busy")
            self.addr = arg
            return 0
        assert request == cd.I2C_SMBUS, hex(request)
        assert isinstance(arg, cd.SMBusIoctlData)
        d = arg.data.contents
        stick = self.sticks.get(self.addr)
        if stick is None:
            raise OSError(errno.ENXIO, "No such device or address")
        if (arg.read_write, arg.size) == (cd.I2C_SMBUS_READ, cd.I2C_SMBUS_BYTE_DATA):
            d.byte = stick.read(arg.command)
            self.transfers.append((self.addr, "r", arg.command, d.byte))
        elif (arg.read_write, arg.size) == (cd.I2C_SMBUS_WRITE, cd.I2C_SMBUS_BYTE_DATA):
            stick.write(arg.command, d.byte)
            self.transfers.append((self.addr, "w", arg.command, d.byte))
        elif (arg.read_write, arg.size) == (cd.I2C_SMBUS_WRITE, cd.I2C_SMBUS_BLOCK_DATA):
            payload = bytes(d.block[1 : 1 + d.block[0]])
            stick.write_block(arg.command, payload)
            self.transfers.append((self.addr, "block", arg.command, payload))
        else:
            raise AssertionError(f"unexpected transfer {arg.read_write}/{arg.size}")
        return 0

    def dev(self, devnode="/nonexistent/i2c-3"):
        from louver.corsair_dram import I2CDev

        return I2CDev(devnode, ioctl=self.ioctl, open_=self.open, close_=self.close)

    def writes(self):
        return [t for t in self.transfers if t[1] != "r"]


def ram_listing(*sticks, error="", skipped=(), retry=False, none=False):
    """A helper's list reply as RamClient.list returns it. Each stick: (addr,
    leds); two 10-LED sticks unless given, none at all with ``none``."""
    from louver.ramrgb import Listing, RamStick

    sticks = () if none else sticks or ((0x19, 10), (0x1B, 10))
    return Listing(
        "SMBus I801 adapter at 0000:00:1f.4",
        "/dev/i2c-3",
        [RamStick(a, "Corsair Vengeance RGB DDR5", 0x0700, n, "1.3.300", 4) for a, n in sticks],
        list(skipped),
        error,
        retry,
    )


class FakeRamClient:
    def __init__(self, owner):
        self.owner = owner
        self.lists: list[bool] = []
        self.list_timeouts: list[float | None] = []
        self.sets: list[tuple[int, list]] = []
        self.closed = False

    def list(self, rescan=False, timeout=None):
        self.lists.append(rescan)
        self.list_timeouts.append(timeout)
        if self.owner.list_error is not None:
            raise self.owner.list_error
        return self.owner.listing

    def set(self, addr, colors):
        if self.owner.set_errors:
            raise self.owner.set_errors.pop(0)
        self.sets.append((addr, list(colors)))

    def close(self):
        self.closed = True


class FakeRamAccess:
    """The daemon's way to the louver-ram helper, in memory. Each connect
    makes a FakeRamClient; ``set_errors`` are raised by the next sets, in order."""

    path = "/nonexistent/louver-ram.sock"

    def __init__(self):
        self.connects = 0
        self.clients: list[FakeRamClient] = []
        self.connect_error: OSError | None = None
        self.listing = ram_listing()
        self.list_error: Exception | None = None
        self.set_errors: list[Exception] = []

    def connect(self):
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        self.clients.append(FakeRamClient(self))
        return self.clients[-1]

    def sets(self):
        return [s for c in self.clients for s in c.sets]
