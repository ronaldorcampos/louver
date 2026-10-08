import pytest

from unihub.devices import LightingEffect, StrimerPlus, UniHubALV2

from .fakes import HUB_RESPONSES, STRIMER_RESPONSES, FakeTransport


def hub(responses=HUB_RESPONSES):
    t = FakeTransport(responses)
    return UniHubALV2(t, sleep=lambda s: None), t


def test_speed_packet_is_percent_per_group():
    h, t = hub()
    h.set_speed(2, 55)
    h.set_speed(0, 150)  # clamped
    h.set_speed(3, -4)
    assert t.features == [bytes([0xE0, 0x22, 0x00, 55]), bytes([0xE0, 0x20, 0x00, 100]), bytes([0xE0, 0x23, 0x00, 0])]


def test_group_bounds_are_enforced():
    h, t = hub()
    with pytest.raises(ValueError):
        h.set_speed(4, 50)
    with pytest.raises(ValueError):
        h.set_fan_count(0, 7)
    assert t.features == []


def test_fan_count_packet_matches_reference_layout():
    # Same expectation as lian-li-linux's quantity test for AlV2Fan.
    h, t = hub()
    h.set_fan_count(2, 3)
    assert t.features == [bytes([0xE0, 0x10, 0x60, 3, 3, 0])]


def test_mb_pwm_sync_mask_and_enable_bits():
    h, t = hub()
    h.set_mb_pwm_sync(1, True)
    h.set_mb_pwm_sync(3, False)
    assert t.features == [bytes([0xE0, 0x10, 0x62, 0x22, 0, 0]), bytes([0xE0, 0x10, 0x62, 0x80, 0, 0])]


def test_firmware_and_rpm_parsing():
    h, t = hub()
    fw = h.read_firmware()
    assert fw.valid_alv2 and fw.version == "v1.2"
    assert h.read_rpms() == [1200, 800, 0, 1950]
    assert t.features == [bytes([0xE0, 0x50, 0x01]), bytes([0xE0, 0x50, 0x00])]


def test_static_lighting_writes_both_rings_rbg_order_then_commits():
    h, t = hub()
    h.fan_counts[1] = 2
    h.set_lighting(1, LightingEffect(mode="Static", colors=((10, 20, 30), (40, 50, 60))))
    inner, outer = t.outputs
    assert inner[:2] == bytes([0xE0, 0x32]) and outer[:2] == bytes([0xE0, 0x33])
    assert len(inner) == 2 + 6 * 8 * 3 and len(outer) == 2 + 6 * 12 * 3
    # fan 0 = colour 0, fan 1 = colour 1, fan 2 cycles back to colour 0
    assert inner[2:5] == bytes([10, 30, 20])
    assert inner[2 + 8 * 3 : 5 + 8 * 3] == bytes([40, 60, 50])
    assert inner[2 + 16 * 3 : 5 + 16 * 3] == bytes([10, 30, 20])
    assert t.features[0] == bytes([0xE0, 0x12, 1, 0, 0, 0])  # inner: static, normal speed, cw, max bright
    assert t.features[1] == bytes([0xE0, 0x13, 1, 0, 0, 0])
    assert t.features[-2:] == [bytes([0xE0, 0x10, 0x60, 2, 2, 0]), bytes([0xE0, 0x60, 0x00, 0x01])]
    # colours go out before each ring's effect command
    kinds = [k for k, _ in t.log]
    assert kinds[:4] == ["O", "F", "O", "F"]


def test_outer_only_modes_skip_inner_ring_and_rainbow_maps_per_ring():
    h, t = hub()
    h.set_lighting(0, LightingEffect(mode="Tornado"))  # outer-only
    assert [o[1] for o in t.outputs] == [0x31]
    t.features.clear(); t.outputs.clear()
    h.set_lighting(0, LightingEffect(mode="Rainbow", speed=4, brightness=-1, direction="ccw"))
    assert t.features[0] == bytes([0xE0, 0x10, 5, 0xFE, 1, 8])
    assert t.features[1] == bytes([0xE0, 0x11, 43, 0xFE, 1, 8])


def test_corner_colours_are_outer_ring_only_and_four_per_fan():
    h, t = hub()
    cols = ((1, 0, 0), (2, 0, 0), (3, 0, 0), (4, 0, 0))
    h.set_lighting(0, LightingEffect(mode="Static Colorful", colors=cols, ring="both"))
    (outer,) = t.outputs
    assert outer[1] == 0x31
    reds = outer[2::3]
    assert list(reds[:12]) == [1, 1, 1, 2, 2, 2, 3, 3, 3, 4, 4, 4]


def test_unknown_mode_rejected():
    h, _ = hub()
    with pytest.raises(ValueError):
        h.set_lighting(0, LightingEffect(mode="Disco Inferno"))


def test_strimer_firmware_ports_and_enable_bitmap():
    t = FakeTransport(STRIMER_RESPONSES)
    s = StrimerPlus(t, sleep=lambda s: None)
    assert s.read_firmware() == "v1.2"
    assert s.read_channel2_ports() == 4 and s.port_count == 10
    t.features.clear()
    s.set_lighting(LightingEffect(mode="Static", colors=((9, 8, 7),)))
    assert len(t.outputs) == 10
    assert t.outputs[0][:5] == bytes([0xE0, 0x30, 9, 7, 8]) and len(t.outputs[0]) == 2 + 27 * 3
    # all 6 ports of ch1 + 4 of ch2
    assert t.features[-1] == bytes([0xE0, 0x2C, 0x03, 0xFF])
    t.features.clear()
    s.set_lighting(LightingEffect(mode="Ripple"))  # channel-wide mode -> base ports only
    assert t.features[-1] == bytes([0xE0, 0x2C, 0x00, 0x41])


def test_strimer_mb_sync():
    t = FakeTransport(STRIMER_RESPONSES)
    s = StrimerPlus(t, sleep=lambda s: None)
    s.set_mb_rgb_sync(True)
    assert t.features == [bytes([0xE0, 0x10, 0x40, 1, 0, 0]), bytes([0xE0, 0x2C, 0, 0])]


class StaleTransport(FakeTransport):
    """Models the real hub: a reply appears only after `lag` input reads;
    until then GET_INPUT keeps returning the previous reply."""

    def __init__(self, responses, lag=3):
        super().__init__(responses)
        self.lag, self.pending, self.current = lag, 0, b"\xf0" + b"\x11" * 8

    def send_feature(self, data):
        super().send_feature(data)
        self.pending = self.lag

    def get_input(self, report_id, length):
        if self.pending:
            self.pending -= 1
            if self.pending == 0:
                self.current = self.responses.get(self._last, self.current)
        return self.current.ljust(length, b"\0")


def test_queries_wait_past_stale_replies():
    t = StaleTransport(HUB_RESPONSES)
    h = UniHubALV2(t, sleep=lambda s: None)
    fw = h.read_firmware()  # buffer starts out holding an RPM reply
    assert fw.valid_alv2
    assert h.read_rpms() == [1200, 800, 0, 1950]  # buffer held the firmware reply


def test_query_times_out_instead_of_returning_garbage():
    now = [0.0]

    def tick(s):
        now[0] += s

    t = StaleTransport(HUB_RESPONSES, lag=10**9)
    t.current = bytes([0xE0, 0x50, 0x80, 0xC6, 0x04])  # firmware reply stuck in the buffer
    h = UniHubALV2(t, sleep=tick, clock=lambda: now[0])
    with pytest.raises(TimeoutError):
        h.read_rpms()
