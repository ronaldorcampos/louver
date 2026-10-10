"""The Corsair DDR5 RGB driver (a port of OpenRGB's): CRC, device table,
frame encoding, the i2c-dev ioctl layer, the allowlist and identification.
All against fakes: the ioctls go to FakeI2CKernel, never a real /dev/i2c-*."""

import ctypes
import errno
import os

import pytest

from louver import corsair_dram as cd
from louver.sim import SimCorsairStick

from .fakes import FakeI2CKernel

GREY = [(i, 2 * i, 3 * i) for i in range(1, 13)]  # 12 distinct colours


def kernel(**sticks):
    """FakeI2CKernel with SimCorsairSticks at the given addresses: k(a0x19=stick)."""
    return FakeI2CKernel({int(k[1:], 16): v for k, v in sticks.items()})


def safe(k):
    return cd.SafeBus(k.dev())


# ---------------- CRC and the device table ----------------
def test_crc8_is_crcpp_crc_8():
    # CRCpp's CRC_8(): poly 0x07, init 0, no reflection, no final XOR; its check value is 0xF4.
    assert cd.crc8(b"123456789") == 0xF4
    assert cd.crc8(b"") == 0x00
    assert cd.crc8(b"\x00") == 0x00 and cd.crc8(b"\x01") == 0x07 and cd.crc8(b"\x80") == 0x89


def test_device_table_is_the_ddr5_entries_only():
    pids = [p for d in cd.DEVICES for p in d.pids]
    assert len(pids) == len(set(pids)) == 18
    assert not [p for p in pids if p >> 8 in (0x01, 0x02, 0x03, 0x04)]  # the DDR4 families
    assert cd.device_for(0x0700).name == "Corsair Vengeance RGB DDR5"
    assert (cd.device_for(0x0911).leds, cd.device_for(0x0911).reverse) == (10, False)
    assert (cd.device_for(0x0601).leds, cd.device_for(0x0601).reverse) == (12, True)  # Dominator Platinum
    assert (cd.device_for(0x0811).leds, cd.device_for(0x0811).reverse) == (12, True)  # Dominator Titanium
    assert cd.device_for(0x0B01).leds == 6 and cd.device_for(0x0A11).leds == 10
    assert cd.device_for(0x0100) is None and cd.device_for(0x0000) is None
    assert cd.ADDRESSES == tuple(range(0x18, 0x20))


def test_parse_device_info_follows_read_device_info():
    d = bytearray(32)
    d[0:4] = bytes([0x1C, 0x1B, 0x01, 0x07])
    d[8:12] = bytes([7, 2, 0x34, 0x12])
    d[28] = 5
    assert cd.parse_device_info(bytes(d)) == cd.DeviceInfo(0x1B1C, 0x0701, "2.7.4660", 5)
    with pytest.raises(ValueError):
        cd.parse_device_info(bytes(31))


# ---------------- frames ----------------
def test_ten_led_frame_is_one_32_byte_block():
    dev = cd.device_for(0x0700)
    packet = cd.direct_packet(dev, GREY[:10])
    assert len(packet) == 32 and packet[0] == 10
    assert packet[1:4] == bytes(GREY[0]) and packet[28:31] == bytes(GREY[9])
    assert packet[31] == cd.crc8(packet[:31])
    assert cd.direct_blocks(packet) == [(0x31, packet)]


def test_twelve_led_frame_is_two_blocks_and_reversed():
    dev = cd.device_for(0x0600)  # Dominator Platinum: reverse
    packet = cd.direct_packet(dev, GREY)
    assert len(packet) == 38 and packet[0] == 12
    assert packet[1:4] == bytes(GREY[11]) and packet[34:37] == bytes(GREY[0])  # last LED first
    assert packet[37] == cd.crc8(packet[:37])
    assert cd.direct_blocks(packet) == [(0x31, packet[:32]), (0x32, packet[32:])]
    assert len(packet[32:]) == 6


def test_six_led_frame_is_padded_to_the_32_byte_block():
    dev = cd.device_for(0x0B00)
    packet = cd.direct_packet(dev, GREY[:6])
    assert len(packet) == 20 and packet[19] == cd.crc8(packet[:19])
    assert cd.direct_blocks(packet) == [(0x31, packet + bytes(12))]


def test_frame_refuses_a_wrong_count_or_bad_colour():
    dev = cd.device_for(0x0700)
    with pytest.raises(ValueError, match="10 LEDs"):
        cd.direct_packet(dev, GREY[:12])
    for bad in ((256, 0, 0), (-1, 0, 0), (1, 2), (True, 0, 0), (1.0, 0, 0)):
        with pytest.raises(ValueError):
            cd.direct_packet(dev, [bad] + GREY[:9])


# ---------------- the i2c-dev layer ----------------
def test_ioctl_structs_match_the_kernel_uapi():
    if ctypes.sizeof(ctypes.c_void_p) == 8:
        assert ctypes.sizeof(cd.SMBusIoctlData) == 16
    assert [cd.SMBusIoctlData.read_write.offset, cd.SMBusIoctlData.command.offset, cd.SMBusIoctlData.size.offset] == [
        0,
        1,
        4,
    ]
    assert cd.SMBusIoctlData.data.offset == ctypes.sizeof(ctypes.c_void_p)
    assert ctypes.sizeof(cd.SMBusData) == 34
    assert (cd.I2C_SLAVE, cd.I2C_FUNCS, cd.I2C_SMBUS) == (0x0703, 0x0705, 0x0720)


def test_i2cdev_checks_funcs_then_marshals_transfers():
    stick = SimCorsairStick()
    k = kernel(a0x19=stick)
    dev = k.dev("/nonexistent/i2c-3")
    assert k.opened == [("/nonexistent/i2c-3", os.O_RDWR | os.O_CLOEXEC)] and k.calls == [cd.I2C_FUNCS]
    assert dev.read_byte_data(0x19, 0x43) == 0x1B
    assert dev.read_byte_data(0x19, 0x44) == 0x04
    dev.write_byte_data(0x19, 0x61, 0)
    dev.write_block_data(0x19, 0x31, bytes(range(32)))
    # One I2C_SLAVE for the address, then the transfers.
    assert k.calls == [cd.I2C_FUNCS, cd.I2C_SLAVE] + [cd.I2C_SMBUS] * 4
    assert k.transfers == [
        (0x19, "r", 0x43, 0x1B),
        (0x19, "r", 0x44, 0x04),
        (0x19, "w", 0x61, 0),
        (0x19, "block", 0x31, bytes(range(32))),
    ]
    assert stick.blocks == [(0x31, bytes(range(32)))]
    dev.close()
    dev.close()
    assert k.closed == [k.FD]
    with pytest.raises(OSError):
        dev.read_byte_data(0x19, 0x43)


def test_i2cdev_never_forces_an_address_and_refuses_a_bus_without_block_writes():
    k = kernel(a0x19=SimCorsairStick())
    k.busy.add(0x1A)
    dev = k.dev()
    with pytest.raises(cd.AddressInUse) as e:
        dev.read_byte_data(0x1A, 0x43)
    assert e.value.errno == errno.EBUSY and "in use by a kernel driver" in str(e.value)
    assert dev.read_byte_data(0x19, 0x43) == 0x1B
    with pytest.raises(OSError):
        dev.read_byte_data(0x1A, 0x43)  # claimed again (and refused again), not assumed held
    assert k.calls == [cd.I2C_FUNCS, cd.I2C_SLAVE, cd.I2C_SLAVE, cd.I2C_SMBUS, cd.I2C_SLAVE]
    assert 0x0706 not in k.calls  # I2C_SLAVE_FORCE
    k = FakeI2CKernel(funcs=cd.I2C_FUNC_SMBUS_READ_BYTE_DATA | cd.I2C_FUNC_SMBUS_WRITE_BYTE_DATA)
    with pytest.raises(OSError) as e:
        k.dev()
    assert e.value.errno == errno.EOPNOTSUPP and k.closed == [k.FD] and k.calls == [cd.I2C_FUNCS]


# ---------------- the allowlist ----------------
def _identified(k=None, addr=0x19):
    k = k or kernel(a0x19=SimCorsairStick())
    bus = safe(k)
    bus.probe(addr)
    bus.identify(addr)
    return bus, k


def test_disallowed_reads_are_refused_before_any_ioctl():
    bus, k = _identified()
    before = list(k.calls)
    for addr, reg in (
        (0x19, 0x41),  # busy status
        (0x19, 0x30),  # status
        (0x19, 0x63),
        (0x19, 0x82),
        (0x19, 0x23),
        (0x19, 0x00),
        (0x19, 67.0),  # 0x43 as a float
        (0x19, True),
        (0x50, 0x43),  # the SPD hub's range
        (0x17, 0x43),
        (0x20, 0x43),
        (0x58, 0x43),  # DDR4 Corsair addresses: not used
        (True, 0x43),
        (25.0, 0x43),
        ("0x19", 0x43),
    ):
        with pytest.raises(cd.Refused):
            bus.read(addr, reg)
    assert k.calls == before


def test_disallowed_writes_are_refused_before_any_ioctl():
    bus, k = _identified()
    before = list(k.calls)
    refused = [
        lambda: bus.write_byte(0x19, 0x82, 0),  # write configuration: stored on the stick
        lambda: bus.write_byte(0x19, 0x23, 0),  # bootloader switch
        lambda: bus.write_byte(0x19, 0x0B, 0),  # reset buffer (the non-direct path)
        lambda: bus.write_byte(0x19, 0x20, 0),  # binary data (the non-direct path)
        lambda: bus.write_byte(0x19, 0x26, 0),
        lambda: bus.write_byte(0x19, 0x61, 1),  # only 0x00 is ever written
        lambda: bus.write_byte(0x19, 0x21, False),
        lambda: bus.write_byte(0x1A, 0x61, 0),  # never probed
        lambda: bus.write_byte(0x50, 0x61, 0),
        lambda: bus.write_block(0x19, 0x82, b"\x01"),
        lambda: bus.write_block(0x19, 0x30, b"\x01"),
        lambda: bus.write_block(0x19, 0x31, b""),
        lambda: bus.write_block(0x19, 0x31, bytes(33)),
        lambda: bus.write_block(0x19, 0x31, [1, 2, 3]),
        lambda: bus.write_block(0x19, 49.0, b"\x01"),  # 0x31 as a float
        lambda: bus.write_block(0x1B, 0x31, b"\x01"),  # nothing identified there
        lambda: bus.write_block(0x58, 0x31, b"\x01"),
    ]
    for call in refused:
        with pytest.raises(cd.Refused):
            call()
    assert k.calls == before
    bus.write_block(0x19, 0x32, bytes(6))  # the allowed ones still work
    assert k.calls == before + [cd.I2C_SMBUS]


def test_block_writes_need_identification_not_just_the_id_bytes():
    k = kernel(a0x19=SimCorsairStick())
    bus = safe(k)
    with pytest.raises(cd.Refused):
        bus.identify(0x19)  # not probed yet
    with pytest.raises(cd.Refused):
        bus.write_byte(0x19, 0x61, 0)
    assert k.calls == [cd.I2C_FUNCS]
    bus.probe(0x19)
    n = len(k.calls)
    with pytest.raises(cd.Refused):
        bus.write_block(0x19, 0x31, bytes(32))  # probed, not identified
    assert len(k.calls) == n


@pytest.mark.parametrize(
    "setup, reason",
    [
        (lambda s: s.regs.update({0x43: 0x00}), "register 0x43 = 0x00"),
        (lambda s: s.regs.update({0x44: 0x02}), "register 0x44 = 0x02"),
        (lambda s: setattr(s, "checksum", 0x00), "checksum mismatch"),
        (lambda s: setattr(s, "info", bytes([0x0B, 0x05]) + s.info[2:]), "vendor 0x050b"),
        (
            lambda s: setattr(s, "info", s.info[:2] + bytes([0x00, 0x01]) + s.info[4:]),
            "unknown Corsair DDR5 product 0x0100",
        ),
        (lambda s: setattr(s, "info", s.info[:28] + bytes([3]) + s.info[29:]), "protocol 3 has no direct mode"),
    ],
)
def test_identification_rejects_and_then_refuses_writes(setup, reason):
    stick = SimCorsairStick()
    setup(stick)
    if stick.checksum == 0x00 and cd.crc8(stick.info) == 0x00:
        stick.checksum = 0x01
    k = kernel(a0x19=stick)
    bus = safe(k)
    with pytest.raises(cd.NotIdentified, match=reason):
        bus.probe(0x19)
        bus.identify(0x19)
    n = len(k.calls)
    with pytest.raises(cd.Refused):
        bus.write_block(0x19, 0x31, bytes(32))
    assert len(k.calls) == n and stick.blocks == []
    if "register 0x4" in reason:
        assert k.writes() == []  # a failed 0x43/0x44 check writes nothing at all
    else:
        assert k.writes() == [(0x19, "w", 0x61, 0), (0x19, "w", 0x21, 0)]  # only the device-info select


def test_identify_reads_device_info_like_openrgb_and_a_failed_retry_revokes_writes():
    stick = SimCorsairStick(pid=0x0901, protocol=4)
    k = kernel(a0x19=stick)
    bus = safe(k)
    bus.probe(0x19)
    got = bus.identify(0x19)
    assert got.device.name == "Corsair Vengeance RGB DDR5" and got.info.pid == 0x0901
    assert got.describe() == {
        "addr": 0x19,
        "name": "Corsair Vengeance RGB DDR5",
        "pid": 0x0901,
        "leds": 10,
        "firmware": "1.3.300",
        "protocol": 4,
    }
    regs = [(t[1], t[2]) for t in k.transfers]
    assert regs == [("r", 0x43), ("r", 0x44), ("w", 0x61), ("w", 0x21)] + [("r", 0x40)] * 32 + [("r", 0x42)]
    stick.checksum = (cd.crc8(stick.info) + 1) & 0xFF
    with pytest.raises(cd.NotIdentified):
        bus.identify(0x19)
    with pytest.raises(cd.Refused):
        bus.write_block(0x19, 0x31, bytes(32))


def test_set_colors_writes_one_direct_frame():
    stick = SimCorsairStick(pid=0x0600)  # 12 LEDs, reversed
    bus, k = _identified(kernel(a0x1b=stick), 0x1B)
    k.transfers.clear()
    cd.set_colors(bus, bus.identify(0x1B), GREY)
    packet = cd.direct_packet(cd.device_for(0x0600), GREY)
    assert [t for t in k.transfers if t[1] == "block"] == [
        (0x1B, "block", 0x31, packet[:32]),
        (0x1B, "block", 0x32, packet[32:]),
    ]
    assert stick.colors() == list(reversed(GREY))


# ---------------- scanning ----------------
def test_scan_lists_sticks_and_explains_the_rest():
    other = SimCorsairStick()
    other.regs[0x43] = 0x00
    unknown = SimCorsairStick(pid=0x0123)
    k = kernel(a0x19=SimCorsairStick(), a0x1b=SimCorsairStick(pid=0x0811), a0x1c=other, a0x1d=unknown)
    k.busy.add(0x1A)
    sleeps = []
    sticks, skipped = cd.scan(safe(k), sleep=sleeps.append)
    assert [(s.addr, s.device.leds) for s in sticks] == [(0x19, 10), (0x1B, 12)]
    assert skipped == [
        {"addr": 0x1A, "reason": "in use by a kernel driver", "retry": False},
        {"addr": 0x1C, "reason": "not a Corsair RGB controller (register 0x43 = 0x00)", "retry": False},
        {"addr": 0x1D, "reason": "unknown Corsair DDR5 product 0x0123: its LED count isn't known", "retry": False},
    ]
    assert sleeps == [0.01] * 7  # between the 8 addresses
    slaves = [t[0] for t in k.transfers]
    assert set(slaves) <= set(range(0x18, 0x20))
    assert [t for t in k.writes() if t[0] in (0x1A, 0x1C, 0x18, 0x1E, 0x1F)] == []


class TroubleKernel(FakeI2CKernel):
    """Transfers to ``eio`` addresses fail on the device-info reads, to
    ``busy`` ones with EBUSY (the bus, not the address), to ``remote`` ones
    with EREMOTEIO (a NACK on some adapters)."""

    def __init__(self, sticks, eio=(), busy=(), remote=()):
        super().__init__(sticks)
        self.eio, self.busy_bus, self.remote = set(eio), set(busy), set(remote)

    def ioctl(self, fd, request, arg):
        if request == cd.I2C_SMBUS:
            if self.addr in self.remote:
                raise OSError(errno.EREMOTEIO, "Remote I/O error")
            if self.addr in self.busy_bus:
                raise OSError(errno.EBUSY, "Device or resource busy")
            if self.addr in self.eio and arg.command == cd.REG_GET_BINARY_DATA:
                raise OSError(errno.EIO, "Input/output error")
        return super().ioctl(fd, request, arg)


def test_scan_retries_a_checksum_mismatch_but_not_a_real_answer():
    garbled = SimCorsairStick()
    garbled.checksum = (cd.crc8(garbled.info) + 1) & 0xFF  # a corrupted transfer
    foreign = SimCorsairStick()
    foreign.info = bytes([0x0B, 0x05]) + foreign.info[2:]  # a valid answer: not Corsair
    foreign.checksum = cd.crc8(foreign.info)
    k = kernel(a0x18=SimCorsairStick(), a0x19=garbled, a0x1a=foreign)
    found, skipped = cd.scan(safe(k), sleep=lambda s: None)
    assert [s.addr for s in found] == [0x18]
    assert [(x["addr"], x["retry"]) for x in skipped] == [(0x19, True), (0x1A, False)]
    assert skipped[0]["reason"].startswith("device info checksum mismatch")
    assert issubclass(cd.GarbledInfo, cd.NotIdentified)  # still refused writes, like any other


def test_scan_marks_io_trouble_worth_retrying_and_skips_nacks():
    sticks = {a: SimCorsairStick() for a in (0x18, 0x19, 0x1A, 0x1B)}
    k = TroubleKernel(sticks, eio={0x19}, busy={0x1A}, remote={0x1B})
    k.busy.add(0x1C)  # I2C_SLAVE says a driver holds it
    found, skipped = cd.scan(safe(k), sleep=lambda s: None)
    assert [s.addr for s in found] == [0x18]
    assert skipped == [
        {"addr": 0x19, "reason": "I/O error: Input/output error", "retry": True},  # mid-identification
        {"addr": 0x1A, "reason": "the bus was busy", "retry": True},
        {"addr": 0x1C, "reason": "in use by a kernel driver", "retry": False},
    ]  # 0x1B: EREMOTEIO, nothing there
    assert [t for t in k.writes() if t[1] == "block"] == []


# ---------------- DDR5 only ----------------
def _dimm(dmi, n, mem_type, size=16384, length=0x28):
    """An SMBIOS type 17 entry as /sys/firmware/dmi/entries/17-N/raw holds it:
    the formatted area (Size at 0x0C, Memory Type at 0x12), then its strings."""
    d = dmi / f"17-{n}"
    d.mkdir(parents=True, exist_ok=True)
    raw = bytearray(length)
    raw[0], raw[1] = 17, length
    raw[0x0C:0x0E] = size.to_bytes(2, "little")
    if length > 0x12:
        raw[0x12] = mem_type
    (d / "raw").write_bytes(bytes(raw) + b"Controller0-ChannelA-DIMM1\0BANK 0\0\0")


def test_require_ddr5_reads_smbios_type_17(tmp_path):
    dmi = tmp_path / "entries"
    _dimm(dmi, 0, 0x22)
    _dimm(dmi, 1, 0x02, size=0)  # an empty slot: its type doesn't count
    _dimm(dmi, 2, 0x22, size=0xFFFF)  # size unknown, but a module is there
    _dimm(dmi, 3, 0x22, size=0x7FFF)  # size in the extended field
    (dmi / "16-0").mkdir()  # the physical memory array: not a module
    (dmi / "16-0" / "raw").write_bytes(bytes([16, 0x17]) + bytes(0x15))
    assert cd.require_ddr5(dmi) == 3


@pytest.mark.parametrize(
    "dimms, reason",
    [
        ([(0x1A, 16384), (0x1A, 16384)], "not a DDR5 system: the firmware lists DDR4 memory"),
        ([(0x22, 16384), (0x1A, 16384)], "not a DDR5 system: the firmware lists DDR4 memory"),
        ([(0x22, 16384), (0x02, 8192)], "not a DDR5 system: the firmware lists type 0x02 memory"),
        ([(0x22, 0), (0x22, 0)], "lists no installed module"),
        ([], "lists no installed module"),
    ],
)
def test_require_ddr5_refuses_anything_else(tmp_path, dimms, reason):
    dmi = tmp_path / "entries"
    dmi.mkdir()
    for n, (mem_type, size) in enumerate(dimms):
        _dimm(dmi, n, mem_type, size)
    with pytest.raises(cd.NoBus, match=reason):
        cd.require_ddr5(dmi)


def test_require_ddr5_refuses_an_unreadable_or_odd_table(tmp_path):
    with pytest.raises(cd.NoBus, match="cannot read the firmware's memory table"):
        cd.require_ddr5(tmp_path / "missing")
    dmi = tmp_path / "entries"
    (dmi / "17-0").mkdir(parents=True)
    with pytest.raises(cd.NoBus, match="cannot read"):
        cd.require_ddr5(dmi)  # no raw file (on the real system: not root)
    _dimm(dmi, 0, 0x22, length=0x12)  # an SMBIOS 2.0 entry, too short to hold Memory Type
    with pytest.raises(cd.NoBus, match="isn't a memory device entry"):
        cd.require_ddr5(dmi)
    (dmi / "17-0" / "raw").write_bytes(bytes([4, 0x30]) + bytes(0x2E))  # wrong type byte
    with pytest.raises(cd.NoBus, match="isn't a memory device entry"):
        cd.require_ddr5(dmi)


def test_dmi_offsets_follow_the_smbios_spec():
    assert (cd.DMI_MEMORY_DEVICE, cd.DMI_SIZE_OFFSET, cd.DMI_TYPE_OFFSET, cd.DMI_DDR5) == (17, 0x0C, 0x12, 0x22)
    assert cd.SYSFS_DMI == cd.Path("/sys/firmware/dmi/entries")


# ---------------- bus choice ----------------
def _adapter(sysfs, n, name):
    d = sysfs / f"i2c-{n}"
    d.mkdir(parents=True)
    (d / "name").write_text(name + "\n")


def test_find_intel_bus_by_name(tmp_path):
    sysfs, dev = tmp_path / "sys", tmp_path / "dev"
    dev.mkdir()
    _adapter(sysfs, 0, "i915 gmbus dpa")
    _adapter(sysfs, 3, "SMBus I801 adapter at 0000:00:1f.4")
    _adapter(sysfs, 12, "NVIDIA i2c adapter 1 at 1:00.0")
    (sysfs / "3-0050").mkdir()  # a client device, not an adapter
    (sysfs / "i2c-MSFT8000:00").mkdir()
    (sysfs / "i2c-7").mkdir()  # no name file: skipped
    (dev / "i2c-3").write_text("")
    assert cd.find_intel_bus(sysfs, dev) == ("SMBus I801 adapter at 0000:00:1f.4", str(dev / "i2c-3"))


def test_find_intel_bus_wants_exactly_one_with_its_node(tmp_path):
    sysfs, dev = tmp_path / "sys", tmp_path / "dev"
    dev.mkdir()
    _adapter(sysfs, 0, "i915 gmbus dpa")
    _adapter(sysfs, 1, "SMBus PIIX4 adapter port 0 at 0b00")  # AMD: not supported
    with pytest.raises(cd.NoBus, match="no Intel SMBus adapter"):
        cd.find_intel_bus(sysfs, dev)
    _adapter(sysfs, 4, "SMBus I801 adapter at efa0")
    with pytest.raises(cd.NoBus, match="i2c-dev module isn't loaded"):
        cd.find_intel_bus(sysfs, dev)
    _adapter(sysfs, 9, "SMBus I801 adapter at 0000:00:1f.4")
    (dev / "i2c-4").write_text("")
    (dev / "i2c-9").write_text("")
    with pytest.raises(cd.NoBus, match=r"2 Intel SMBus adapters \(i2c-4, i2c-9\)"):
        cd.find_intel_bus(sysfs, dev)
    with pytest.raises(cd.NoBus, match="cannot list"):
        cd.find_intel_bus(tmp_path / "missing", dev)


def test_provenance():
    assert cd.OPENRGB_COMMIT in cd.__doc__ and "GPL-2.0-or-later" in cd.__doc__
    for name in ("CorsairDRAMController", "CorsairDRAMControllerDetect.cpp", "CorsairDRAMDevices", "Erik Gilling"):
        assert name in cd.__doc__, name
